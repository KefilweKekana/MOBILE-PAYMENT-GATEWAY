"""
Persistent Payment Links with Auto-Refreshing HPP Sessions.

Generates a permanent payment URL (hosted on your ERPNext site) that:
 - Never expires as long as the invoice is unpaid
 - Auto-creates a fresh HPP session when visited (if the previous one expired)
 - Can be shared via SMS, Email, WhatsApp, or any channel
 - Supports both WaafiPay and Edahab providers

URL format:  https://yoursite.com/api/method/mobile_payments.api.payment_link.pay?token=<TOKEN>
"""
from __future__ import unicode_literals

import hashlib
import secrets

import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_seconds, cint


def _generate_payment_token():
    """Generate a secure, URL-safe payment link token."""
    return secrets.token_urlsafe(32)


def _get_invoice_details(invoice_id):
    """Get invoice details for payment link display."""
    if not frappe.db.exists("Sales Invoice", invoice_id):
        return None

    inv = frappe.get_doc("Sales Invoice", invoice_id)
    return {
        "name": inv.name,
        "customer": inv.customer,
        "customer_name": inv.customer_name,
        "grand_total": inv.grand_total,
        "outstanding_amount": inv.outstanding_amount,
        "currency": inv.currency,
        "status": inv.status,
        "docstatus": inv.docstatus,
    }


def _get_charge_amount(invoice_id):
    """Get the correct charge amount in the TRANSACTION (invoice) currency.

    ERPNext stores ``outstanding_amount`` in the **company** currency.  For
    foreign-currency invoices (e.g. SLSH invoice on a USD company) that gives
    the wrong number (the USD equivalent, not the SLSH total the customer owes).

    ``grand_total`` is always in the invoice's own currency, so we use that as
    the charge amount sent to the payment provider.

    We still use ``outstanding_amount > 0`` as the *gating* check ("is this
    invoice paid?"), but never as the amount to charge.
    """
    inv = frappe.db.get_value(
        "Sales Invoice", invoice_id,
        ["grand_total", "currency"],
        as_dict=True,
    )
    if not inv:
        return 0
    return float(inv.grand_total or 0)


