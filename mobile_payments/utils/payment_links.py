"""
Auto-generate WaafiPay and Edahab HPP payment links on Sales Invoices.

When a Sales Invoice's due_date is today, both payment links are generated
and stored in the custom fields `waafi_payment_link` and `edahab_payment_link`.

Triggered by:
  - Sales Invoice on_submit / validate (if due_date == today)
  - Daily scheduled task (for existing invoices becoming due today)
  - Manual button click on the Sales Invoice form
"""
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import getdate, today, flt, now_datetime


def generate_payment_links_if_due(doc, method=None):
    """
    Hook for Sales Invoice on_submit / on_update_after_submit.
    Generates payment links if due_date is today and invoice is unpaid.
    """
    if not doc.due_date:
        return

    if getdate(doc.due_date) != getdate(today()):
        return

    # Only for submitted, unpaid invoices
    if doc.docstatus != 1:
        return

    if flt(doc.outstanding_amount) <= 0:
        return

    # Don't regenerate if both links already exist
    if doc.get("waafi_payment_link") and doc.get("edahab_payment_link"):
        return

    _generate_links_for_invoice(doc)


def daily_generate_payment_links():
    """
    Scheduled task: runs daily to generate payment links for all
    Sales Invoices whose due_date is today and are still unpaid.
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return

    invoices = frappe.get_all(
        "Sales Invoice",
        filters={
            "due_date": today(),
            "docstatus": 1,
            "outstanding_amount": [">", 0],
        },
        fields=["name"],
    )

    generated_count = 0
    for inv in invoices:
        doc = frappe.get_doc("Sales Invoice", inv.name)

        # Skip if both links already exist
        if doc.get("waafi_payment_link") and doc.get("edahab_payment_link"):
            continue

        try:
            _generate_links_for_invoice(doc)
            generated_count += 1
        except Exception as e:
            frappe.log_error(
                f"Failed to generate payment links for {inv.name}: {str(e)}\n"
                f"{frappe.get_traceback()}",
                "Payment Link Generation Error",
            )

    if generated_count:
        frappe.logger("mobile_payments").info(
            f"Generated payment links for {generated_count} invoices due today"
        )


@frappe.whitelist()
def generate_links_for_invoice(invoice_name):
    """
    Manually generate payment links for a Sales Invoice.
    Called from a button on the Sales Invoice form.

    Args:
        invoice_name: Sales Invoice name

    Returns:
        dict with waafi_payment_link and edahab_payment_link
    """
    doc = frappe.get_doc("Sales Invoice", invoice_name)

    if doc.docstatus != 1:
        frappe.throw(_("Invoice must be submitted to generate payment links"))

    if flt(doc.outstanding_amount) <= 0:
        frappe.throw(_("Invoice is already fully paid"))

    result = _generate_links_for_invoice(doc)
    return result


def _generate_links_for_invoice(doc):
    """
    Core function: generate both WaafiPay and Edahab HPP links for an invoice.
    Stores them in the custom fields on the Sales Invoice.

    Args:
        doc: Sales Invoice document

    Returns:
        dict with waafi_payment_link and edahab_payment_link
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return {"waafi_payment_link": "", "edahab_payment_link": ""}

    amount = flt(doc.outstanding_amount) or flt(doc.grand_total)
    currency = doc.currency or "USD"
    description = f"Payment for {doc.name}"
    result = {}

    # ── Generate WaafiPay HPP Link ──
    if settings.waafipay_enabled and not doc.get("waafi_payment_link"):
        try:
            from mobile_payments.api.waafipay import WaafiPayClient
            client = WaafiPayClient()
            waafi_result = client.create_hpp_session(
                amount=amount,
                invoice_id=doc.name,
                description=description,
                currency=currency,
            )

            if waafi_result.get("success") and waafi_result.get("hpp_url"):
                result["waafi_payment_link"] = waafi_result["hpp_url"]
                frappe.logger("mobile_payments").info(
                    f"WaafiPay HPP link generated for {doc.name}: {waafi_result['hpp_url']}"
                )
            else:
                frappe.log_error(
                    f"WaafiPay HPP link generation failed for {doc.name}: "
                    f"{waafi_result.get('message', 'Unknown error')}",
                    "WaafiPay HPP Link Error",
                )
                result["waafi_payment_link"] = ""
        except Exception as e:
            frappe.log_error(
                f"WaafiPay HPP link error for {doc.name}: {str(e)}\n"
                f"{frappe.get_traceback()}",
                "WaafiPay HPP Link Error",
            )
            result["waafi_payment_link"] = ""

    # ── Generate Edahab HPP Link ──
    if settings.edahab_enabled and not doc.get("edahab_payment_link"):
        try:
            from mobile_payments.api.edahab import EdahabClient
            client = EdahabClient()
            edahab_result = client.create_hpp_session(
                amount=amount,
                invoice_id=doc.name,
                description=description,
                currency=currency,
            )

            if edahab_result.get("success") and edahab_result.get("hpp_url"):
                result["edahab_payment_link"] = edahab_result["hpp_url"]
                frappe.logger("mobile_payments").info(
                    f"Edahab HPP link generated for {doc.name}: {edahab_result['hpp_url']}"
                )
            else:
                frappe.log_error(
                    f"Edahab HPP link generation failed for {doc.name}: "
                    f"{edahab_result.get('message', 'Unknown error')}",
                    "Edahab HPP Link Error",
                )
                result["edahab_payment_link"] = ""
        except Exception as e:
            frappe.log_error(
                f"Edahab HPP link error for {doc.name}: {str(e)}\n"
                f"{frappe.get_traceback()}",
                "Edahab HPP Link Error",
            )
            result["edahab_payment_link"] = ""

    # ── Save the links to the Sales Invoice ──
    if result:
        update_fields = {}
        if result.get("waafi_payment_link"):
            update_fields["waafi_payment_link"] = result["waafi_payment_link"]
        if result.get("edahab_payment_link"):
            update_fields["edahab_payment_link"] = result["edahab_payment_link"]

        if update_fields:
            frappe.db.set_value("Sales Invoice", doc.name, update_fields,
                                update_modified=False)
            frappe.db.commit()

    return result
