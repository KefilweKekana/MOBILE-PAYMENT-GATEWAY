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
            "payment_link_token", None, update_modified=False,
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
        log.db_set("payment_link_token", None)
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

    # ── Always create a fresh HPP session ──
    # Do NOT reuse old HPP URLs from logs — they expire on the provider
    # side (Edahab shows "Invalid Invoice") and we can't reliably tell
    # when.  Instead, always call the provider:
    #   - StatusCode 0 → new session created, redirect
    #   - StatusCode 7 → Edahab returns the active InvoiceId, we use it
    #   - Any other error → show error page
    #
    # Clear old hpp_url values so they never interfere.
    provider = log.provider
    old_hpp_logs = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "sales_invoice": invoice_id,
            "provider": provider,
            "hpp_url": ["!=", ""],
            "name": ["!=", log.name],
        },
        pluck="name",
    )
    for old_name in old_hpp_logs:
        frappe.db.set_value(
            "Mobile Payment Transaction Log", old_name,
            {"hpp_url": "", "status": "Expired", "payment_link_token": None},
            update_modified=False,
        )
    if old_hpp_logs:
        frappe.db.commit()

    # ── Edahab: self-hosted payment page ──
    # Edahab does NOT have a customer-facing payment portal.  Instead of
    # redirecting to a non-existent HPP page, we serve our own page where
    # the customer enters their Edahab phone number and we trigger a USSD
    # Push to their phone.
    if provider == "Edahab":
        prefill_phone = log.phone_number or ""
        if not prefill_phone or prefill_phone == "HPP":
            customer = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
            if customer:
                try:
                    from mobile_payments.api.pos import get_customer_phone_for_provider
                    phone_data = get_customer_phone_for_provider(customer, provider="Edahab")
                    prefill_phone = phone_data.get("phone", "") if phone_data else ""
                except Exception:
                    prefill_phone = ""
            if prefill_phone == "HPP":
                prefill_phone = ""

        # Track visit
        visit_count = cint(log.get("payment_link_visits") or 0) + 1
        log.db_set("payment_link_visits", visit_count)

        frappe.logger("mobile_payments").info(
            f"Edahab payment link visited: {token[:8]}... | Invoice: {invoice_id} | "
            f"Visit #{visit_count} | Serving self-hosted payment page"
        )

        _render_edahab_payment_page(
            token, invoice_id, charge_amount,
            inv_data.currency or "USD", prefill_phone,
        )
        return

    # ── WaafiPay: create HPP session and redirect ──
    amount = charge_amount
    description = f"Payment for {invoice_id}"

    try:
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


