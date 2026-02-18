"""
Payment Link Notification System
Sends payment links to customers via all Frappe notification channels:
  - Email (frappe.sendmail)
  - SMS (Frappe SMS Settings / SMS DocType)
  - System Notification (Notification Log)
  - Slack (Incoming Webhook)

Supports both direct HPP URLs and persistent payment links.
"""
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import fmt_money, get_url


# ──────────────────────────────────────────────
# Main Dispatchers
# ──────────────────────────────────────────────

@frappe.whitelist()
def send_payment_link_notification(payment_link, invoice_id, amount, currency="USD",
                                   provider="", transaction_log=""):
    """
    Send a persistent payment link to the customer via all enabled notification channels.
    This is the preferred method — the link auto-refreshes HPP sessions.

    Args:
        payment_link: The persistent payment link URL (hosted on your ERPNext site)
        invoice_id: Sales Invoice name
        amount: Payment amount
        currency: Currency code
        provider: Payment provider name (WaafiPay / Edahab)
        transaction_log: Transaction log name for reference

    Returns:
        dict with results per channel
    """
    return send_hpp_notification(
        hpp_url=payment_link,
        invoice_id=invoice_id,
        amount=amount,
        currency=currency,
        provider=provider,
        transaction_log=transaction_log,
    )


@frappe.whitelist()
def send_hpp_notification(hpp_url, invoice_id, amount, currency="USD",
                          provider="", transaction_log=""):
    """
    Send HPP payment link to the customer via all enabled notification channels.

    Args:
        hpp_url: The hosted payment page URL
        invoice_id: Sales Invoice name
        amount: Payment amount
        currency: Currency code
        provider: Payment provider name (WaafiPay / Edahab)
        transaction_log: Transaction log name for reference

    Returns:
        dict with results per channel
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    settings = frappe.get_single("Mobile Payment Settings")
    invoice = frappe.get_doc("Sales Invoice", invoice_id)
    customer = frappe.get_doc("Customer", invoice.customer)

    # Gather customer contact info
    customer_name = invoice.customer_name or customer.customer_name
    customer_email = _get_customer_email(invoice, customer)
    customer_phone = _get_customer_phone(invoice, customer)

    # Template context
    context = {
        "customer_name": customer_name,
        "invoice_id": invoice_id,
        "amount": fmt_money(amount, currency=currency),
        "currency": currency,
        "raw_amount": amount,
        "provider": provider,
        "hpp_url": hpp_url,
        "company": invoice.company,
        "transaction_log": transaction_log,
        "site_url": get_url(),
    }

    results = {
        "email": {"sent": False, "message": ""},
        "sms": {"sent": False, "message": ""},
        "system": {"sent": False, "message": ""},
        "slack": {"sent": False, "message": ""},
    }

    # ── Email ──
    if settings.get("hpp_notify_email"):
        results["email"] = _send_email_notification(settings, customer_email, context)

    # ── SMS ──
    if settings.get("hpp_notify_sms"):
        results["sms"] = _send_sms_notification(settings, customer_phone, context)

    # ── System Notification (Notification Log) ──
    if settings.get("hpp_notify_system"):
        results["system"] = _send_system_notification(settings, invoice, context)

    # ── Slack ──
    if settings.get("hpp_notify_slack"):
        results["slack"] = _send_slack_notification(settings, context)

    # Summary
    sent_channels = [ch for ch, r in results.items() if r["sent"]]
    failed_channels = [ch for ch, r in results.items()
                       if not r["sent"] and settings.get(f"hpp_notify_{ch}")]

    return {
        "success": len(sent_channels) > 0,
        "sent_channels": sent_channels,
        "failed_channels": failed_channels,
        "results": results,
        "message": _("Sent via: {0}").format(", ".join(sent_channels)) if sent_channels
                   else _("No notifications were sent"),
    }


# ──────────────────────────────────────────────
# Channel: Email
# ──────────────────────────────────────────────

def _send_email_notification(settings, email, context):
    """Send HPP link via email using frappe.sendmail."""
    if not email:
        return {"sent": False, "message": _("No email address found for customer")}

    try:
        subject = _render_template(
            settings.get("hpp_email_subject") or _default_email_subject(),
            context
        )
        body = _render_template(
            settings.get("hpp_email_body") or _default_email_body(),
            context
        )

        frappe.sendmail(
            recipients=[email],
            subject=subject,
            message=body,
            reference_doctype="Sales Invoice",
            reference_name=context["invoice_id"],
            now=True,
        )

        frappe.logger("mobile_payments").info(
            f"HPP link emailed to {email} for {context['invoice_id']}"
        )
        return {"sent": True, "message": _("Email sent to {0}").format(email)}

    except Exception as e:
        frappe.log_error(
            f"HPP email notification failed: {str(e)}",
            "HPP Email Notification Error"
        )
        return {"sent": False, "message": str(e)}


# ──────────────────────────────────────────────
# Channel: SMS
# ──────────────────────────────────────────────

def _send_sms_notification(settings, phone, context):
    """Send HPP link via SMS using Frappe's SMS Settings."""
    if not phone:
        return {"sent": False, "message": _("No phone number found for customer")}

    try:
        # Check if Frappe SMS Settings are configured
        sms_settings = frappe.db.get_singles_dict("SMS Settings")
        if not sms_settings.get("sms_gateway_url"):
            return {"sent": False, "message": _("SMS Settings not configured in Frappe")}

        message = _render_template(
            settings.get("hpp_sms_template") or _default_sms_template(),
            context
        )

        from frappe.core.doctype.sms_settings.sms_settings import send_sms
        send_sms([phone], message, sender_name=context.get("company", ""))

        frappe.logger("mobile_payments").info(
            f"HPP link sent via SMS to {phone} for {context['invoice_id']}"
        )
        return {"sent": True, "message": _("SMS sent to {0}").format(phone)}

    except Exception as e:
        frappe.log_error(
            f"HPP SMS notification failed: {str(e)}",
            "HPP SMS Notification Error"
        )
        return {"sent": False, "message": str(e)}