@frappe.whitelist()
def create_payment_link(invoice_id, provider=None, method=None, expiry_hours=24,
                        currency=None):
    """
    Create a persistent payment link for a Sales Invoice.

    This generates a unique token-based URL that customers can visit to pay.
    When a customer visits the link, a fresh HPP session is created on the fly.

    Args:
        invoice_id: Sales Invoice name
        provider: Payment provider (WaafiPay or Edahab). Auto-selects if only one enabled.
        method: Payment method (ZAAD, SAHAL, EVCPlus, Edahab). Only for WaafiPay.
        expiry_hours: Hours until link expires (default: 24, 0 = never expires)
        currency: Currency code (USD or SLSH). Uses invoice currency if not specified.

    Returns:
        dict with payment_link URL and token
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    # Validate invoice
    invoice = _get_invoice_details(invoice_id)
    if not invoice:
        frappe.throw(_("Sales Invoice {0} not found").format(invoice_id))

    if invoice["docstatus"] != 1:
        frappe.throw(_("Sales Invoice must be submitted before creating a payment link"))

    if invoice["outstanding_amount"] <= 0:
        frappe.throw(_("Sales Invoice {0} has no outstanding amount").format(invoice_id))

    # Auto-select provider if not specified
    settings = frappe.get_single("Mobile Payment Settings")
    if not provider:
        if settings.waafipay_enabled and not settings.edahab_enabled:
            provider = "WaafiPay"
        elif settings.edahab_enabled and not settings.waafipay_enabled:
            provider = "Edahab"
        else:
            frappe.throw(_("Please specify a payment provider"))

    if provider == "Edahab":
        method = "Edahab"
    elif not method:
        method = "ZAAD"  # Default WaafiPay method

    # ── Invalidate any existing active tokens for this invoice + provider ──
    # This prevents duplicate Edahab IssueInvoice sessions that tie up the
    # agent's balance and cause "insufficient balance" errors.
    old_tokens = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "sales_invoice": invoice_id,
            "provider": provider,
            "payment_link_token": ["!=", ""],
            "status": ["in", ["Initiated", "Pending"]],
        },
        pluck="name",
    )
    for old_name in old_tokens:
        frappe.db.set_value(
            "Mobile Payment Transaction Log", old_name,
            "payment_link_token", "", update_modified=False,
        )
    if old_tokens:
        frappe.db.commit()

    # Generate token
    token = _generate_payment_token()

    # Calculate expiry (0 = never expires)
    expiry_hours = cint(expiry_hours) if expiry_hours is not None else 24
    link_expiry = None
    if expiry_hours > 0:
        from frappe.utils import add_to_date
        link_expiry = add_to_date(now_datetime(), hours=expiry_hours)

    # Get customer phone using smart provider-based routing
    customer_phone = ""
    customer_name = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
    if customer_name:
        try:
            from mobile_payments.api.pos import get_customer_phone_for_provider
            phone_data = get_customer_phone_for_provider(customer_name, provider=provider)
            customer_phone = phone_data.get("phone", "") if phone_data else ""
        except Exception:
            customer_phone = ""

    # Create a transaction log to track this payment link
    # Use grand_total (transaction currency) — NOT outstanding_amount (company currency)
    charge_amount = _get_charge_amount(invoice_id) or invoice["grand_total"]
    log = frappe.get_doc({
        "doctype": "Mobile Payment Transaction Log",
        "provider": provider,
        "payment_method": method if method in ("ZAAD", "SAHAL", "EVCPlus", "Edahab") else "",
        "flow_type": "Hosted Payment Page (HPP)",
        "status": "Initiated",
        "amount": charge_amount,
        "currency": currency or invoice["currency"],
        "phone_number": customer_phone or "",
        "sales_invoice": invoice_id,
        "initiated_at": now_datetime(),
        "payment_link_token": token,
        "payment_link_expiry": link_expiry,
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    # Build payment link URL
    base_url = settings.get_callback_url("") if hasattr(settings, "get_callback_url") else frappe.utils.get_url() + "/"
    payment_url = f"{base_url}api/method/mobile_payments.api.payment_link.pay?token={token}"

    # Update the Sales Invoice's payment link field
    try:
        link_field = "waafi_payment_link" if provider == "WaafiPay" else "edahab_payment_link"
        frappe.db.set_value("Sales Invoice", invoice_id, link_field, payment_url, update_modified=False)
        frappe.db.commit()
    except Exception:
        pass  # field may not exist yet

    return {
        "success": True,
        "payment_link": payment_url,
        "token": token,
        "transaction_log": log.name,
        "expires_at": str(link_expiry) if link_expiry else "Never",
        "invoice": invoice_id,
        "amount": charge_amount,
        "currency": invoice["currency"],
        "provider": provider,
    }


@frappe.whitelist(allow_guest=True)
def pay(token):
    """
    Handle a payment link visit from a customer.

    This is the endpoint customers hit when they click the payment link.
    It validates the token, checks the invoice, creates a fresh HPP session,
    and redirects the customer to the provider's hosted payment page.

    If the previous HPP session expired, a new one is automatically created.

    Args:
        token: The payment link token

    Returns:
        Redirects to the HPP payment page, or shows an error page
    """
    if not token:
        frappe.respond_as_web_page(
            _("Invalid Payment Link"),
            _("No payment token provided."),
            http_status_code=400,
            indicator_color="red",
        )
        return

    # Find the transaction log with this token
    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )

    if not log_name:
        frappe.respond_as_web_page(
            _("Invalid Payment Link"),
            _("This payment link is invalid or has been removed."),
            http_status_code=404,
            indicator_color="red",
        )
        return

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    # Check if link has expired
    if log.payment_link_expiry:
        expiry_dt = get_datetime(log.payment_link_expiry)
        if now_datetime() > expiry_dt:
            frappe.respond_as_web_page(
                _("Payment Link Expired"),
                _("This payment link has expired. Please request a new one from the merchant."),
                http_status_code=410,
                indicator_color="red",
            )
            return

    # Check if already paid
    if log.status == "Completed":
        frappe.respond_as_web_page(
            _("Already Paid"),
            _("This invoice has already been paid. Thank you!"),
            http_status_code=200,
            indicator_color="green",
        )
        return

    # Check if invoice still has outstanding amount
    invoice_id = log.sales_invoice
    if not invoice_id or not frappe.db.exists("Sales Invoice", invoice_id):
        frappe.respond_as_web_page(
            _("Invoice Not Found"),
            _("The associated invoice could not be found."),
            http_status_code=404,
            indicator_color="red",
        )
        return

    # Hard-block: check all three conditions that indicate the invoice is settled.
    # outstanding_amount alone can lag briefly after a Payment Entry is created,
    # so we also check the invoice status and whether a submitted Payment Entry
    # references this invoice — preventing duplicate collection.
    inv_data = frappe.db.get_value(
        "Sales Invoice",
        invoice_id,
        ["outstanding_amount", "grand_total", "currency", "status"],
        as_dict=True,
    )
    invoice_is_paid = (
        not inv_data
        or float(inv_data.outstanding_amount or 0) <= 0
        or (inv_data.status or "") in ("Paid", "Return", "Credit Note Issued")
        or frappe.db.exists(
            "Payment Entry Reference",
            {"reference_name": invoice_id, "docstatus": 1},
        )
    )

    if invoice_is_paid:
        # Invalidate the token so future visits immediately show this page
        # without re-querying the provider — prevents duplicate charge attempts.
        if log.status != "Completed":
            log.db_set("status", "Completed")
        log.db_set("payment_link_token", "")
        frappe.db.commit()
        frappe.respond_as_web_page(
            _("Already Paid"),
            _("This invoice has already been fully paid. Thank you!"),
            http_status_code=200,
            indicator_color="green",
        )
        return

    # Use grand_total (transaction currency) — NOT outstanding_amount (company currency)
    charge_amount = float(inv_data.grand_total or 0)

    # ── Try to reuse an existing HPP session for this invoice ──
    # Edahab rejects duplicate IssueInvoice calls while a previous one is
    # still active (StatusCode 7 "User declined").  We must redirect to
    # the existing HPP URL rather than trying to create a new session.
    #
    # Search broadly — ANY status, not just "Pending" — because the log may
    # have been marked Failed/Expired/Initiated while the provider-side
    # session is still live.  The provider portal will show its own expiry
    # message if the session is truly dead, which is far better UX than our
    # generic error page.
    provider = log.provider
    existing_hpp = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {
            "sales_invoice": invoice_id,
            "provider": provider,
            "hpp_url": ["!=", ""],
        },
        ["name", "hpp_url"],
        as_dict=True,
        order_by="modified desc",
    )
    if existing_hpp and existing_hpp.hpp_url:
        hpp_url = existing_hpp.hpp_url
        frappe.logger("mobile_payments").info(
            f"Reusing existing HPP session for {invoice_id}: {hpp_url[:60]}..."
        )
        # Track visit
        visit_count = cint(log.get("payment_link_visits") or 0) + 1
        log.db_set("payment_link_visits", visit_count)
        frappe.response["type"] = "redirect"
        frappe.response["location"] = hpp_url
        return

    # ── Mark any stale "Pending" sessions as expired before creating new ──
    # If we reach here, there's no Pending session with an hpp_url.
    # Clear orphaned Pending logs (no hpp_url or very old) so Edahab
    # doesn't see a conflicting active invoice on their side.
    stale_logs = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "sales_invoice": invoice_id,
            "provider": provider,
            "status": "Pending",
            "name": ["!=", log.name],
        },
        pluck="name",
    )
    for stale_name in stale_logs:
        frappe.db.set_value(
            "Mobile Payment Transaction Log", stale_name,
            {"status": "Expired", "payment_link_token": ""},
            update_modified=False,
        )
    if stale_logs:
        frappe.db.commit()

    # No reusable session — create a fresh HPP session
    amount = charge_amount
    description = f"Payment for {invoice_id}"

    # Edahab requires phone even for HPP — resolve from log or customer contact
    edahab_phone = ""
    if provider == "Edahab":
        edahab_phone = log.phone_number or ""
        if not edahab_phone or edahab_phone == "HPP":
            customer = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
            if customer:
                try:
                    from mobile_payments.api.pos import get_customer_phone_for_provider
                    phone_data = get_customer_phone_for_provider(customer, provider="Edahab")
                    edahab_phone = phone_data.get("phone", "") if phone_data else ""
                except Exception:
                    edahab_phone = ""

        if not edahab_phone or edahab_phone == "HPP":
            frappe.respond_as_web_page(
                _("Phone Number Required"),
                _("Edahab requires a phone number to process payment. "
                  "Please contact the merchant to update your phone number."),
                http_status_code=400,
                indicator_color="orange",
            )
            return

    try:
        if provider == "Edahab":
            from mobile_payments.api.edahab import EdahabClient
            client = EdahabClient()
            result = client.create_hpp_session(
                amount=amount,
                invoice_id=invoice_id,
                description=description,
                currency=log.currency or "USD",
                transaction_log=log.name,
                phone=edahab_phone,
            )
        else:
            from mobile_payments.api.waafipay import WaafiPayClient
            client = WaafiPayClient()
            result = client.create_hpp_session(
                amount=amount,
                invoice_id=invoice_id,
                description=description,
                currency=log.currency or "USD",
                transaction_log=log.name,
            )

        if result.get("success") and result.get("hpp_url"):
            hpp_url = result["hpp_url"]

            # Update the log with fresh HPP URL
            log.reload()
            log.hpp_url = hpp_url
            log.db_set("hpp_url", hpp_url)

            # NOTE: Do NOT overwrite the Sales Invoice payment link field here.
            # The field already contains the persistent token-based URL
            # (e.g. /api/method/...pay?token=XYZ) which auto-refreshes HPP
            # sessions on each visit. Overwriting it with the direct provider
            # HPP URL (e.g. pg.waafipay.net/...) would cause expiration.

            # Track visit count
            visit_count = cint(log.get("payment_link_visits") or 0) + 1
            log.db_set("payment_link_visits", visit_count)

            frappe.logger("mobile_payments").info(
                f"Payment link visited: {token[:8]}... | Invoice: {invoice_id} | "
                f"Visit #{visit_count} | Fresh HPP: {hpp_url[:50]}..."
            )

            # Redirect to the provider's HPP page
            frappe.response["type"] = "redirect"
            frappe.response["location"] = hpp_url
            return

        else:
            error_msg = result.get("message", "Failed to create payment session")

            # ── Fallback: if creation failed but the log itself has an old
            # hpp_url (e.g. from a previous visit), try redirecting there.
            # The provider portal will show its own expiry if it's truly dead,
            # which is better UX than our generic error page.
            fallback_url = log.get("hpp_url") or ""
            if not fallback_url:
                # Check any log for this invoice+provider that has an hpp_url
                fallback_row = frappe.db.get_value(
                    "Mobile Payment Transaction Log",
                    {
                        "sales_invoice": invoice_id,
                        "provider": provider,
                        "hpp_url": ["!=", ""],
                    },
                    "hpp_url",
                )
                fallback_url = fallback_row or ""

            if fallback_url:
                frappe.logger("mobile_payments").warning(
                    f"HPP creation failed ({error_msg}), falling back to "
                    f"existing URL: {fallback_url[:60]}..."
                )
                visit_count = cint(log.get("payment_link_visits") or 0) + 1
                log.db_set("payment_link_visits", visit_count)
                frappe.response["type"] = "redirect"
                frappe.response["location"] = fallback_url
                return

            frappe.respond_as_web_page(
                _("Payment Error"),
                _("Could not create a payment session: {0}<br><br>"
                  "Please try again or contact the merchant.").format(error_msg),
                http_status_code=500,
                indicator_color="red",
            )

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="Payment Link Error")
        frappe.respond_as_web_page(
            _("Payment Error"),
            _("An error occurred while processing your payment. Please try again later."),
            http_status_code=500,
            indicator_color="red",
        )


@frappe.whitelist()
def get_payment_link_status(token):
    """
    Check the status of a payment link.

    Args:
        token: Payment link token

    Returns:
        dict with link status, visits, HPP URL, etc.
    """
    frappe.has_permission("Sales Invoice", "read", throw=True)

    log = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        ["name", "status", "sales_invoice", "amount", "currency", "provider",
         "payment_link_expiry", "payment_link_visits", "hpp_url",
         "initiated_at", "completed_at"],
        as_dict=True,
    )

    if not log:
        return {"success": False, "message": "Payment link not found"}

    # Check expiry
    is_expired = False
    if log.payment_link_expiry:
        is_expired = now_datetime() > get_datetime(log.payment_link_expiry)

    # Check if invoice is paid
    is_paid = False
    outstanding = 0
    if log.sales_invoice:
        outstanding = frappe.db.get_value(
            "Sales Invoice", log.sales_invoice, "outstanding_amount"
        ) or 0
        is_paid = float(outstanding) <= 0

    return {
        "success": True,
        "status": log.status,
        "is_expired": is_expired,
        "is_paid": is_paid,
        "outstanding_amount": outstanding,
        "visits": cint(log.payment_link_visits),
        "expires_at": str(log.payment_link_expiry) if log.payment_link_expiry else "Never",
        "transaction_log": log.name,
    }


@frappe.whitelist()
def extend_payment_link(token, additional_hours=24):
    """
    Extend the expiry of a payment link.

    Args:
        token: Payment link token
        additional_hours: Hours to extend (default: 24)

    Returns:
        dict with new expiry time
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )

    if not log_name:
        frappe.throw(_("Payment link not found"))

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    from frappe.utils import add_to_date

    additional_hours = cint(additional_hours) or 24

    # Extend from current time (not from previous expiry)
    new_expiry = add_to_date(now_datetime(), hours=additional_hours)
    log.db_set("payment_link_expiry", new_expiry)

    return {
        "success": True,
        "new_expiry": str(new_expiry),
        "message": f"Payment link extended by {additional_hours} hours",
    }


