"""
WooCommerce Integration for Mobile Payments
Handles mobile money payments for orders coming from WooCommerce via the
ERPNext WooCommerce connector (frappe/erpnext-woocommerce or similar).

Flow:
1. WooCommerce order placed → synced to ERPNext as Sales Order / Sales Invoice
2. If WooCommerce payment method is mobile money, we auto-initiate payment
3. WooCommerce can also send payment webhooks that we process
4. Additionally provides a REST API that WooCommerce plugins can call directly
"""
from __future__ import unicode_literals

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, getdate, cstr


# ──────────────────────────────────────────────
# WooCommerce Webhook Handler
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def woocommerce_payment_webhook():
    """
    Receive payment notifications from WooCommerce.
    This endpoint is called by WooCommerce when an order payment status changes.

    WooCommerce webhook payload includes:
    - id: WooCommerce order ID
    - status: Order status (processing, completed, etc.)
    - payment_method: Payment method used
    - payment_method_title: Display name
    - meta_data: May contain mobile payment details

    Expected custom meta fields from WooCommerce Mobile Money plugin:
    - _mobile_payment_provider: WaafiPay or Edahab
    - _mobile_payment_method: ZAAD, SAHAL, EVCPlus, Edahab
    - _mobile_payment_phone: Customer phone number
    - _mobile_payment_transaction_id: Provider transaction ID
    """
    try:
        # Validate webhook
        if not _validate_woocommerce_webhook():
            frappe.local.response["http_status_code"] = 401
            return {"error": "Invalid webhook signature"}

        data = frappe.local.form_dict
        if not data:
            data = json.loads(frappe.request.data or "{}")

        if not data:
            frappe.local.response["http_status_code"] = 400
            return {"error": "No data received"}

        frappe.logger("mobile_payments").info(
            f"WooCommerce webhook received: Order #{data.get('id')}"
        )

        # Process the webhook
        result = _process_woocommerce_order(data)

        return result

    except Exception as e:
        frappe.log_error(
            f"WooCommerce webhook error: {str(e)}\n{frappe.get_traceback()}",
            "WooCommerce Mobile Payment Webhook Error",
        )
        frappe.local.response["http_status_code"] = 500
        return {"error": "Internal server error"}


def _validate_woocommerce_webhook():
    """
    Validate WooCommerce webhook signature.
    WooCommerce signs webhooks with HMAC-SHA256 using the webhook secret.
    """
    settings = frappe.get_single("Mobile Payment Settings")
    webhook_secret = settings.get("woocommerce_webhook_secret")

    if not webhook_secret:
        # No secret configured, allow (but log warning)
        frappe.logger("mobile_payments").warning(
            "WooCommerce webhook secret not configured - accepting without validation"
        )
        return True

    # Get signature from headers
    signature = frappe.request.headers.get("X-WC-Webhook-Signature", "")
    if not signature:
        return False

    # Calculate expected signature
    body = frappe.request.data or b""
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()

    import base64
    expected_b64 = base64.b64encode(expected).decode("utf-8")

    return hmac.compare_digest(signature, expected_b64)