# ──────────────────────────────────────────────
# Channel: System Notification (Notification Log)
# ──────────────────────────────────────────────

def _send_system_notification(settings, invoice, context):
    """Create a Frappe Notification Log (in-app notification) for relevant users."""
    try:
        # Notify the invoice owner and all users with Accounts Manager role
        recipients = set()

        # Invoice owner
        if invoice.owner:
            recipients.add(invoice.owner)

        # Current user
        if frappe.session.user and frappe.session.user != "Guest":
            recipients.add(frappe.session.user)

        # Accounts Managers
        accounts_managers = frappe.get_all(
            "Has Role",
            filters={"role": "Accounts Manager", "parenttype": "User"},
            fields=["parent"],
        )
        for row in accounts_managers:
            user = row.parent
            if user and frappe.db.exists("User", user) and frappe.db.get_value("User", user, "enabled"):
                recipients.add(user)

        # Remove Guest and Administrator if present
        recipients.discard("Guest")

        if not recipients:
            return {"sent": False, "message": _("No recipients for system notification")}

        subject = _("Payment Link Sent: {0}").format(context["invoice_id"])
        message = _(
            "A payment link ({provider}) for <b>{amount}</b> has been generated for "
            "invoice <a href='/app/sales-invoice/{invoice_id}'>{invoice_id}</a>."
            "<br><br>Payment URL: <a href='{hpp_url}'>{hpp_url}</a>"
        ).format(**context)

        # Create Notification Log entries
        for user in recipients:
            try:
                notification = frappe.get_doc({
                    "doctype": "Notification Log",
                    "for_user": user,
                    "from_user": frappe.session.user,
                    "type": "Alert",
                    "document_type": "Sales Invoice",
                    "document_name": context["invoice_id"],
                    "subject": subject,
                    "email_content": message,
                })
                notification.insert(ignore_permissions=True)
            except Exception:
                pass  # Skip individual failures

        frappe.logger("mobile_payments").info(
            f"System notification created for {len(recipients)} users | {context['invoice_id']}"
        )
        return {
            "sent": True,
            "message": _("System notification sent to {0} users").format(len(recipients)),
        }

    except Exception as e:
        frappe.log_error(
            f"HPP system notification failed: {str(e)}",
            "HPP System Notification Error"
        )
        return {"sent": False, "message": str(e)}


# ──────────────────────────────────────────────
# Channel: Slack
# ──────────────────────────────────────────────

