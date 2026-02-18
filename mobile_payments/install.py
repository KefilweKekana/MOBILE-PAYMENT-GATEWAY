"""
Mobile Payments - Installation Script
Sets up required configurations, custom fields, and default data.
"""
from __future__ import unicode_literals

import frappe
from frappe import _


def after_install():
    """Run after the app is installed."""
    _create_modes_of_payment()
    _create_payment_gateway_entries()
    _create_custom_fields()
    frappe.db.commit()
    print("Mobile Payments app installed successfully!")


def after_uninstall():
    """Cleanup when the app is uninstalled."""
    _remove_custom_fields()
    frappe.db.commit()
    print("Mobile Payments app uninstalled.")


def _create_modes_of_payment():
    """Create default Mobile Money modes of payment."""
    modes = [
        {"name": "ZAAD", "type": "Phone"},
        {"name": "SAHAL", "type": "Phone"},
        {"name": "EVCPlus", "type": "Phone"},
        {"name": "Edahab", "type": "Phone"},
        {"name": "WaafiPay", "type": "Phone"},
    ]

    for mode in modes:
        if not frappe.db.exists("Mode of Payment", mode["name"]):
            doc = frappe.get_doc(
                {
                    "doctype": "Mode of Payment",
                    "mode_of_payment": mode["name"],
                    "type": mode["type"],
                    "enabled": 1,
                }
            )
            doc.insert(ignore_permissions=True)
            frappe.msgprint(_("Created Mode of Payment: {0}").format(mode["name"]))


def _create_payment_gateway_entries():
    """Create Payment Gateway entries for WaafiPay and Edahab."""
    gateways = ["WaafiPay", "Edahab"]

    for gw in gateways:
        if not frappe.db.exists("Payment Gateway", gw):
            doc = frappe.get_doc(
                {
                    "doctype": "Payment Gateway",
                    "gateway": gw,
                    "gateway_settings": "Mobile Payment Settings",
                }
            )
            doc.insert(ignore_permissions=True)
            frappe.msgprint(_("Created Payment Gateway: {0}").format(gw))


def _create_custom_fields():
    """Add custom fields to Sales Invoice and Payment Entry."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    custom_fields = {
        "Sales Invoice": [
            {
                "fieldname": "mobile_payment_section",
                "label": "Mobile Payment",
                "fieldtype": "Section Break",
                "insert_after": "payments_section",
                "collapsible": 1,
            },
            {
                "fieldname": "mobile_payment_status",
                "label": "Mobile Payment Status",
                "fieldtype": "Select",
                "options": "\nPending\nProcessing\nCompleted\nFailed\nCancelled\nRefunded",
                "insert_after": "mobile_payment_section",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_provider",
                "label": "Payment Provider",
                "fieldtype": "Select",
                "options": "\nWaafiPay\nEdahab",
                "insert_after": "mobile_payment_status",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_method",
                "label": "Payment Method",
                "fieldtype": "Select",
                "options": "\nZAAD\nSAHAL\nEVCPlus\nEdahab",
                "insert_after": "mobile_payment_provider",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_phone",
                "label": "Payment Phone Number",
                "fieldtype": "Data",
                "insert_after": "mobile_payment_method",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_column_break",
                "fieldtype": "Column Break",
                "insert_after": "mobile_payment_phone",
            },
            {
                "fieldname": "mobile_payment_reference",
                "label": "Payment Reference ID",
                "fieldtype": "Data",
                "insert_after": "mobile_payment_column_break",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_transaction_id",
                "label": "Transaction Log",
                "fieldtype": "Link",
                "options": "Mobile Payment Transaction Log",
                "insert_after": "mobile_payment_reference",
                "read_only": 1,
                "no_copy": 1,
            },
        ],
        "Payment Entry": [
            {
                "fieldname": "mobile_payment_reference",
                "label": "Mobile Payment Reference",
                "fieldtype": "Data",
                "insert_after": "reference_no",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "mobile_payment_transaction_id",
                "label": "Mobile Transaction Log",
                "fieldtype": "Link",
                "options": "Mobile Payment Transaction Log",
                "insert_after": "mobile_payment_reference",
                "read_only": 1,
                "no_copy": 1,
            },
        ],
    }

    create_custom_fields(custom_fields, update=True)


def _remove_custom_fields():
    """Remove custom fields on uninstall."""
    fields_to_remove = {
        "Sales Invoice": [
            "mobile_payment_section",
            "mobile_payment_status",
            "mobile_payment_provider",
            "mobile_payment_method",
            "mobile_payment_phone",
            "mobile_payment_column_break",
            "mobile_payment_reference",
            "mobile_payment_transaction_id",
        ],
        "Payment Entry": [
            "mobile_payment_reference",
            "mobile_payment_transaction_id",
        ],
    }

    for doctype, fields in fields_to_remove.items():
        for field in fields:
            custom_field = frappe.db.get_value(
                "Custom Field", {"dt": doctype, "fieldname": field}
            )
            if custom_field:
                frappe.delete_doc("Custom Field", custom_field, force=True)