@frappe.whitelist(allow_guest=True)
def refresh_hpp(token):
    """
    Refresh the HPP session for an existing payment link token.

    Called when a customer's Edahab/WaafiPay HPP session expires (typically ~10 min).
    Issues a brand-new HPP session from the provider and redirects the customer to it —
    WITHOUT changing the payment link URL or token.

    Args:
        token: The payment link token (same one from the original link)

    Returns:
        Redirects to a fresh HPP payment page
    """
    if not token:
        frappe.respond_as_web_page(
            _("Invalid Request"),
            _("No payment token provided."),
            http_status_code=400,
            indicator_color="red",
        )
        return

    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )

    if not log_name:
        frappe.respond_as_web_page(
            _("Invalid Payment Link"),
            _("This payment link is invalid or has been removed."),
            http_status_code=404,
            indicator_color="red",
        )
        return

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    # Check if link-level expiry has passed
    if log.payment_link_expiry:
        expiry_dt = get_datetime(log.payment_link_expiry)
        if now_datetime() > expiry_dt:
            frappe.respond_as_web_page(
                _("Payment Link Expired"),
                _("This payment link has expired. Please request a new one from the merchant."),
                http_status_code=410,
                indicator_color="red",
            )
            return

    if log.status == "Completed":
        frappe.respond_as_web_page(
            _("Already Paid"),
            _("This invoice has already been paid. Thank you!"),
            http_status_code=200,
            indicator_color="green",
        )
        return

    invoice_id = log.sales_invoice
    if not invoice_id or not frappe.db.exists("Sales Invoice", invoice_id):
        frappe.respond_as_web_page(
            _("Invoice Not Found"),
            _("The associated invoice could not be found."),
            http_status_code=404,
            indicator_color="red",
        )
        return

    inv_data = frappe.db.get_value(
        "Sales Invoice",
        invoice_id,
        ["outstanding_amount", "grand_total", "currency", "status"],
        as_dict=True,
    )
    invoice_is_paid = (
        not inv_data
        or float(inv_data.outstanding_amount or 0) <= 0
        or (inv_data.status or "") in ("Paid", "Return", "Credit Note Issued")
        or frappe.db.exists(
            "Payment Entry Reference",
            {"reference_name": invoice_id, "docstatus": 1},
        )
    )

    if invoice_is_paid:
        if log.status != "Completed":
            log.db_set("status", "Completed")
        log.db_set("payment_link_token", "")
        frappe.db.commit()
        frappe.respond_as_web_page(
            _("Already Paid"),
            _("This invoice has already been fully paid. Thank you!"),
            http_status_code=200,
            indicator_color="green",
        )
        return

    # Use grand_total (transaction currency) — NOT outstanding_amount (company currency)
    # Issue a fresh HPP session from the provider
    provider = log.provider
    amount = float(inv_data.grand_total or 0)
    description = f"Payment for {invoice_id}"

    # Edahab requires phone for HPP
    edahab_phone = ""
    if provider == "Edahab":
        edahab_phone = log.phone_number or ""
        if not edahab_phone or edahab_phone == "HPP":
            customer = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
            if customer:
                try:
                    from mobile_payments.api.pos import get_customer_phone
                    phone_data = get_customer_phone(customer)
                    edahab_phone = phone_data.get("phone", "") if phone_data else ""
                except Exception:
                    edahab_phone = ""

    try:
        if provider == "Edahab":
            from mobile_payments.api.edahab import EdahabClient
            client = EdahabClient()
            result = client.create_hpp_session(
                amount=amount,
                invoice_id=invoice_id,
                description=description,
                currency=log.currency or "USD",
                transaction_log=log.name,
                phone=edahab_phone,
            )
        else:
            from mobile_payments.api.waafipay import WaafiPayClient
            client = WaafiPayClient()
            result = client.create_hpp_session(
                amount=amount,
                invoice_id=invoice_id,
                description=description,
                currency=log.currency or "USD",
                transaction_log=log.name,
            )

        if result.get("success") and result.get("hpp_url"):
            hpp_url = result["hpp_url"]
            log.reload()
            log.db_set("hpp_url", hpp_url)

            frappe.logger("mobile_payments").info(
                f"HPP session refreshed via token {token[:8]}... | "
                f"Invoice: {invoice_id} | Fresh HPP: {hpp_url[:60]}..."
            )

            frappe.local.flags.redirect_location = hpp_url
            raise frappe.Redirect

        else:
            error_msg = result.get("message", "Failed to refresh payment session")
            frappe.respond_as_web_page(
                _("Payment Error"),
                _("Could not refresh the payment session: {0}<br><br>"
                  "Please try again or contact the merchant.").format(error_msg),
                http_status_code=500,
                indicator_color="red",
            )

    except frappe.Redirect:
        raise
    except Exception:
        frappe.log_error(message=frappe.get_traceback(), title="HPP Refresh Error")
        frappe.respond_as_web_page(
            _("Payment Error"),
            _("An error occurred while refreshing your payment session. Please try again."),
            http_status_code=500,
            indicator_color="red",
        )


