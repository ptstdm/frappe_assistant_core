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

from frappe_assistant_core.tests.base_test import BaseAssistantTest

DOCTYPE = "FAC Plugin Configuration"
TEST_USER = "fac-plugin-toggle-test@example.com"


def _plugin(plugin_name, enabled, was=None):
    """In-memory record for validate(); `was` is the stored value of an existing record."""
    doc = frappe.new_doc(DOCTYPE)
    doc.plugin_name = plugin_name
    doc.display_name = plugin_name.replace("_", " ").title()
    doc.enabled = enabled
    if was is not None:
        # Production: Document.save() loads the stored row into _doc_before_save before validate.
        before = frappe.new_doc(DOCTYPE)
        before.plugin_name = plugin_name
        before.enabled = was
        doc._doc_before_save = before
    return doc


@contextmanager
def _as_user(roles):
    frappe.set_user(TEST_USER)  # nosemgrep: frappe-setuser — act as a non-Administrator
    try:
        with patch("frappe.get_roles", autospec=True, return_value=["All", *roles]):
            yield
    finally:
        frappe.set_user("Administrator")  # nosemgrep: frappe-setuser


class TestPluginTogglePermissions(BaseAssistantTest):
    def test_system_manager_cannot_switch_on_a_stored_plugin(self):
        if not frappe.db.exists(DOCTYPE, "data_science"):
            self.skipTest("plugin configurations are not synced on this site")
        # Fresh installs store data_science disabled: enabled_plugins_list defaults to ["core"].
        frappe.db.set_value(DOCTYPE, "data_science", "enabled", 0)
        doc = frappe.get_doc(DOCTYPE, "data_science")
        doc.enabled = 1
        with _as_user(["System Manager"]):
            # The save PluginPersistence.save_plugin_state makes; refused before any write.
            self.assertRaises(frappe.PermissionError, doc.save, ignore_permissions=True)
        self.assertEqual(frappe.db.get_value(DOCTYPE, "data_science", "enabled"), 0)

    def test_system_manager_cannot_insert_an_enabled_plugin(self):
        doc = _plugin("_test_fac_toggle_plugin", 1)
        with _as_user(["System Manager"]):
            self.assertRaises(frappe.PermissionError, doc.insert, ignore_permissions=True)
        self.assertFalse(frappe.db.exists(DOCTYPE, "_test_fac_toggle_plugin"))

    def test_assistant_admin_cannot_enable_non_core_plugin(self):
        for plugin_name in ("data_science", "visualization", "custom_tools", "faco"):
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
            # REST and form posts can send the Check value as a string.
            _plugin("custom_tools", "1", was=1).validate()

    def test_administrator_can_enable_any_plugin(self):
        _plugin("data_science", 1, was=0).validate()
        _plugin("_test_fac_toggle_plugin", 1).validate()

    def test_user_without_plugin_role_cannot_toggle(self):
        with _as_user([]):
            self.assertRaises(frappe.PermissionError, _plugin("core", 0, was=1).validate)
            self.assertRaises(frappe.PermissionError, _plugin("core", 1, was=0).validate)