def _process_woocommerce_order(data):
    """
    Process a WooCommerce order for mobile payment.

    Args:
        data: WooCommerce order data

    Returns:
        dict with processing result
    """
    wc_order_id = data.get("id")
    payment_method = data.get("payment_method", "")
    status = data.get("status", "")

    # Check if this is a mobile money payment
    meta_data = data.get("meta_data", [])
    meta = _parse_meta_data(meta_data)

    # Mobile payment identifiers
    mobile_payment_methods = [
        "waafipay", "edahab", "zaad", "sahal", "evcplus",
        "mobile_money", "mobile_payment",
    ]

    is_mobile = (
        payment_method.lower() in mobile_payment_methods
        or meta.get("_mobile_payment_provider")
        or meta.get("_is_mobile_payment") == "yes"
    )

    if not is_mobile:
        return {"status": "skipped", "message": "Not a mobile payment order"}

    # Find the linked ERPNext Sales Invoice or Sales Order
    invoice_name = _find_linked_invoice(wc_order_id, data)

    if not invoice_name:
        frappe.logger("mobile_payments").info(
            f"No ERPNext invoice found for WooCommerce order #{wc_order_id} - "
            f"queuing for later processing"
        )
        # Queue for later - the WooCommerce sync may not have happened yet
        _queue_pending_wc_order(wc_order_id, data, meta)
        return {"status": "queued", "message": "Order queued for processing"}

    # Determine provider and method from meta or payment_method
    provider = meta.get("_mobile_payment_provider", "")
    method = meta.get("_mobile_payment_method", "")
    phone = meta.get("_mobile_payment_phone", "")
    provider_tx_id = meta.get("_mobile_payment_transaction_id", "")

    if not provider:
        provider = _detect_provider(payment_method)
    if not method:
        method = _detect_method(payment_method, provider)

    # Create or update transaction log
    log = _create_wc_transaction_log(
        wc_order_id=wc_order_id,
        invoice_name=invoice_name,
        provider=provider,
        method=method,
        phone=phone,
        amount=flt(data.get("total", 0)),
        currency=data.get("currency", "USD"),
        provider_tx_id=provider_tx_id,
        status="Completed" if status in ("processing", "completed") else "Pending",
    )

    # If payment is already confirmed, process it
    if log.status == "Completed":
        frappe.enqueue(
            "mobile_payments.utils.payment_handler.process_successful_payment",
            transaction_log=log.name,
            invoice_id=invoice_name,
            queue="short",
        )
        return {
            "status": "success",
            "transaction_log": log.name,
            "message": "Payment processing initiated",
        }

    return {
        "status": "pending",
        "transaction_log": log.name,
        "message": "Transaction logged, awaiting confirmation",
    }