def _render_edahab_payment_page(token, invoice_id, amount, currency, prefill_phone=""):
    """Render a self-hosted Edahab payment page.

    Edahab does not have a customer-facing payment portal like WaafiPay.
    Instead of redirecting to a non-existent HPP page, we serve our own
    page where the customer enters their Edahab phone number and we
    trigger a USSD Push to their phone.

    The page handles the full payment flow:
      1. Customer enters their Edahab phone number
      2. Clicks "Pay with Edahab"
      3. We call Purchase API → USSD Push sent to their phone
      4. Page polls for payment confirmation
      5. Shows success or failure
    """
    import html as html_mod

    formatted_amount = f"{float(amount):,.2f}"
    safe_invoice = html_mod.escape(str(invoice_id))
    safe_phone = html_mod.escape(str(prefill_phone or ""))
    safe_token = html_mod.escape(str(token))
    safe_currency = html_mod.escape(str(currency))

    page_html = f"""
    <style>
      .ep-page {{ max-width: 420px; margin: -15px auto 40px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
      .ep-card {{ background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); overflow: hidden; }}
      .ep-header {{ background: linear-gradient(135deg, #10b981, #059669); padding: 36px 24px 30px; text-align: center; color: #fff; position: relative; overflow: hidden; }}
      .ep-header::before {{ content: ''; position: absolute; top: -50%; left: -50%; width: 300%; height: 200%; background: radial-gradient(circle, rgba(255,255,255,0.1) 1px, transparent 1px); background-size: 20px 20px; opacity: 0.3; }}
      .ep-header .ep-inner {{ position: relative; z-index: 2; }}
      .ep-header .ep-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; opacity: 0.85; margin: 0 0 6px; }}
      .ep-header .ep-amount {{ font-size: 34px; font-weight: 700; margin: 0 0 8px; line-height: 1; }}
      .ep-header .ep-invoice {{ font-size: 12px; opacity: 0.8; margin: 0; }}
      .ep-body {{ padding: 28px 24px 20px; }}
      .ep-field {{ margin-bottom: 20px; }}
      .ep-field label {{ display: block; font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }}
      .ep-field input {{ width: 100%; border: 2px solid #e5e7eb; border-radius: 10px; padding: 12px 16px; font-size: 15px; background: #fafafa; transition: border-color 0.2s, box-shadow 0.2s; color: #1f2937; box-sizing: border-box; outline: none; }}
      .ep-field input:focus {{ border-color: #10b981; background: #fff; box-shadow: 0 0 0 3px rgba(16,185,129,0.1); }}
      .ep-hint {{ font-size: 12px; color: #6b7280; margin: 6px 0 0; }}
      .ep-btn {{ background: linear-gradient(135deg, #10b981 0%, #059669 100%); border: none; font-weight: 600; font-size: 16px; color: #fff; padding: 14px 32px; border-radius: 12px; width: 100%; cursor: pointer; box-shadow: 0 4px 6px -1px rgba(16,185,129,0.2); transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 8px; }}
      .ep-btn:hover {{ transform: translateY(-1px); box-shadow: 0 10px 15px -3px rgba(16,185,129,0.3); }}
      .ep-btn:active {{ transform: translateY(0); }}
      .ep-btn:disabled {{ opacity: 0.6; cursor: not-allowed; transform: none; }}
      .ep-btn-outline {{ background: transparent; border: 2px solid #10b981; color: #10b981; box-shadow: none; }}
      .ep-btn-outline:hover {{ background: #f0fdf4; transform: translateY(-1px); }}
      .ep-secure {{ display: flex; align-items: center; justify-content: center; gap: 6px; color: #9ca3af; font-size: 12px; margin-top: 16px; }}
      .ep-secure i {{ color: #10b981; }}
      .ep-center {{ text-align: center; padding: 20px 0; }}
      .ep-spinner {{ border: 4px solid #e5e7eb; border-top: 4px solid #10b981; border-radius: 50%; width: 48px; height: 48px; animation: ep-spin 1s linear infinite; margin: 0 auto 20px; }}
      @keyframes ep-spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
      .ep-icon {{ width: 64px; height: 64px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; margin-bottom: 16px; font-size: 28px; }}
      .ep-icon-success {{ background: #dcfce7; color: #16a34a; }}
      .ep-icon-error {{ background: #fef2f2; color: #dc2626; }}
      .ep-phone-prompt {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 16px; margin: 16px 0; }}
      .ep-phone-prompt p {{ margin: 0; font-size: 14px; color: #374151; }}
      .ep-phone-prompt .ep-prompt-icon {{ color: #10b981; margin-right: 8px; }}
      .ep-status {{ color: #6b7280; font-size: 13px; margin-top: 12px; }}
      .ep-footer {{ text-align: center; padding: 16px 24px; border-top: 1px solid #f3f4f6; }}
      .ep-footer a {{ color: #10b981; text-decoration: none; font-size: 13px; }}

      /* Countdown ring — Capitec style */
      .ep-countdown-ring {{ position: relative; width: 160px; height: 160px; margin: 0 auto 24px; }}
      .ep-countdown-ring svg {{ width: 100%; height: 100%; transform: rotate(-90deg); }}
      .ep-ring-bg {{ fill: none; stroke: #f3f4f6; stroke-width: 8; }}
      .ep-ring-fg {{ fill: none; stroke: #10b981; stroke-width: 8; stroke-linecap: round; stroke-dasharray: 408.41; stroke-dashoffset: 0; transition: stroke-dashoffset 1s linear, stroke 0.5s ease; }}
      .ep-ring-fg.ep-ring-warn {{ stroke: #f59e0b; }}
      .ep-ring-fg.ep-ring-danger {{ stroke: #ef4444; }}
      .ep-countdown-text {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; }}
      .ep-countdown-time {{ font-size: 36px; font-weight: 700; color: #1f2937; line-height: 1; letter-spacing: 1px; }}
      .ep-countdown-label {{ font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
    </style>

    <div class="ep-page">
      <div class="ep-card">
        <div class="ep-header">
          <div class="ep-inner">
            <p class="ep-label">Total Amount</p>
            <div class="ep-amount">{formatted_amount} {safe_currency}</div>
            <p class="ep-invoice">Invoice #{safe_invoice}</p>
          </div>
        </div>

        <div class="ep-body">
          <!-- ① Form: phone input + pay button -->
          <div id="ep-form-section">
            <div class="ep-field">
              <label for="ep-phone">Edahab Phone Number</label>
              <input type="tel" id="ep-phone" value="{safe_phone}"
                     placeholder="e.g. 652345678" autocomplete="tel">
              <p class="ep-hint">Enter the Edahab wallet number to charge (digits only, no +252)</p>
            </div>
            <button type="button" id="ep-pay-btn" class="ep-btn"
                    onclick="edahabPay.initiate()">
              Pay with Edahab &rarr;
            </button>
            <div class="ep-secure">
              <i class="fa fa-lock"></i> Secure payment via Edahab
            </div>
          </div>

          <!-- ② Processing: waiting for USSD confirmation -->
          <div id="ep-processing-section" class="ep-center" style="display:none">
            <div class="ep-countdown-ring">
              <svg viewBox="0 0 140 140">
                <circle class="ep-ring-bg" cx="70" cy="70" r="65" />
                <circle class="ep-ring-fg" id="ep-ring" cx="70" cy="70" r="65" />
              </svg>
              <div class="ep-countdown-text">
                <div class="ep-countdown-time" id="ep-countdown">2:00</div>
                <div class="ep-countdown-label">remaining</div>
              </div>
            </div>
            <h3 style="margin:0 0 8px; color:#1f2937; font-size:18px;">Confirm payment on your phone</h3>
            <p style="color:#6b7280; font-size:14px; margin:0 0 8px;">Check your phone for the payment prompt</p>
            <p class="ep-status" id="ep-poll-status" style="color:#9ca3af; font-size:12px;">Waiting for confirmation&hellip;</p>
          </div>

          <!-- ③ Success -->
          <div id="ep-success-section" class="ep-center" style="display:none">
            <div class="ep-icon ep-icon-success">&#10004;</div>
            <h3 style="margin:0 0 8px; color:#16a34a;">Payment Successful!</h3>
            <p style="color:#374151;">Your payment of <strong>{formatted_amount} {safe_currency}</strong> has been confirmed.</p>
            <p style="color:#6b7280; font-size:13px;">Thank you for your payment!</p>
          </div>

          <!-- ④ Error -->
          <div id="ep-error-section" class="ep-center" style="display:none">
            <div class="ep-icon ep-icon-error">&#10008;</div>
            <h3 style="margin:0 0 8px; color:#dc2626;">Payment Failed</h3>
            <p id="ep-error-msg" style="color:#374151;"></p>
            <button type="button" class="ep-btn ep-btn-outline" style="margin-top:16px"
                    onclick="edahabPay.reset()">
              Try Again
            </button>
          </div>
        </div>


      </div>
    </div>

    <script>
    var edahabPay = {{
      token: '{safe_token}',
      pollTimer: null,
      pollCount: 0,
      transactionLog: null,

      getCsrf: function() {{
        if (typeof frappe !== 'undefined' && frappe.csrf_token) return frappe.csrf_token;
        var m = document.cookie.match(/csrf_token=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : 'None';
      }},

      api: function(method, args) {{
        return fetch('/api/method/' + method, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Frappe-CSRF-Token': this.getCsrf()
          }},
          body: JSON.stringify(args)
        }}).then(function(r) {{
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }}).then(function(d) {{ return d.message; }});
      }},

      showSection: function(id) {{
        ['ep-form-section', 'ep-processing-section', 'ep-success-section', 'ep-error-section'].forEach(function(s) {{
          document.getElementById(s).style.display = (s === id) ? 'block' : 'none';
        }});
      }},

      reset: function() {{
        if (this.pollTimer) clearInterval(this.pollTimer);
        if (this.countdownTimer) clearInterval(this.countdownTimer);
        var btn = document.getElementById('ep-pay-btn');
        btn.disabled = false;
        btn.textContent = 'Pay with Edahab →';
        this.showSection('ep-form-section');
      }},

      initiate: function() {{
        var phone = (document.getElementById('ep-phone').value || '').trim().replace(/[\\s\\-\\+]/g, '');
        if (!phone || !/^\\d{{7,15}}$/.test(phone)) {{
          alert('Please enter a valid Edahab phone number (7–15 digits, no spaces or dashes).');
          return;
        }}

        var btn = document.getElementById('ep-pay-btn');
        btn.disabled = true;
        btn.innerHTML = '<span class="ep-spinner" style="width:20px;height:20px;border-width:2px;margin:0"></span> Processing...';

        // Show countdown IMMEDIATELY — don't wait for API response
        this.showSection('ep-processing-section');
        this.startCountdown();

        var self = this;
        this.api('mobile_payments.api.payment_link.edahab_initiate_payment', {{
          token: this.token,
          phone: phone
        }}).then(function(result) {{
          if (!result) {{
            self.stopTimers();
            self.showError('No response from server. Please try again.');
            return;
          }}
          if (result.success) {{
            self.stopTimers();
            self.showSection('ep-success-section');
          }} else if (result.pending) {{
            self.transactionLog = result.transaction_log;
            // Countdown already running — just start polling
            self.startPolling();
          }} else {{
            self.stopTimers();
            self.showError(result.message || 'Payment failed. Please try again.');
          }}
        }}).catch(function(err) {{
          console.error('Edahab payment error:', err);
          self.stopTimers();
          self.showError('An error occurred. Please try again.');
        }});
      }},

      stopTimers: function() {{
        if (this.pollTimer) {{ clearInterval(this.pollTimer); this.pollTimer = null; }}
        if (this.countdownTimer) {{ clearInterval(this.countdownTimer); this.countdownTimer = null; }}
      }},

      startCountdown: function() {{
        var self = this;
        this.secondsLeft = 120;
        var totalSeconds = 120;
        var circumference = 2 * Math.PI * 65; // r=65 → 408.41
        if (this.countdownTimer) clearInterval(this.countdownTimer);

        var ring = document.getElementById('ep-ring');
        var countdownEl = document.getElementById('ep-countdown');

        // Reset ring to full
        if (ring) {{
          ring.style.strokeDashoffset = '0';
          ring.classList.remove('ep-ring-warn', 'ep-ring-danger');
        }}
        if (countdownEl) countdownEl.textContent = '2:00';

        this.countdownTimer = setInterval(function() {{
          self.secondsLeft--;
          if (self.secondsLeft <= 0) {{
            self.stopTimers();
            self.reset();
            return;
          }}
          var mins = Math.floor(self.secondsLeft / 60);
          var secs = self.secondsLeft % 60;
          if (countdownEl) countdownEl.textContent = mins + ':' + (secs < 10 ? '0' : '') + secs;
          var progress = 1 - (self.secondsLeft / totalSeconds);
          if (ring) {{
            ring.style.strokeDashoffset = (progress * circumference).toFixed(1);
            ring.classList.remove('ep-ring-warn', 'ep-ring-danger');
            if (self.secondsLeft <= 30) ring.classList.add('ep-ring-danger');
            else if (self.secondsLeft <= 60) ring.classList.add('ep-ring-warn');
          }}
        }}, 1000);
      }},

      startPolling: function() {{
        var self = this;
        this.pollCount = 0;
        if (this.pollTimer) clearInterval(this.pollTimer);

        this.pollTimer = setInterval(function() {{
          self.pollCount++;
          self.api('mobile_payments.api.payment_link.edahab_payment_status', {{
            token: self.token
          }}).then(function(result) {{
            if (!result) return;
            if (result.status === 'Completed') {{
              self.stopTimers();
              self.showSection('ep-success-section');
            }} else if (result.status === 'Failed' || result.status === 'Cancelled') {{
              self.stopTimers();
              self.showError(result.error_message || 'Payment was declined or cancelled.');
            }}
          }}).catch(function() {{}});
        }}, 2000);
      }},

      showError: function(msg) {{
        document.getElementById('ep-error-msg').textContent = msg;
        this.showSection('ep-error-section');
      }}
    }};
    </script>
    """

    frappe.respond_as_web_page(
        _("Pay with Edahab"),
        page_html,
        http_status_code=200,
        indicator_color="green",
    )


