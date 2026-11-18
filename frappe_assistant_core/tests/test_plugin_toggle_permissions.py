# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Only Administrator can enable plugins other than Core (FAC Plugin Configuration.validate)."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

DOCTYPE = "FAC Plugin Configuration"
TEST_USER = "fac-plugin-toggle-test@example.com"


def _plugin(plugin_name, enabled, was=None):
    """In-memory record; `was` fakes the saved value so validate sees an update."""
    doc = frappe.new_doc(DOCTYPE)
    doc.plugin_name = plugin_name
    doc.display_name = plugin_name.replace("_", " ").title()
    doc.enabled = enabled
    if was is not None:
        before = frappe.new_doc(DOCTYPE)
        before.plugin_name = plugin_name
        before.enabled = was
        doc._doc_before_save = before
    return doc


@contextmanager
def _as_user(roles):
    frappe.set_user(TEST_USER)
    try:
        with patch("frappe.get_roles", return_value=["All", *roles]):
            yield
    finally:
        frappe.set_user("Administrator")


class TestPluginTogglePermissions(FrappeTestCase):
    def test_system_manager_cannot_enable_non_core_plugin(self):
        # A real insert, so the controller wiring is exercised; it is refused before any write.
        doc = _plugin("_test_fac_toggle_plugin", 1)
        with _as_user(["System Manager"]):
            self.assertRaises(frappe.PermissionError, doc.insert, ignore_permissions=True)
        self.assertFalse(frappe.db.exists(DOCTYPE, "_test_fac_toggle_plugin"))

    def test_assistant_admin_cannot_enable_non_core_plugin(self):
        for plugin_name in ("data_science", "visualization", "custom_tools"):
            with self.subTest(plugin_name=plugin_name), _as_user(["Assistant Admin"]):
                self.assertRaises(frappe.PermissionError, _plugin(plugin_name, 1, was=0).validate)

    def test_system_manager_can_enable_core(self):
        with _as_user(["System Manager"]):
            _plugin("core", 1, was=0).validate()

    def test_system_manager_can_disable_any_plugin(self):
        with _as_user(["System Manager"]):
            _plugin("custom_tools", 0, was=1).validate()
            _plugin("core", 0, was=1).validate()

    def test_saving_an_already_enabled_plugin_is_allowed(self):
        with _as_user(["System Manager"]):
            _plugin("custom_tools", 1, was=1).validate()
            _plugin("custom_tools", "1", was=1).validate()

    def test_administrator_can_enable_any_plugin(self):
        _plugin("data_science", 1, was=0).validate()
        _plugin("_test_fac_toggle_plugin", 1).validate()

    def test_user_without_plugin_role_cannot_toggle(self):
        with _as_user([]):
            self.assertRaises(frappe.PermissionError, _plugin("core", 0, was=1).validate)
            self.assertRaises(frappe.PermissionError, _plugin("core", 1, was=0).validate)
