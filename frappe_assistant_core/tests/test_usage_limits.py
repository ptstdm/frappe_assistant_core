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

"""Per-user MCP usage limits, driven through MCPServer._handle_tools_call as a client's call is."""

import time
from typing import Any, Dict
from unittest.mock import patch

import frappe

from frappe_assistant_core.core.base_tool import BaseTool
from frappe_assistant_core.mcp.server import MCPServer
from frappe_assistant_core.mcp.tool_adapter import build_tool_dict
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import usage_limits
from frappe_assistant_core.utils.usage_limits import DEFAULT_LIMITS, Limits

_TOOL = "usage_limit_test_tool"


class _CountingTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.name = _TOOL
        self.description = "Usage limit test tool"
        self.inputSchema = {"type": "object", "properties": {}}
        self.requires_permission = None  # skip the DocType permission check
        self.source_app = "frappe_assistant_core"
        self.runs = 0
        self.run_seconds = 0.0

    def execute(self, arguments: Dict[str, Any]) -> Any:
        self.runs += 1
        time.sleep(self.run_seconds)
        return {"ok": True}

    def log_execution(self, *args, **kwargs):
        # Only the limiter's Rate Limited rows are under test; the tool's own row would mix in.
        return None


def _limits(**overrides) -> Limits:
    values = {
        "enabled": True,
        "mode": usage_limits.MODE_BLOCK,
        "calls_per_day": DEFAULT_LIMITS["max_calls_per_user_per_day"],
        "calls_per_minute": DEFAULT_LIMITS["max_calls_per_user_per_minute"],
        "tool_seconds_per_day": DEFAULT_LIMITS["max_tool_seconds_per_user_per_day"],
        "concurrent_per_user": DEFAULT_LIMITS["max_concurrent_calls_per_user"],
        "concurrent_per_site": DEFAULT_LIMITS["max_concurrent_calls_per_site"],
    }
    values.update(overrides)
    return Limits(**values)


