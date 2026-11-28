# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Per-user limits on MCP tool calls, so one user cannot tie up a shared server.

Counters live in Redis under the site's own keys. Nothing is counted unless
Assistant Core Settings > Enable Usage Limits is on. Every check fails open: if
Redis cannot be reached, the call runs.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from frappe_assistant_core.utils.audit_trail import AUDIT_STATUS_RATE_LIMITED, log_tool_execution

MODE_BLOCK = "Block"
MODE_LOG_ONLY = "Log only"

# A limit left at 0 or blank uses its default, the same rule as the code execution limits.
DEFAULT_LIMITS = {
    "max_calls_per_user_per_day": 3000,
    "max_calls_per_user_per_minute": 60,
    "max_tool_seconds_per_user_per_day": 7200,
    "max_concurrent_calls_per_user": 6,
    "max_concurrent_calls_per_site": 12,
}

EXEMPT_USERS = {"Administrator"}
DAY_TTL_SECONDS = 2 * 86400
MINUTE_TTL_SECONDS = 120
# Frees an in-flight slot whose worker was killed before it could give the slot back.
IN_FLIGHT_TTL_SECONDS = 300
SITE = "__site__"


@dataclass
class Limits:
    enabled: bool
    mode: str
    calls_per_day: int
    calls_per_minute: int
    tool_seconds_per_day: int
    concurrent_per_user: int
    concurrent_per_site: int


@dataclass
class Ticket:
    """One tool call's hold on the counters, handed back to after_call."""

    user: str
    day: str
    started: float
    holds_slots: bool = False
    limit_hit: Optional[str] = None
    message: Optional[str] = None
    refused: bool = False


def limits_from_settings(settings) -> Limits:
    def limit(fieldname: str) -> int:
        return cint(settings.get(fieldname)) or DEFAULT_LIMITS[fieldname]

    return Limits(
        enabled=bool(cint(settings.get("enable_usage_limits"))),
        mode=settings.get("usage_limit_mode") or MODE_BLOCK,
        calls_per_day=limit("max_calls_per_user_per_day"),
        calls_per_minute=limit("max_calls_per_user_per_minute"),
        tool_seconds_per_day=limit("max_tool_seconds_per_user_per_day"),
        concurrent_per_user=limit("max_concurrent_calls_per_user"),
        concurrent_per_site=limit("max_concurrent_calls_per_site"),
    )


def get_limits() -> Limits:
    return limits_from_settings(frappe.get_cached_doc("Assistant Core Settings"))