# ──────────────────────────────────────────────
# WooCommerce REST API Endpoints
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def initiate_wc_payment():
    """
    REST API endpoint for WooCommerce mobile money plugin to initiate payment.
    Called directly from WooCommerce checkout when customer selects mobile money.

    Expected POST body:
    {
        "order_id": 12345,
        "provider": "WaafiPay",
        "method": "ZAAD",
        "phone": "252612345678",
        "amount": 100.00,
        "currency": "USD",
        "customer_email": "customer@example.com",
        "api_key": "configured_api_key"
    }

    Returns:
        dict with payment status
    """
    try:
        data = json.loads(frappe.request.data or "{}")

        # Validate API key
        if not _validate_wc_api_key(data.get("api_key", "")):
            frappe.local.response["http_status_code"] = 401
            return {"success": False, "error": "Invalid API key"}

        # Validate required fields
        required = ["order_id", "provider", "phone", "amount"]
        for field in required:
            if not data.get(field):
                frappe.local.response["http_status_code"] = 400
                return {"success": False, "error": f"Missing required field: {field}"}

        provider = data["provider"]
        method = data.get("method", provider)
        phone = data["phone"]
        amount = flt(data["amount"])
        wc_order_id = data["order_id"]
        currency = data.get("currency", "USD")

        # Find linked ERPNext document (may not exist yet)
        invoice_name = _find_linked_invoice(wc_order_id, data)

        # Create transaction log
        log = frappe.get_doc({
            "doctype": "Mobile Payment Transaction Log",
            "provider": provider,
            "payment_method": method,
            "phone_number": phone,
            "amount": amount,
            "currency": currency,
            "status": "Initiated",
            "sales_invoice": invoice_name or "",
            "request_timestamp": now_datetime(),
            "custom_woocommerce_order_id": str(wc_order_id),
            "custom_source": "WooCommerce",
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()

        # Initiate payment with provider
        try:
            if provider == "Edahab":
                from mobile_payments.api.edahab import EdahabClient
                client = EdahabClient()
                result = client.purchase_request(
                    phone=phone,
                    amount=amount,
                    description=f"WooCommerce Order #{wc_order_id}",
                    transaction_id=log.transaction_id,
                )
            else:
                from mobile_payments.api.waafipay import WaafiPayClient
                client = WaafiPayClient()
                result = client.purchase_request(
                    phone=phone,
                    amount=amount,
                    method=method,
                    description=f"WooCommerce Order #{wc_order_id}",
                    transaction_id=log.transaction_id,
                )

            if result.get("success"):
                log.status = "Completed"
                log.provider_transaction_id = result.get("transaction_id", "")
                log.provider_response = str(result.get("raw_response", ""))
                log.response_timestamp = now_datetime()
                log.save(ignore_permissions=True)
                frappe.db.commit()

                # Process payment if invoice exists
                if invoice_name:
                    frappe.enqueue(
                        "mobile_payments.utils.payment_handler.process_successful_payment",
                        transaction_log=log.name,
                        invoice_id=invoice_name,
                        queue="short",
                    )

                return {
                    "success": True,
                    "transaction_id": log.transaction_id,
                    "provider_transaction_id": result.get("transaction_id", ""),
                    "status": "completed",
                    "message": "Payment successful",
                }

            elif result.get("pending"):
                log.status = "Pending"
                log.save(ignore_permissions=True)
                frappe.db.commit()

                return {
                    "success": False,
                    "pending": True,
                    "transaction_id": log.transaction_id,
                    "status": "pending",
                    "message": "Payment pending - check status",
                    "status_url": f"/api/method/mobile_payments.api.woocommerce.check_wc_payment_status?transaction_id={log.transaction_id}",
                }

            else:
                log.status = "Failed"
                log.error_message = result.get("message", "Payment failed")
                log.save(ignore_permissions=True)
                frappe.db.commit()

                return {
                    "success": False,
                    "transaction_id": log.transaction_id,
                    "status": "failed",
                    "message": result.get("message", "Payment failed"),
                }

        except Exception as e:
            log.status = "Failed"
            log.error_message = str(e)
            log.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.log_error(
                f"WC payment error: {str(e)}\n{frappe.get_traceback()}",
                "WooCommerce Mobile Payment Error",
            )

            return {
                "success": False,
                "transaction_id": log.transaction_id,
                "status": "error",
                "message": str(e),
            }

    except Exception as e:
        frappe.log_error(
            f"WC initiate payment error: {str(e)}\n{frappe.get_traceback()}",
            "WooCommerce Initiate Payment Error",
        )
        frappe.local.response["http_status_code"] = 500
        return {"success": False, "error": "Internal server error"}


@frappe.whitelist(allow_guest=True)
def check_wc_payment_status():
    """
    Check payment status for a WooCommerce transaction.
    Called by WooCommerce to poll for payment completion.

    Query params:
        transaction_id: The transaction ID returned from initiate_wc_payment
        api_key: API key for authentication

    Returns:
        dict with current payment status
    """
    transaction_id = frappe.form_dict.get("transaction_id")
    api_key = frappe.form_dict.get("api_key", "")

    if not _validate_wc_api_key(api_key):
        frappe.local.response["http_status_code"] = 401
        return {"success": False, "error": "Invalid API key"}

    if not transaction_id:
        frappe.local.response["http_status_code"] = 400
        return {"success": False, "error": "Missing transaction_id"}

    # Find the transaction log
    log_name = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"transaction_id": transaction_id},
        "name",
    )

    if not log_name:
        frappe.local.response["http_status_code"] = 404
        return {"success": False, "error": "Transaction not found"}

    log = frappe.get_doc("Mobile Payment Transaction Log", log_name)

    # If still pending, check with provider
    if log.status in ("Pending", "Initiated"):
        try:
            if log.provider == "Edahab":
                from mobile_payments.api.edahab import EdahabClient
                client = EdahabClient()
                result = client.check_transaction_status(log.transaction_id)
            else:
                from mobile_payments.api.waafipay import WaafiPayClient
                client = WaafiPayClient()
                result = client.check_transaction_status(log.transaction_id)

            if result.get("success"):
                log.status = "Completed"
                log.provider_transaction_id = result.get("transaction_id", "")
                log.response_timestamp = now_datetime()
                log.save(ignore_permissions=True)
                frappe.db.commit()

                # Process payment if invoice is linked
                if log.sales_invoice and not log.payment_entry:
                    frappe.enqueue(
                        "mobile_payments.utils.payment_handler.process_successful_payment",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                        queue="short",
                    )

            elif result.get("failed"):
                log.status = "Failed"
                log.error_message = result.get("message", "")
                log.response_timestamp = now_datetime()
                log.save(ignore_permissions=True)
                frappe.db.commit()

        except Exception as e:
            frappe.log_error(
                f"WC status check error: {str(e)}",
                "WooCommerce Payment Status Error",
            )

    return {
        "success": log.status == "Completed",
        "status": log.status.lower(),
        "transaction_id": log.transaction_id,
        "provider_transaction_id": log.provider_transaction_id or "",
        "payment_entry": log.payment_entry or "",
        "error_message": log.error_message or "",
    }