@frappe.whitelist(allow_guest=True)
def edahab_initiate_payment(token, phone):
    """Initiate an Edahab USSD Push payment from the self-hosted payment page.

    Called via AJAX from the Edahab payment page rendered by ``pay()``.
    Validates the token, checks the invoice, and triggers a USSD Push
    to the customer's Edahab phone number.

    Args:
        token: Payment link token
        phone: Customer's Edahab phone number

    Returns:
        dict: ``{success, pending, transaction_log, message, ...}``
    """
    if not token or not phone:
        return {"success": False, "message": _("Token and phone number are required.")}

    # ── Find & validate the payment-link log ──
    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )
    if not log_name:
        return {"success": False, "message": _("Invalid or expired payment link.")}

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    # Expiry check
    if log.payment_link_expiry:
        if now_datetime() > get_datetime(log.payment_link_expiry):
            return {"success": False, "message": _("This payment link has expired.")}

    # Already paid
    if log.status == "Completed":
        return {"success": True, "message": _("This invoice has already been paid.")}

    # Invoice validation
    invoice_id = log.sales_invoice
    if not invoice_id or not frappe.db.exists("Sales Invoice", invoice_id):
        return {"success": False, "message": _("Invoice not found.")}

    inv_data = frappe.db.get_value(
        "Sales Invoice", invoice_id,
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
        log.db_set("payment_link_token", None)
        frappe.db.commit()
        return {"success": True, "message": _("This invoice has already been paid.")}

    # ── Initiate USSD Push via Edahab Purchase API ──
    charge_amount = float(inv_data.grand_total or 0)

    try:
        from mobile_payments.api.edahab import EdahabClient
        client = EdahabClient()
        result = client.purchase_request(
            phone=phone,
            amount=charge_amount,
            invoice_id=invoice_id,
            description=f"Payment for {invoice_id}",
            currency=log.currency or inv_data.currency or "USD",
            transaction_log=log.name,
        )

        frappe.logger("mobile_payments").info(
            f"Edahab USSD initiated from payment link {token[:8]}... | "
            f"Invoice: {invoice_id} | Phone: {phone} | Result: {result.get('pending', False)}"
        )

        # If Edahab returned instant success, create PE.
        # CRITICAL: commit first so the log status (Completed) is visible
        # to both inline and background processing.
        if result.get("success") and invoice_id:
            frappe.db.commit()

            # Process payment INLINE — same pattern as WaafiPay callback
            from mobile_payments.utils.payment_handler import process_successful_payment
            try:
                process_successful_payment(
                    transaction_log=log.name,
                    invoice_id=invoice_id,
                )
            except Exception as pe_err:
                frappe.log_error(
                    message=(
                        f"Inline PE failed for Edahab payment link: {pe_err}\n"
                        f"Log: {log.name} | Invoice: {invoice_id}\n"
                        f"{frappe.get_traceback()}"
                    ),
                    title="Edahab Payment Link PE Error",
                )
                frappe.enqueue(
                    "mobile_payments.utils.payment_handler.process_successful_payment",
                    transaction_log=log.name,
                    invoice_id=invoice_id,
                    queue="short",
                )

            frappe.logger("mobile_payments").info(
                f"Edahab instant success PE processed | log={log.name} | invoice={invoice_id}"
            )

        return result

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="Edahab Payment Link USSD Error")
        return {
            "success": False,
            "message": _("An error occurred while initiating the payment. Please try again."),
        }