class TestUsageLimits(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self.user = self.make_throwaway_user("usage-limit")
        # A site key of its own, so the dev site's real in-flight count is never touched.
        site_patch = patch.object(usage_limits, "SITE", f"__test_site_{frappe.generate_hash(length=6)}__")
        site_patch.start()
        self.addCleanup(site_patch.stop)
        minute_patch = patch.object(usage_limits, "_current_minute", autospec=True, return_value="1")
        minute_patch.start()
        self.addCleanup(minute_patch.stop)
        self.addCleanup(self._clear_counters)
        self.tool = _CountingTool()
        self.registry = {_TOOL: build_tool_dict(self.tool)}
        # nosemgrep: frappe-setuser — runs as the limited user; tearDown puts Administrator back
        frappe.set_user(self.user)

    def tearDown(self):
        # nosemgrep: frappe-setuser — restore the test runner's user
        frappe.set_user("Administrator")
        super().tearDown()

    def _clear_counters(self):
        for name in (self.user, usage_limits.SITE):
            for key in frappe.cache.scan_iter(frappe.cache.make_key(f"fac_usage:*{name}*")):
                frappe.cache.delete(key)

    def _call(self, limits: Limits, tool_name: str = _TOOL, registry=None):
        with patch.object(usage_limits, "get_limits", autospec=True, return_value=limits):
            return MCPServer("test")._handle_tools_call(
                {"name": tool_name, "arguments": {}}, registry or self.registry
            )

    def _rate_limited_rows(self):
        return frappe.get_all(
            "Assistant Audit Log",
            filters={"user": self.user, "status": "Rate Limited"},
            fields=["tool_name", "error_type", "error_message"],
        )

    def _counter(self, *parts) -> int:
        return int(frappe.cache.incrby(usage_limits._key(*parts), 0))

    def _preset(self, value: int, *parts):
        frappe.cache.incrby(usage_limits._key(*parts), value)

    def test_off_counts_nothing(self):
        for _ in range(3):
            self.assertFalse(self._call(_limits(enabled=False, calls_per_day=1))["isError"])
        self.assertEqual(self.tool.runs, 3)
        self.assertEqual(self._counter("calls", frappe.utils.nowdate(), self.user), 0)

    def test_calls_under_the_limit_run_and_give_their_slots_back(self):
        for _ in range(3):
            self.assertFalse(self._call(_limits(calls_per_day=3))["isError"])
        self.assertEqual(self.tool.runs, 3)
        self.assertEqual(self._counter("calls", frappe.utils.nowdate(), self.user), 3)
        self.assertEqual(self._counter("in_flight", self.user), 0)
        self.assertEqual(self._counter("in_flight", usage_limits.SITE), 0)
        self.assertEqual(self._rate_limited_rows(), [])

    def test_call_over_the_daily_limit_is_refused_and_logged(self):
        self._call(_limits(calls_per_day=2))
        self._call(_limits(calls_per_day=2))
        result = self._call(_limits(calls_per_day=2))

        self.assertTrue(result["isError"])
        self.assertIn("Daily limit reached", result["content"][0]["text"])
        self.assertEqual(self.tool.runs, 2)
        rows = self._rate_limited_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tool_name, _TOOL)
        self.assertEqual(rows[0].error_type, "calls_per_day")
        self.assertEqual(self._counter("in_flight", self.user), 0)

    def test_a_client_retrying_in_a_loop_writes_one_audit_row_a_minute(self):
        self._call(_limits(calls_per_day=1))
        for _ in range(5):
            self.assertTrue(self._call(_limits(calls_per_day=1))["isError"])
        self.assertEqual(len(self._rate_limited_rows()), 1)

    def test_call_over_the_per_minute_limit_is_refused(self):
        self._call(_limits(calls_per_minute=1))
        result = self._call(_limits(calls_per_minute=1))
        self.assertTrue(result["isError"])
        self.assertIn("in one minute", result["content"][0]["text"])
        self.assertEqual(self._rate_limited_rows()[0].error_type, "calls_per_minute")

    def test_call_over_the_user_concurrency_limit_is_refused_and_frees_its_slot(self):
        # Two calls still running: a client sends parallel tool calls in one turn.
        self._preset(2, "in_flight", self.user)
        result = self._call(_limits(concurrent_per_user=2))
        self.assertTrue(result["isError"])
        self.assertIn("running at once", result["content"][0]["text"])
        self.assertEqual(self.tool.runs, 0)
        self.assertEqual(self._counter("in_flight", self.user), 2)

    def test_call_over_the_site_concurrency_limit_is_refused(self):
        # Other users' calls still running on the same site.
        self._preset(4, "in_flight", usage_limits.SITE)
        result = self._call(_limits(concurrent_per_site=4))
        self.assertTrue(result["isError"])
        self.assertIn("on this site", result["content"][0]["text"])
        self.assertEqual(self._counter("in_flight", usage_limits.SITE), 4)

    def test_slot_is_given_back_when_the_tool_raises(self):
        def broken(**kwargs):
            raise RuntimeError("tool blew up")

        registry = {_TOOL: dict(build_tool_dict(self.tool), fn=broken)}
        result = self._call(_limits(), registry=registry)
        self.assertTrue(result["isError"])
        self.assertIn("tool blew up", result["content"][0]["text"])
        self.assertEqual(self._counter("in_flight", self.user), 0)
        self.assertEqual(self._counter("in_flight", usage_limits.SITE), 0)

    def test_tool_time_is_added_up_and_limited(self):
        # A real tool takes time; this one returns in under a millisecond, which rounds to 0.
        self.tool.run_seconds = 0.02
        self._call(_limits())
        seconds_key = usage_limits._key("seconds", frappe.utils.nowdate(), self.user)
        self.assertGreaterEqual(frappe.cache.incrbyfloat(seconds_key, 0), 0.02)

        frappe.cache.incrbyfloat(seconds_key, 10)
        result = self._call(_limits(tool_seconds_per_day=10))
        self.assertTrue(result["isError"])
        self.assertIn("seconds of assistant tool time", result["content"][0]["text"])

    def test_log_only_mode_lets_the_call_run_and_logs_it(self):
        log_only = _limits(calls_per_day=1, mode=usage_limits.MODE_LOG_ONLY)
        self._call(log_only)
        result = self._call(log_only)
        self.assertFalse(result["isError"])
        self.assertEqual(self.tool.runs, 2)
        rows = self._rate_limited_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("Logged only", rows[0].error_message)
        self.assertEqual(self._counter("in_flight", self.user), 0)

    def test_administrator_is_never_limited(self):
        # nosemgrep: frappe-setuser — Administrator is the exempt user under test
        frappe.set_user("Administrator")
        for _ in range(3):
            self.assertFalse(self._call(_limits(calls_per_day=1))["isError"])
        self.assertEqual(self.tool.runs, 3)

    def test_unknown_tool_is_not_counted(self):
        result = self._call(_limits(calls_per_day=1), tool_name="no_such_tool")
        self.assertTrue(result["isError"])
        self.assertEqual(self._counter("calls", frappe.utils.nowdate(), self.user), 0)

    def test_call_runs_when_redis_fails(self):
        with patch.object(frappe.cache, "pipeline", side_effect=ConnectionError("redis down")):
            result = self._call(_limits(calls_per_day=1))
        self.assertFalse(result["isError"])
        self.assertEqual(self.tool.runs, 1)

    def test_blank_or_zero_limits_use_the_defaults(self):
        # A site that had Assistant Core Settings before this section existed reads every field as blank.
        limits = usage_limits.limits_from_settings(frappe._dict())
        self.assertFalse(limits.enabled)
        self.assertEqual(limits.mode, usage_limits.MODE_BLOCK)
        self.assertEqual(limits.calls_per_day, DEFAULT_LIMITS["max_calls_per_user_per_day"])
        zeros = usage_limits.limits_from_settings(frappe._dict({name: 0 for name in DEFAULT_LIMITS}))
        self.assertEqual(zeros.concurrent_per_site, DEFAULT_LIMITS["max_concurrent_calls_per_site"])

    def test_saving_settings_shows_the_default_for_a_blank_limit(self):
        settings = frappe.get_doc("Assistant Core Settings")
        settings.max_calls_per_user_per_day = 0
        settings.max_concurrent_calls_per_site = None
        settings._fill_usage_limit_defaults()
        self.assertEqual(settings.max_calls_per_user_per_day, DEFAULT_LIMITS["max_calls_per_user_per_day"])
        self.assertEqual(
            settings.max_concurrent_calls_per_site, DEFAULT_LIMITS["max_concurrent_calls_per_site"]
        )