# ──────────────────────────────────────────────
# WooCommerce Order Sync Hooks
# ──────────────────────────────────────────────

def on_woocommerce_order_sync(doc, method=None):
    """
    Hook into WooCommerce order sync.
    Called when ERPNext WooCommerce connector creates/updates a Sales Order or Sales Invoice.

    This is registered in hooks.py via doc_events for Sales Order/Sales Invoice.
    """
    # Check if this document came from WooCommerce
    wc_order_id = _get_wc_order_id(doc)
    if not wc_order_id:
        return

    # Check if there's a pending mobile payment for this WooCommerce order
    pending_logs = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "custom_woocommerce_order_id": str(wc_order_id),
            "sales_invoice": ["in", ["", None]],
        },
        fields=["name", "status"],
    )

    if not pending_logs:
        # Also check queued orders
        _process_queued_wc_order(wc_order_id, doc.name)
        return

    for log_data in pending_logs:
        log = frappe.get_doc("Mobile Payment Transaction Log", log_data["name"])
        log.sales_invoice = doc.name
        log.save(ignore_permissions=True)

        frappe.logger("mobile_payments").info(
            f"Linked WooCommerce order #{wc_order_id} transaction {log.name} "
            f"to {doc.doctype} {doc.name}"
        )

        # If payment was already completed, create Payment Entry
        if log.status == "Completed" and not log.payment_entry:
            settings = frappe.get_single("Mobile Payment Settings")
            if settings.auto_create_payment_entry:
                frappe.enqueue(
                    "mobile_payments.utils.payment_handler.process_successful_payment",
                    transaction_log=log.name,
                    invoice_id=doc.name,
                    queue="short",
                )

    frappe.db.commit()


def process_pending_wc_orders():
    """
    Scheduled task to process any pending WooCommerce orders
    that weren't linked during initial sync.
    Runs every 10 minutes.
    """
    # Find transaction logs from WooCommerce without linked invoices
    pending = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "custom_source": "WooCommerce",
            "sales_invoice": ["in", ["", None]],
            "status": ["in", ["Completed", "Pending"]],
            "creation": [">", frappe.utils.add_days(now_datetime(), -7)],
        },
        fields=["name", "custom_woocommerce_order_id"],
    )

    for p in pending:
        wc_order_id = p.get("custom_woocommerce_order_id")
        if not wc_order_id:
            continue

        # Try to find the linked invoice
        invoice_name = _find_linked_invoice(wc_order_id, {})

        if invoice_name:
            log = frappe.get_doc("Mobile Payment Transaction Log", p["name"])
            log.sales_invoice = invoice_name
            log.save(ignore_permissions=True)

            frappe.logger("mobile_payments").info(
                f"Late-linked WC order #{wc_order_id}: {log.name} → {invoice_name}"
            )

            if log.status == "Completed" and not log.payment_entry:
                frappe.enqueue(
                    "mobile_payments.utils.payment_handler.process_successful_payment",
                    transaction_log=log.name,
                    invoice_id=invoice_name,
                    queue="short",
                )

    if pending:
        frappe.db.commit()