def _current_minute() -> str:
    return str(int(time.time() // 60))


def _key(*parts: str) -> bytes:
    return frappe.cache.make_key("fac_usage:" + ":".join(parts))


def _first_limit_hit(limits: Limits, counts: Dict[str, float]) -> Optional[tuple]:
    """(limit name, message for the client) of the first limit the call goes over."""
    if counts["site_in_flight"] > limits.concurrent_per_site:
        return "concurrent_per_site", _(
            "Too many assistant tool calls are running on this site right now (limit {0}). "
            "Try again in a moment."
        ).format(limits.concurrent_per_site)
    if counts["user_in_flight"] > limits.concurrent_per_user:
        return "concurrent_per_user", _(
            "You have too many assistant tool calls running at once (limit {0}). "
            "Wait for them to finish, then try again."
        ).format(limits.concurrent_per_user)
    if counts["minute_calls"] > limits.calls_per_minute:
        return "calls_per_minute", _(
            "Too many assistant tool calls in one minute (limit {0}). Wait a minute, then try again."
        ).format(limits.calls_per_minute)
    if counts["day_calls"] > limits.calls_per_day:
        return "calls_per_day", _(
            "Daily limit reached: {0} assistant tool calls a day. "
            "It resets at midnight, site time. Ask your administrator if you need more."
        ).format(limits.calls_per_day)
    if counts["day_seconds"] >= limits.tool_seconds_per_day:
        return "tool_seconds_per_day", _(
            "Daily limit reached: {0} seconds of assistant tool time a day. "
            "It resets at midnight, site time. Ask your administrator if you need more."
        ).format(limits.tool_seconds_per_day)
    return None


def before_call() -> Ticket:
    """Count one tool call for the session user. ticket.refused says the call must not run."""
    ticket = Ticket(user="", day="", started=time.monotonic())
    try:
        ticket.user = frappe.session.user
        if ticket.user in EXEMPT_USERS:
            return ticket
        limits = get_limits()
        if not limits.enabled:
            return ticket

        ticket.day = nowdate()
        user, day, minute = ticket.user, ticket.day, _current_minute()
        pipe = frappe.cache.pipeline(transaction=False)
        pipe.incr(_key("calls", day, user))
        pipe.expire(_key("calls", day, user), DAY_TTL_SECONDS)
        pipe.incr(_key("minute", minute, user))
        pipe.expire(_key("minute", minute, user), MINUTE_TTL_SECONDS)
        pipe.incr(_key("in_flight", user))
        pipe.expire(_key("in_flight", user), IN_FLIGHT_TTL_SECONDS)
        pipe.incr(_key("in_flight", SITE))
        pipe.expire(_key("in_flight", SITE), IN_FLIGHT_TTL_SECONDS)
        pipe.get(_key("seconds", day, user))
        day_calls, _e1, minute_calls, _e2, user_in_flight, _e3, site_in_flight, _e4, seconds = pipe.execute()
        ticket.holds_slots = True
    except Exception:
        _warn("Usage limit check skipped")
        return ticket

    hit = _first_limit_hit(
        limits,
        {
            "day_calls": day_calls,
            "minute_calls": minute_calls,
            "user_in_flight": user_in_flight,
            "site_in_flight": site_in_flight,
            "day_seconds": flt(seconds),
        },
    )
    if hit:
        ticket.limit_hit, ticket.message = hit
        if limits.mode == MODE_BLOCK:
            ticket.refused = True
            _release_slots(ticket)
    return ticket


def after_call(ticket: Ticket) -> None:
    """Give back the call's in-flight slots and add its run time to the user's day."""
    if not ticket.holds_slots:
        return
    try:
        seconds_key = _key("seconds", ticket.day, ticket.user)
        pipe = frappe.cache.pipeline(transaction=False)
        pipe.incrbyfloat(seconds_key, round(time.monotonic() - ticket.started, 3))
        pipe.expire(seconds_key, DAY_TTL_SECONDS)
        pipe.execute()
    except Exception:
        _warn("Usage limit seconds not recorded")
    _release_slots(ticket)


def _release_slots(ticket: Ticket) -> None:
    ticket.holds_slots = False
    try:
        keys = (_key("in_flight", ticket.user), _key("in_flight", SITE))
        pipe = frappe.cache.pipeline(transaction=False)
        for key in keys:
            pipe.decr(key)
        for key, value in zip(keys, pipe.execute()):
            # The slot's TTL ran out while the call was running; never leave the count below zero.
            if value < 0:
                frappe.cache.delete(key)
    except Exception:
        _warn("Usage limit slot not released")


def _warn(message: str) -> None:
    # The limiter must never break a tool call, not even through its own logging.
    try:
        frappe.logger("usage_limits").warning(message, exc_info=True)
    except Exception:
        pass


def record_limit_hit(ticket: Ticket, tool_name: str, arguments: Optional[Dict[str, Any]]) -> None:
    """Write the Rate Limited audit row the usage meters count.

    At most one row per user, limit and minute, so a client retrying in a loop adds no DB writes.
    """
    if not ticket.limit_hit:
        return
    try:
        logged_key = _key("logged", ticket.limit_hit, _current_minute(), ticket.user)
        pipe = frappe.cache.pipeline(transaction=False)
        pipe.incr(logged_key)
        pipe.expire(logged_key, MINUTE_TTL_SECONDS)
        if pipe.execute()[0] > 1:
            return
    except Exception:
        _warn("Usage limit audit throttle skipped")

    message = ticket.message if ticket.refused else _("Logged only, the call ran: {0}").format(ticket.message)
    log_tool_execution(
        tool_name=tool_name,
        user=ticket.user,
        arguments=arguments,
        status=AUDIT_STATUS_RATE_LIMITED,
        execution_time=0,
        error_message=message,
        error_type=ticket.limit_hit,
    )
