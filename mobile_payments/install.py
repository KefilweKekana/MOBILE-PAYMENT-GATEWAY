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


def after_migrate():
    """Run after every bench migrate — ensures custom fields always exist."""
    _create_custom_fields()
    frappe.db.commit()


@frappe.whitelist()
def setup_custom_fields():
    """
    Manually create/update all custom fields.
    Call this from the bench console or the browser if fields are missing:
      bench execute mobile_payments.install.setup_custom_fields
    Or via API:
      /api/method/mobile_payments.install.setup_custom_fields
    """
    _create_custom_fields()
    frappe.db.commit()
    return {"success": True, "message": "Custom fields created/updated successfully."}


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
    """Create Payment Gateway entries for WaafiPay and Edahab.
    
    The 'Payment Gateway' DocType comes from the 'payments' app
    (not core Frappe or ERPNext). Skip gracefully if not installed.
    """
    if not frappe.db.table_exists("Payment Gateway"):
        print("Payment Gateway DocType not found (payments app not installed). Skipping gateway setup.")
        return

    gateways = ["WaafiPay", "Edahab"]

    for gw in gateways:
        try:
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
        except Exception as e:
            frappe.log_error(
                message=f"Could not create Payment Gateway {gw}: {e}",
                title="Mobile Payments Install",
            )


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
            {
                "fieldname": "payment_links_section",
                "label": "Payment Links",
                "fieldtype": "Section Break",
                "insert_after": "mobile_payment_transaction_id",
                "collapsible": 1,
            },
            {
                "fieldname": "waafi_payment_link",
                "label": "Waafi Payment Link",
                "fieldtype": "Small Text",
                "insert_after": "payment_links_section",
                "read_only": 1,
                "no_copy": 1,
                "length": 2048,
            },
            {
                "fieldname": "payment_links_column_break",
                "fieldtype": "Column Break",
                "insert_after": "waafi_payment_link",
            },
            {
                "fieldname": "edahab_payment_link",
                "label": "Edahab Payment Link",
                "fieldtype": "Small Text",
                "insert_after": "payment_links_column_break",
                "read_only": 1,
                "no_copy": 1,
                "length": 2048,
            },
        ],
    }

    # Only add Patient Appointment fields if Healthcare module is installed
    if frappe.db.exists("DocType", "Patient Appointment"):
        custom_fields["Patient Appointment"] = [
            {
                "fieldname": "mobile_payment_section",
                "label": "Mobile Payment",
                "fieldtype": "Section Break",
                "insert_after": "notes",
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
            {
                "fieldname": "mobile_payment_sales_invoice",
                "label": "Sales Invoice",
                "fieldtype": "Link",
                "options": "Sales Invoice",
                "insert_after": "mobile_payment_transaction_id",
                "read_only": 1,
                "no_copy": 1,
                "description": "Auto-created Sales Invoice after a successful mobile payment",
            },
        ]

    custom_fields["Payment Entry"] = [
        {
            "fieldname": "mobile_payment_section",
            "label": "Mobile Payment",
            "fieldtype": "Section Break",
            "insert_after": "clearance_date",
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
            "label": "Mobile Transaction Log",
            "fieldtype": "Link",
            "options": "Mobile Payment Transaction Log",
            "insert_after": "mobile_payment_reference",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "payment_links_section",
            "label": "Payment Links",
            "fieldtype": "Section Break",
            "insert_after": "mobile_payment_transaction_id",
            "collapsible": 1,
        },
        {
            "fieldname": "waafi_payment_link",
            "label": "Waafi Payment Link",
            "fieldtype": "Small Text",
            "insert_after": "payment_links_section",
            "read_only": 1,
            "no_copy": 1,
            "length": 2048,
        },
        {
            "fieldname": "payment_links_column_break",
            "fieldtype": "Column Break",
            "insert_after": "waafi_payment_link",
        },
        {
            "fieldname": "edahab_payment_link",
            "label": "Edahab Payment Link",
            "fieldtype": "Small Text",
            "insert_after": "payment_links_column_break",
            "read_only": 1,
            "no_copy": 1,
            "length": 2048,
        },
    ]

    # Temporarily disable doctype field validation during custom field creation.
    # Some ERPNext instances have pre-existing broken fields (e.g. restaurant_table
    # with invalid options) that cause validate_fields_for_doctype() to throw when
    # ANY new custom field is added to that doctype. Monkey-patching the validator
    # to a no-op during our install avoids this without modifying existing data.
    from frappe.core.doctype.doctype import doctype as doctype_module

    original_validate = doctype_module.validate_fields_for_doctype
    doctype_module.validate_fields_for_doctype = lambda *args, **kwargs: None

    try:
        create_custom_fields(custom_fields, update=True)
    finally:
        doctype_module.validate_fields_for_doctype = original_validate


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
            "payment_links_section",
            "waafi_payment_link",
            "payment_links_column_break",
            "edahab_payment_link",
        ],
        "Patient Appointment": [
            "mobile_payment_section",
            "mobile_payment_status",
            "mobile_payment_provider",
            "mobile_payment_method",
            "mobile_payment_phone",
            "mobile_payment_column_break",
            "mobile_payment_reference",
            "mobile_payment_transaction_id",
            "mobile_payment_sales_invoice",
        ],
        "Payment Entry": [
            "mobile_payment_section",
            "mobile_payment_status",
            "mobile_payment_provider",
            "mobile_payment_method",
            "mobile_payment_phone",
            "mobile_payment_column_break",
            "mobile_payment_reference",
            "mobile_payment_transaction_id",
            "payment_links_section",
            "waafi_payment_link",
            "payment_links_column_break",
            "edahab_payment_link",
        ],
    }

    for doctype, fields in fields_to_remove.items():
        for field in fields:
            custom_field = frappe.db.get_value(
                "Custom Field", {"dt": doctype, "fieldname": field}
            )
            if custom_field:
                frappe.delete_doc("Custom Field", custom_field, force=True)