def _send_slack_notification(settings, context):
    """Send HPP link to a Slack channel via incoming webhook."""
    webhook_url = settings.get("hpp_slack_webhook_url")
    if not webhook_url:
        return {"sent": False, "message": _("Slack webhook URL not configured")}

    try:
        import requests

        payload = {
            "text": (
                f":money_with_wings: *Payment Link Generated*\n"
                f"*Invoice:* {context['invoice_id']}\n"
                f"*Customer:* {context['customer_name']}\n"
                f"*Amount:* {context['amount']}\n"
                f"*Provider:* {context['provider']}\n"
                f"*Payment Link:* {context['hpp_url']}"
            ),
        }

        resp = requests.post(webhook_url, json=payload, timeout=10)

        if resp.status_code == 200 and resp.text == "ok":
            frappe.logger("mobile_payments").info(
                f"HPP link posted to Slack for {context['invoice_id']}"
            )
            return {"sent": True, "message": _("Slack notification sent")}
        else:
            return {"sent": False, "message": f"Slack returned {resp.status_code}: {resp.text}"}

    except Exception as e:
        frappe.log_error(
            f"HPP Slack notification failed: {str(e)}",
            "HPP Slack Notification Error"
        )
        return {"sent": False, "message": str(e)}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _get_customer_email(invoice, customer):
    """Get customer email from invoice or customer master."""
    # Try contact_email on invoice first
    if invoice.get("contact_email"):
        return invoice.contact_email

    # Try customer's primary email
    if customer.get("email_id"):
        return customer.email_id

    # Try linked contact
    contact = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Customer", "link_name": customer.name, "parenttype": "Contact"},
        "parent",
    )
    if contact:
        email = frappe.db.get_value("Contact", contact, "email_id")
        if email:
            return email

    return None


def _get_customer_phone(invoice, customer):
    """Get customer phone from invoice or customer master."""
    # Try contact_mobile on invoice
    if invoice.get("contact_mobile"):
        return invoice.contact_mobile

    # Try contact_phone on invoice
    if invoice.get("contact_phone"):
        return invoice.contact_phone

    # Try customer's mobile
    if customer.get("mobile_no"):
        return customer.mobile_no

    # Try linked contact
    contact = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Customer", "link_name": customer.name, "parenttype": "Contact"},
        "parent",
    )
    if contact:
        phone = frappe.db.get_value("Contact", contact, "mobile_no")
        if phone:
            return phone
        phone = frappe.db.get_value("Contact", contact, "phone")
        if phone:
            return phone

    return None


def _render_template(template, context):
    """Render a Jinja-style template string with context."""
    try:
        return frappe.render_template(template, context)
    except Exception:
        # Fallback: simple string format
        try:
            return template.format(**context)
        except Exception:
            return template


# ──────────────────────────────────────────────
# Default Templates
# ──────────────────────────────────────────────

def _default_email_subject():
    return _("Payment Link for Invoice {{ invoice_id }}")


def _default_email_body():
    return """
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
    <h2 style="color: #2c3e50;">Payment Request</h2>
    <p>Dear {{ customer_name }},</p>
    <p>A payment of <strong>{{ amount }}</strong> is due for invoice <strong>{{ invoice_id }}</strong>.</p>
    <p>Please click the button below to complete your payment securely via {{ provider }}:</p>
    <div style="text-align: center; margin: 30px 0;">
        <a href="{{ hpp_url }}"
           style="background-color: #2ecc71; color: white; padding: 14px 30px;
                  text-decoration: none; border-radius: 5px; font-size: 16px;
                  display: inline-block;">
            Pay Now — {{ amount }}
        </a>
    </div>
    <p style="color: #7f8c8d; font-size: 12px;">
        If the button doesn't work, copy and paste this link into your browser:<br>
        <a href="{{ hpp_url }}">{{ hpp_url }}</a>
    </p>
    <hr style="border: none; border-top: 1px solid #ecf0f1; margin: 20px 0;">
    <p style="color: #95a5a6; font-size: 11px;">
        This is an automated payment request from {{ company }}.
        If you did not expect this email, please ignore it.
    </p>
</div>
"""


def _default_sms_template():
    return _(
        "Payment of {{ amount }} due for invoice {{ invoice_id }}. "
        "Pay securely here: {{ hpp_url }}"
    )
