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
                message=(
                    f"Failed to generate payment links for {inv.name}: {str(e)}\n"
                    f"{frappe.get_traceback()}"
                ),
                title="Payment Link Generation Error",
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
    Core function: generate both WaafiPay and Edahab persistent payment links
    for an invoice.  Uses ``create_payment_link()`` which produces a
    token-based URL that auto-refreshes HPP sessions on each visit —
    so the link never expires while the invoice is unpaid.

    Args:
        doc: Sales Invoice document

    Returns:
        dict with waafi_payment_link and edahab_payment_link
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return {"waafi_payment_link": "", "edahab_payment_link": ""}

    from mobile_payments.api.payment_link import create_payment_link

    # Use grand_total (transaction currency) not outstanding_amount (company currency).
    currency = doc.currency or "USD"
    result = {}

    # ── Generate WaafiPay persistent payment link ──
    if settings.waafipay_enabled and not doc.get("waafi_payment_link"):
        try:
            # Validate credentials first
            creds = settings.get_waafipay_credentials()
            missing = []
            if not creds.get("merchant_uid"):
                missing.append("Merchant UID")
            if not creds.get("api_key"):
                missing.append("API Key")
            if not creds.get("store_id"):
                missing.append("Store ID")
            if not creds.get("hpp_key"):
                missing.append("HPP Key")

            frappe.logger("mobile_payments").info(
                f"WaafiPay payment link generation attempt for {doc.name} "
                f"| Currency: {currency} | Missing: {missing or 'None'}"
            )

            if missing:
                err = (
                    f"WaafiPay payment link generation failed for {doc.name}: "
                    f"Missing credentials: {', '.join(missing)}. "
                    f"Please configure them in Mobile Payment Settings."
                )
                frappe.log_error(message=err, title="Payment Link - Missing Credentials")
                frappe.msgprint(
                    _(
                        "Cannot generate WaafiPay payment link: {0} is not configured. "
                        "Please update Mobile Payment Settings."
                    ).format(", ".join(missing)),
                    title=_("Missing WaafiPay Credentials"),
                    indicator="red",
                )
                result["waafi_payment_link"] = ""
            else:
                link_result = create_payment_link(
                    invoice_id=doc.name,
                    provider="WaafiPay",
                    method="ZAAD",
                    expiry_hours=0,  # never expires
                    currency=currency,
                )
                if link_result.get("success") and link_result.get("payment_link"):
                    result["waafi_payment_link"] = link_result["payment_link"]
                    frappe.logger("mobile_payments").info(
                        f"WaafiPay persistent link generated for {doc.name}: "
                        f"{link_result['payment_link']}"
                    )
                else:
                    frappe.log_error(
                        message=(
                            f"WaafiPay payment link generation failed for {doc.name}: "
                            f"{link_result.get('message', 'Unknown error')}"
                        ),
                        title="WaafiPay Payment Link Error",
                    )
                    result["waafi_payment_link"] = ""
        except Exception as e:
            frappe.log_error(
                message=(
                    f"WaafiPay payment link error for {doc.name}: {str(e)}\n"
                    f"{frappe.get_traceback()}"
                ),
                title="WaafiPay Payment Link Error",
            )
            result["waafi_payment_link"] = ""

    # ── Generate Edahab persistent payment link ──
    if settings.edahab_enabled and not doc.get("edahab_payment_link"):
        try:
            edahab_creds = settings.get_edahab_credentials()
            missing_ed = []
            if not edahab_creds.get("api_key"):
                missing_ed.append("API Key")
            if not edahab_creds.get("api_secret"):
                missing_ed.append("API Secret")

            frappe.logger("mobile_payments").info(
                f"Edahab payment link generation attempt for {doc.name} "
                f"| Currency: {currency} | Missing: {missing_ed or 'None'}"
            )

            if missing_ed:
                err = (
                    f"Edahab payment link generation failed for {doc.name}: "
                    f"Missing credentials: {', '.join(missing_ed)}. "
                    f"Please configure them in Mobile Payment Settings."
                )
                frappe.log_error(message=err, title="Payment Link - Missing Credentials")
                frappe.msgprint(
                    _(
                        "Cannot generate Edahab payment link: {0} is not configured. "
                        "Please update Mobile Payment Settings."
                    ).format(", ".join(missing_ed)),
                    title=_("Missing Edahab Credentials"),
                    indicator="red",
                )
                result["edahab_payment_link"] = ""
            else:
                link_result = create_payment_link(
                    invoice_id=doc.name,
                    provider="Edahab",
                    method="Edahab",
                    expiry_hours=0,  # never expires
                    currency=currency,
                )
                if link_result.get("success") and link_result.get("payment_link"):
                    result["edahab_payment_link"] = link_result["payment_link"]
                    frappe.logger("mobile_payments").info(
                        f"Edahab persistent link generated for {doc.name}: "
                        f"{link_result['payment_link']}"
                    )
                else:
                    frappe.log_error(
                        message=(
                            f"Edahab payment link generation failed for {doc.name}: "
                            f"{link_result.get('message', 'Unknown error')}"
                        ),
                        title="Edahab Payment Link Error",
                    )
                    result["edahab_payment_link"] = ""
        except Exception as e:
            frappe.log_error(
                message=(
                    f"Edahab payment link error for {doc.name}: {str(e)}\n"
                    f"{frappe.get_traceback()}"
                ),
                title="Edahab Payment Link Error",
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
