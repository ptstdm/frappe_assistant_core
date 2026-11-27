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

"""The dashboard cache clear that runs after every audit row stays cheap on a shared Redis."""

from unittest.mock import patch

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import cache as fac_cache

MOST_USED_TOOLS_KEY = "frappe_assistant_core.utils.cache.get_cached_most_used_tools"


class TestDashboardCacheInvalidation(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self._reset_throttle()
        self.addCleanup(self._reset_throttle)

    def _reset_throttle(self):
        frappe.cache.delete(frappe.cache.make_key("fac_dashboard_cache_cleared"))

    def test_clears_the_cached_tool_statistics(self):
        fac_cache.get_cached_most_used_tools()
        self.assertTrue(frappe.cache.get_keys(MOST_USED_TOOLS_KEY))
        fac_cache.invalidate_dashboard_cache()
        self.assertFalse(frappe.cache.get_keys(MOST_USED_TOOLS_KEY))

    def test_many_audit_rows_in_a_minute_clear_once(self):
        # Every tool call writes an audit row, and each row runs this hook.
        with patch.object(frappe.cache, "delete_keys", wraps=frappe.cache.delete_keys) as delete_keys:
            for _ in range(5):
                fac_cache.invalidate_dashboard_cache()
        self.assertEqual(delete_keys.call_count, 2, "one scan per cached statistic, on the first row only")

    def test_redis_failure_never_reaches_the_audit_row(self):
        with patch.object(frappe.cache, "pipeline", side_effect=ConnectionError("redis down")):
            fac_cache.invalidate_dashboard_cache()