@frappe.whitelist(allow_guest=True)
def edahab_payment_status(token):
    """Check Edahab payment status from the self-hosted payment page.

    Called via AJAX polling from the Edahab payment page.
    Looks up the latest transaction log for this token and checks
    with Edahab if the payment is still pending.

    Args:
        token: Payment link token

    Returns:
        dict: ``{status, error_message, provider_transaction_id}``
    """
    if not token:
        return {"status": "Failed", "error_message": "No token provided."}

    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"payment_link_token": token},
        "name",
    )
    if not log_name:
        return {"status": "Failed", "error_message": "Invalid payment link."}

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    if log.status == "Completed":
        return {
            "status": "Completed",
            "provider_transaction_id": log.provider_transaction_id or "",
        }

    if log.status in ("Cancelled", "Timeout"):
        return {
            "status": log.status,
            "error_message": log.error_message or f"Payment {log.status.lower()}.",
        }

    # ── Still pending or failed (e.g. Edahab error code 7) — ask Edahab ──
    # Error code 7 sets status to "Failed" but the payment may still complete
    # on Edahab's side, so we always recheck with the provider.
    if log.status in ("Pending", "Initiated", "Failed") and log.provider_reference:
        try:
            from mobile_payments.api.edahab import EdahabClient
            client = EdahabClient()
            status_result = client.check_transaction_status(log.provider_reference)

            invoice_status = status_result.get("invoice_status", "")
            if invoice_status == "Paid":
                txn_id = status_result.get("transaction_id", "")
                frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                    "status": "Completed",
                    "provider_transaction_id": txn_id,
                    "completed_at": now_datetime(),
                }, update_modified=False)
                frappe.db.commit()

                # Commit first, then process PE inline (same as WaafiPay)
                if log.sales_invoice and not log.payment_entry:
                    from mobile_payments.utils.payment_handler import process_successful_payment
                    try:
                        process_successful_payment(
                            transaction_log=log.name,
                            invoice_id=log.sales_invoice,
                        )
                    except Exception as e:
                        frappe.log_error(
                            message=f"Inline PE failed for Edahab poll success: {e}\n{frappe.get_traceback()}",
                            title="Edahab Poll PE Error",
                        )
                        frappe.enqueue(
                            "mobile_payments.utils.payment_handler.process_successful_payment",
                            transaction_log=log.name,
                            invoice_id=log.sales_invoice,
                            queue="short",
                        )

                return {
                    "status": "Completed",
                    "provider_transaction_id": txn_id,
                }

            elif invoice_status in ("Cancelled", "Declined", "Failed"):
                err_msg = status_result.get("message", f"Invoice {invoice_status}")
                frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                    "status": "Failed",
                    "error_message": err_msg,
                    "completed_at": now_datetime(),
                }, update_modified=False)
                frappe.db.commit()
                return {"status": "Failed", "error_message": err_msg}

        except Exception:
            frappe.log_error(
                message=frappe.get_traceback(),
                title="Edahab Payment Status Check Error",
            )

    # Still pending — no change
    return {"status": log.status, "error_message": ""}


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
        log.db_set("payment_link_token", None)
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

    # ── Edahab: redirect to main pay() which serves our self-hosted page ──
    if provider == "Edahab":
        prefill_phone = log.phone_number or ""
        if not prefill_phone or prefill_phone == "HPP":
            customer = frappe.db.get_value("Sales Invoice", invoice_id, "customer")
            if customer:
                try:
                    from mobile_payments.api.pos import get_customer_phone_for_provider
                    phone_data = get_customer_phone_for_provider(customer, provider="Edahab")
                    prefill_phone = phone_data.get("phone", "") if phone_data else ""
                except Exception:
                    prefill_phone = ""
            if prefill_phone == "HPP":
                prefill_phone = ""

        _render_edahab_payment_page(
            token, invoice_id, amount,
            inv_data.currency or "USD", prefill_phone,
        )
        return

    # ── WaafiPay: create fresh HPP session and redirect ──
    description = f"Payment for {invoice_id}"

    try:
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
                    {"status": "Completed", "payment_link_token": None},
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
    log.db_set("payment_link_token", None)

    return {
        "success": True,
        "message": "Payment link has been revoked",
    }