def refresh_active_hpp_sessions():
    """
    Scheduled task: refresh HPP sessions for active payment links every 8 minutes.

    Edahab HPP sessions expire after ~10 minutes. This job proactively issues
    fresh HPP sessions for any pending payment-link transactions, so the next
    customer visit immediately gets a valid redirect without waiting.
    """
    from frappe.utils import add_to_date

    # Find pending payment-link transaction logs that still have a valid token
    active_logs = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "status": ["in", ["Initiated", "Pending"]],
            "payment_link_token": ["!=", ""],
            "flow_type": "Hosted Payment Page (HPP)",
        },
        fields=["name", "provider", "sales_invoice", "currency",
                "payment_link_expiry", "payment_link_token", "phone_number"],
    )

    refreshed = 0
    for entry in active_logs:
        try:
            # Skip if link-level expiry has already passed
            if entry.payment_link_expiry:
                if now_datetime() > get_datetime(entry.payment_link_expiry):
                    continue

            invoice_id = entry.sales_invoice
            if not invoice_id:
                continue

            inv_data = frappe.db.get_value(
                "Sales Invoice",
                invoice_id,
                ["outstanding_amount", "grand_total", "currency", "status"],
                as_dict=True,
            )
            invoice_is_paid = (
                not inv_data
                or float(inv_data.outstanding_amount or 0) <= 0
                or (inv_data.status or "") in ("Paid", "Return", "Credit Note Issued")
                or frappe.db.exists(
                    "Payment Entry Reference",
                    {"reference_name": invoice_id, "docstatus": 1},
                )
            )
            if invoice_is_paid:
                # Deactivate the token so the link becomes inert immediately
                frappe.db.set_value(
                    "Mobile Payment Transaction Log",
                    entry.name,
                    {"status": "Completed", "payment_link_token": ""},
                    update_modified=False,
                )
                continue

            # Use grand_total (transaction currency) — NOT outstanding_amount (company currency)
            provider = entry.provider
            amount = float(inv_data.grand_total or 0)
            description = f"Payment for {invoice_id}"

            if provider == "Edahab":
                from mobile_payments.api.edahab import EdahabClient
                client = EdahabClient()
                # Get phone from log or customer
                edahab_phone = entry.phone_number or ""
                if not edahab_phone or edahab_phone == "HPP":
                    customer = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
                    if customer:
                        try:
                            from mobile_payments.api.pos import get_customer_phone
                            phone_data = get_customer_phone(customer)
                            edahab_phone = phone_data.get("phone", "") if phone_data else ""
                        except Exception:
                            edahab_phone = ""
                result = client.create_hpp_session(
                    amount=amount,
                    invoice_id=invoice_id,
                    description=description,
                    currency=entry.currency or "USD",
                    transaction_log=entry.name,
                    phone=edahab_phone,
                )
            else:
                from mobile_payments.api.waafipay import WaafiPayClient
                client = WaafiPayClient()
                result = client.create_hpp_session(
                    amount=amount,
                    invoice_id=invoice_id,
                    description=description,
                    currency=entry.currency or "USD",
                    transaction_log=entry.name,
                )

            if result.get("success") and result.get("hpp_url"):
                frappe.db.set_value(
                    "Mobile Payment Transaction Log",
                    entry.name,
                    {"hpp_url": result["hpp_url"]},
                    update_modified=False,
                )
                refreshed += 1

        except Exception:
            frappe.log_error(
                message=frappe.get_traceback(),
                title=f"HPP Auto-Refresh Error ({entry.name})",
            )

    if refreshed:
        frappe.logger("mobile_payments").info(
            f"Auto-refreshed HPP sessions for {refreshed} active payment link(s)"
        )
    frappe.db.commit()


@frappe.whitelist()
def revoke_payment_link(token):
    """
    Revoke/cancel a payment link so it can no longer be used.

    Args:
        token: Payment link token

    Returns:
        dict confirming revocation
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )

    if not log_name:
        frappe.throw(_("Payment link not found"))

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    if log.status == "Completed":
        frappe.throw(_("Cannot revoke a completed payment"))

    log.update_status("Cancelled", error_message="Payment link revoked by user")
    # Clear the token so the link no longer works
    log.db_set("payment_link_token", "")

    return {
        "success": True,
        "message": "Payment link has been revoked",
    }