# ──────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────

def _parse_meta_data(meta_data):
    """Parse WooCommerce meta_data array into a dict."""
    result = {}
    if isinstance(meta_data, list):
        for item in meta_data:
            if isinstance(item, dict) and "key" in item:
                result[item["key"]] = item.get("value", "")
    return result


def _find_linked_invoice(wc_order_id, data):
    """
    Find the ERPNext Sales Invoice linked to a WooCommerce order.
    The WooCommerce connector stores the WC order ID in different ways
    depending on the connector version.
    """
    wc_order_id = str(wc_order_id)

    # Method 1: Check custom field 'woocommerce_order_id' on Sales Invoice
    invoice = frappe.db.get_value(
        "Sales Invoice",
        {"woocommerce_order_id": wc_order_id, "docstatus": 1},
        "name",
    )
    if invoice:
        return invoice

    # Method 2: Check 'woocommerce_id' field
    invoice = frappe.db.get_value(
        "Sales Invoice",
        {"woocommerce_id": wc_order_id, "docstatus": 1},
        "name",
    )
    if invoice:
        return invoice

    # Method 3: Check Sales Order first (some setups create SO, not SI)
    so_name = frappe.db.get_value(
        "Sales Order",
        {"woocommerce_order_id": wc_order_id},
        "name",
    )
    if so_name:
        # Find SI linked to this SO
        invoice = frappe.db.get_value(
            "Sales Invoice Item",
            {"sales_order": so_name, "docstatus": 1},
            "parent",
        )
        if invoice:
            return invoice

    # Method 4: Search in comment/remarks
    invoice = frappe.db.get_value(
        "Sales Invoice",
        {"remarks": ["like", f"%{wc_order_id}%"], "docstatus": 1},
        "name",
    )
    if invoice:
        return invoice

    return None


def _get_wc_order_id(doc):
    """Get WooCommerce order ID from a document."""
    # Check various fields where WC order ID might be stored
    for field in ["woocommerce_order_id", "woocommerce_id", "wc_order_id"]:
        val = doc.get(field) if hasattr(doc, field) else None
        if val:
            return str(val)

    # Check if mentioned in remarks
    if doc.get("remarks") and "woocommerce" in (doc.remarks or "").lower():
        import re
        match = re.search(r"(?:woocommerce|wc)\s*(?:order)?\s*#?\s*(\d+)", doc.remarks, re.I)
        if match:
            return match.group(1)

    return None


def _detect_provider(payment_method):
    """Detect provider from WooCommerce payment method slug."""
    pm = payment_method.lower()

    if "edahab" in pm:
        return "Edahab"
    elif any(x in pm for x in ["waafipay", "waafi", "zaad", "sahal", "evcplus", "evc"]):
        return "WaafiPay"
    elif "mobile" in pm:
        return "WaafiPay"  # Default to WaafiPay for generic mobile money

    return "WaafiPay"


def _detect_method(payment_method, provider):
    """Detect payment method from WooCommerce payment method slug."""
    pm = payment_method.lower()

    if "zaad" in pm:
        return "ZAAD"
    elif "sahal" in pm:
        return "SAHAL"
    elif "evc" in pm:
        return "EVCPlus"
    elif "edahab" in pm:
        return "Edahab"

    # Default based on provider
    if provider == "Edahab":
        return "Edahab"
    return "ZAAD"  # Default WaafiPay method


def _create_wc_transaction_log(wc_order_id, invoice_name, provider, method,
                                phone, amount, currency, provider_tx_id, status):
    """Create a transaction log for a WooCommerce payment."""

    # Check if log already exists for this WC order
    existing = frappe.db.get_value(
        "Mobile Payment Transaction Log",
        {"custom_woocommerce_order_id": str(wc_order_id)},
        "name",
    )

    if existing:
        log = frappe.get_doc("Mobile Payment Transaction Log", existing)
        log.status = status
        if invoice_name:
            log.sales_invoice = invoice_name
        if provider_tx_id:
            log.provider_transaction_id = provider_tx_id
        log.save(ignore_permissions=True)
        frappe.db.commit()
        return log

    log = frappe.get_doc({
        "doctype": "Mobile Payment Transaction Log",
        "provider": provider,
        "payment_method": method,
        "phone_number": phone,
        "amount": amount,
        "currency": currency,
        "status": status,
        "sales_invoice": invoice_name or "",
        "provider_transaction_id": provider_tx_id or "",
        "request_timestamp": now_datetime(),
        "response_timestamp": now_datetime() if status == "Completed" else None,
        "custom_woocommerce_order_id": str(wc_order_id),
        "custom_source": "WooCommerce",
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    return log


def _validate_wc_api_key(api_key):
    """
    Validate the WooCommerce API key.
    This is a simple shared secret between WooCommerce and ERPNext.
    """
    if not api_key:
        # Allow if no API key is configured (for backward compatibility)
        settings = frappe.get_single("Mobile Payment Settings")
        configured_key = settings.get("woocommerce_api_key")
        return not configured_key  # Allow if no key configured

    settings = frappe.get_single("Mobile Payment Settings")
    configured_key = settings.get("woocommerce_api_key")

    if not configured_key:
        return True  # Not configured, allow all

    return hmac.compare_digest(api_key, configured_key)


def _queue_pending_wc_order(wc_order_id, data, meta):
    """Queue a WooCommerce order for later processing."""
    # Store in a simple cache/custom doctype
    cache_key = f"wc_pending_order_{wc_order_id}"
    frappe.cache().set_value(
        cache_key,
        {
            "wc_order_id": wc_order_id,
            "data": data,
            "meta": meta,
            "timestamp": str(now_datetime()),
        },
        expires_in_sec=86400 * 7,  # 7 days
    )


def _process_queued_wc_order(wc_order_id, invoice_name):
    """Process a previously queued WooCommerce order."""
    cache_key = f"wc_pending_order_{wc_order_id}"
    cached = frappe.cache().get_value(cache_key)

    if not cached:
        return

    meta = cached.get("meta", {})
    data = cached.get("data", {})

    provider = meta.get("_mobile_payment_provider", "")
    method = meta.get("_mobile_payment_method", "")
    phone = meta.get("_mobile_payment_phone", "")
    provider_tx_id = meta.get("_mobile_payment_transaction_id", "")
    status_str = data.get("status", "")

    if not provider:
        provider = _detect_provider(data.get("payment_method", ""))
    if not method:
        method = _detect_method(data.get("payment_method", ""), provider)

    log = _create_wc_transaction_log(
        wc_order_id=wc_order_id,
        invoice_name=invoice_name,
        provider=provider,
        method=method,
        phone=phone,
        amount=flt(data.get("total", 0)),
        currency=data.get("currency", "USD"),
        provider_tx_id=provider_tx_id,
        status="Completed" if status_str in ("processing", "completed") else "Pending",
    )

    # If completed, process payment
    if log.status == "Completed" and not log.payment_entry:
        frappe.enqueue(
            "mobile_payments.utils.payment_handler.process_successful_payment",
            transaction_log=log.name,
            invoice_id=invoice_name,
            queue="short",
        )

    # Clean up cache
    frappe.cache().delete_value(cache_key)
