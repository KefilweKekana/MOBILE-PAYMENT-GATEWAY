"""
Webhook & Callback Handlers
Handles incoming payment notifications from WaafiPay and Edahab.
Supports both server-to-server webhooks and HPP return callbacks.
"""
from __future__ import unicode_literals

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from mobile_payments.utils.security import (
    validate_ip_whitelist,
    validate_webhook_signature,
    check_replay_protection,
    get_client_ip,
)


# ──────────────────────────────────────────────
# WaafiPay Webhooks
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def waafipay_callback():
    """
    Handle WaafiPay server-to-server webhook callback.
    Called by WaafiPay when a payment status changes.

    Expected to receive POST with JSON body containing transaction details.
    """
    try:
        # Security validations
        client_ip = get_client_ip()
        validate_ip_whitelist(client_ip)

        # Parse request body
        if frappe.request.data:
            data = json.loads(frappe.request.data)
        else:
            data = frappe.form_dict

        frappe.logger("mobile_payments").info(
            f"WaafiPay webhook received from {client_ip}: {json.dumps(data, indent=2)}"
        )

        # Validate webhook signature if provided
        signature = frappe.request.headers.get("X-WaafiPay-Signature", "")
        if signature:
            validate_webhook_signature(
                frappe.request.data.decode("utf-8") if frappe.request.data else "",
                signature,
                provider="WaafiPay",
            )

        # Extract transaction details
        params = data.get("params", data.get("serviceParams", {}))
        transaction_id = params.get("transactionId", "")
        reference_id = params.get("referenceId", "")
        state = params.get("state", "")
        response_code = data.get("responseCode", "")

        # Replay protection
        event_id = transaction_id or reference_id
        if event_id and not check_replay_protection(event_id):
            frappe.logger("mobile_payments").warning(
                f"WaafiPay webhook replay blocked: {event_id}"
            )
            return {"status": "duplicate", "message": "Event already processed"}

        # Find the corresponding transaction log
        log = _find_transaction_log(
            provider="WaafiPay",
            reference=reference_id,
            transaction_id=transaction_id,
        )

        if not log:
            frappe.logger("mobile_payments").warning(
                f"WaafiPay webhook: No matching transaction found for "
                f"ref={reference_id}, txn={transaction_id}"
            )
            return {"status": "not_found", "message": "Transaction not found"}

        # Update transaction based on state
        if state == "APPROVED" and response_code == "2001":
            log.update_status(
                "Completed",
                provider_transaction_id=transaction_id,
                callback_payload=data,
            )

            # Process payment INLINE so invoice is marked Paid immediately
            from mobile_payments.utils.payment_handler import process_successful_payment
            try:
                process_successful_payment(
                    transaction_log=log.name,
                    invoice_id=log.sales_invoice,
                )
            except Exception as e:
                frappe.log_error(
                    message=f"Inline PE creation failed for WaafiPay webhook, falling back to enqueue: {e}",
                    title="WaafiPay Webhook Inline PE Error",
                )
                frappe.enqueue(
                    process_successful_payment,
                    queue="short",
                    transaction_log=log.name,
                    invoice_id=log.sales_invoice,
                )

            frappe.logger("mobile_payments").info(
                f"WaafiPay webhook: Payment APPROVED for {log.name}"
            )

        elif state == "DECLINED" or state == "CANCELLED":
            log.update_status(
                "Failed" if state == "DECLINED" else "Cancelled",
                error_message=f"Payment {state.lower()} by provider",
                error_code=response_code,
                callback_payload=data,
            )

        elif state == "PENDING":
            log.update_status("Processing", callback_payload=data)

        else:
            log.update_status(
                "Failed",
                error_message=f"Unknown state: {state}",
                callback_payload=data,
            )

        return {"status": "success", "message": "Webhook processed"}

    except frappe.AuthenticationError:
        frappe.local.response["http_status_code"] = 403
        return {"status": "error", "message": "Authentication failed"}

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="WaafiPay Webhook Error")
        return {"status": "error", "message": str(e)}


@frappe.whitelist(allow_guest=True)
def waafipay_hpp_return(**kwargs):
    """
    Handle WaafiPay HPP return redirect.
    Called when customer completes or cancels payment on HPP.
    Redirects user back to the Sales Invoice.
    """
    try:
        data = kwargs or frappe.form_dict
        frappe.logger("mobile_payments").info(
            f"WaafiPay HPP return: {json.dumps(dict(data), indent=2)}"
        )

        reference_id = data.get("referenceId", data.get("reference_id", ""))
        transaction_id = data.get("transactionId", data.get("transaction_id", ""))
        state = data.get("state", data.get("status", ""))
        response_code = data.get("responseCode", data.get("response_code", ""))

        # Find transaction log
        log = _find_transaction_log(
            provider="WaafiPay",
            reference=reference_id,
            transaction_id=transaction_id,
        )

        # WaafiPay HPP success can come as:
        #   state="APPROVED" + responseCode="2001"   (standard)
        #   state="SUCCESS"                           (some HPP variants)
        #   responseCode="2001" alone                 (when state is missing)
        is_success = (
            state in ("APPROVED", "SUCCESS", "RCS_SUCCESS")
            or response_code in ("2001",)
        ) and state not in ("DECLINED", "CANCELLED", "FAILED")

        if log:
            if is_success:
                log.update_status(
                    "Completed",
                    provider_transaction_id=transaction_id,
                    callback_payload=data,
                )
                from mobile_payments.utils.payment_handler import process_successful_payment
                # Process INLINE so invoice is Paid before we redirect the customer
                try:
                    process_successful_payment(
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )
                except Exception as e:
                    frappe.log_error(
                        message=f"Inline PE creation failed for WaafiPay HPP return, falling back to enqueue: {e}",
                        title="WaafiPay HPP Inline PE Error",
                    )
                    frappe.enqueue(
                        process_successful_payment,
                        queue="short",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )
            elif state in ("DECLINED", "CANCELLED", "FAILED"):
                log.update_status(
                    "Failed" if state in ("DECLINED", "FAILED") else "Cancelled",
                    callback_payload=data,
                )

            # Redirect based on payment outcome
            if is_success:
                frappe.local.response["type"] = "redirect"
                frappe.local.response["location"] = "/payment-success"
                return
            elif log.sales_invoice:
                frappe.local.response["type"] = "redirect"
                frappe.local.response["location"] = (
                    f"/app/sales-invoice/{log.sales_invoice}"
                )
                return

        # Default redirect
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app/sales-invoice"

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="WaafiPay HPP Return")
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app/sales-invoice"


# ──────────────────────────────────────────────
# Edahab Webhooks
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def edahab_callback():
    """
    Handle Edahab server-to-server webhook callback.
    Called by Edahab when a payment status changes.
    """
    try:
        # Security validations
        client_ip = get_client_ip()
        validate_ip_whitelist(client_ip)

        # Parse request body
        if frappe.request.data:
            data = json.loads(frappe.request.data)
        else:
            data = frappe.form_dict

        frappe.logger("mobile_payments").info(
            f"Edahab webhook received from {client_ip}: {json.dumps(data, indent=2)}"
        )

        # Extract transaction details
        transaction_id = str(data.get("TransactionId", data.get("transactionId", "")))
        invoice_id = str(data.get("InvoiceId", data.get("invoiceId", "")))
        status_code = data.get("StatusCode", data.get("statusCode", -1))
        transaction_status = data.get(
            "TransactionStatus", data.get("transactionStatus", "")
        )

        # Replay protection
        event_id = transaction_id or invoice_id
        if event_id and not check_replay_protection(event_id):
            frappe.logger("mobile_payments").warning(
                f"Edahab webhook replay blocked: {event_id}"
            )
            return {"status": "duplicate", "message": "Event already processed"}

        # Find the corresponding transaction log
        log = _find_transaction_log(
            provider="Edahab",
            reference=invoice_id,
            transaction_id=transaction_id,
        )

        if not log:
            frappe.logger("mobile_payments").warning(
                f"Edahab webhook: No matching transaction found for "
                f"invoice={invoice_id}, txn={transaction_id}"
            )
            return {"status": "not_found", "message": "Transaction not found"}

        # Update transaction based on status
        if status_code == 0 or transaction_status == "Approved":
            log.update_status(
                "Completed",
                provider_transaction_id=transaction_id,
                callback_payload=data,
            )

            # Process payment INLINE so invoice is marked Paid immediately
            from mobile_payments.utils.payment_handler import process_successful_payment
            try:
                process_successful_payment(
                    transaction_log=log.name,
                    invoice_id=log.sales_invoice,
                )
            except Exception as e:
                frappe.log_error(
                    message=f"Inline PE creation failed for Edahab webhook, falling back to enqueue: {e}",
                    title="Edahab Webhook Inline PE Error",
                )
                frappe.enqueue(
                    process_successful_payment,
                    queue="short",
                    transaction_log=log.name,
                    invoice_id=log.sales_invoice,
                )

            frappe.logger("mobile_payments").info(
                f"Edahab webhook: Payment APPROVED for {log.name}"
            )

        elif transaction_status in ("Declined", "Cancelled", "Rejected"):
            status = "Cancelled" if transaction_status == "Cancelled" else "Failed"
            log.update_status(
                status,
                error_message=f"Payment {transaction_status.lower()} by Edahab",
                error_code=str(status_code),
                callback_payload=data,
            )

        elif transaction_status == "Pending":
            log.update_status("Processing", callback_payload=data)

        else:
            log.update_status(
                "Failed",
                error_message=f"Unknown status: {transaction_status}",
                callback_payload=data,
            )

        return {"status": "success", "message": "Webhook processed"}

    except frappe.AuthenticationError:
        frappe.local.response["http_status_code"] = 403
        return {"status": "error", "message": "Authentication failed"}

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="Edahab Webhook Error")
        return {"status": "error", "message": str(e)}


@frappe.whitelist(allow_guest=True)
def edahab_hpp_return(**kwargs):
    """
    Handle Edahab HPP return redirect.
    Called when customer completes or cancels payment on the hosted page.
    """
    try:
        data = kwargs or frappe.form_dict
        frappe.logger("mobile_payments").info(
            f"Edahab HPP return: {json.dumps(dict(data), indent=2)}"
        )

        invoice_id = str(data.get("InvoiceId", data.get("invoiceId", "")))
        transaction_id = str(data.get("TransactionId", data.get("transactionId", "")))
        status_code = data.get("StatusCode", data.get("statusCode", -1))
        transaction_status = data.get(
            "TransactionStatus", data.get("transactionStatus", "")
        )

        # Find transaction log
        log = _find_transaction_log(
            provider="Edahab",
            reference=invoice_id,
            transaction_id=transaction_id,
        )

        # Edahab success: StatusCode=0 OR TransactionStatus="Approved"/"Success"
        try:
            sc = int(status_code)
        except (TypeError, ValueError):
            sc = -1
        is_success = sc == 0 or transaction_status in ("Approved", "Success", "APPROVED")

        if log:
            if is_success:
                log.update_status(
                    "Completed",
                    provider_transaction_id=transaction_id,
                    callback_payload=data,
                )
                from mobile_payments.utils.payment_handler import process_successful_payment
                # Process INLINE so invoice is Paid before customer is redirected
                try:
                    process_successful_payment(
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )
                except Exception as e:
                    frappe.log_error(
                        message=f"Inline PE creation failed for Edahab HPP return, falling back to enqueue: {e}",
                        title="Edahab HPP Inline PE Error",
                    )
                    frappe.enqueue(
                        process_successful_payment,
                        queue="short",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )
            elif transaction_status in ("Declined", "Cancelled", "Failed", "Rejected"):
                log.update_status(
                    "Cancelled" if transaction_status == "Cancelled" else "Failed",
                    callback_payload=data,
                )

            # Redirect to Sales Invoice
            if log.sales_invoice:
                frappe.local.response["type"] = "redirect"
                frappe.local.response["location"] = (
                    f"/app/sales-invoice/{log.sales_invoice}"
                )
                return

        # Default redirect
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app/sales-invoice"

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="Edahab HPP Return")
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app/sales-invoice"


# ──────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────

def _find_transaction_log(provider, reference="", transaction_id=""):
    """
    Find a transaction log entry by provider reference or transaction ID.

    Search order (most → least specific):
    1. provider_transaction_id  — set after commit/approval
    2. provider_reference       — set to provider's own invoice/order ID on HPP create
    3. sales_invoice            — ERPNext invoice name sent as referenceId in HPP payload;
                                   the most reliable fallback for HPP callbacks because
                                   referenceId in the callback IS the sales invoice name
    4. hpp_url contains reference — last-resort partial match

    Args:
        provider: Payment provider name
        reference: Provider reference / ERPNext invoice name returned in callback
        transaction_id: Provider transaction ID

    Returns:
        Mobile Payment Transaction Log document or None
    """

    def _get(filters, order_by=None):
        kw = {}
        if order_by:
            kw["order_by"] = order_by
        name = frappe.db.get_value("Mobile Payment Transaction Log", filters, "name", **kw)
        return frappe.get_doc("Mobile Payment Transaction Log", name) if name else None

    # 1. Match by provider_transaction_id (most specific — set after approval)
    if transaction_id:
        doc = _get({"provider": provider, "provider_transaction_id": transaction_id})
        if doc:
            return doc

    # 2. Match by provider_reference (provider's own HPP invoice/order ID)
    if reference:
        doc = _get({"provider": provider, "provider_reference": reference})
        if doc:
            return doc

    # 3. Match by sales_invoice — this is the ERPNext invoice name that was sent
    #    as referenceId in the HPP payload and comes back unchanged in the callback.
    #    Prefer Pending/Processing (most recent unresolved payment for this invoice).
    if reference:
        doc = _get(
            {
                "provider": provider,
                "sales_invoice": reference,
                "status": ["in", ("Initiated", "Pending", "Processing")],
            },
            order_by="creation desc",
        )
        if doc:
            return doc

        # Also accept Completed so we don't create duplicate payments on retry
        doc = _get(
            {"provider": provider, "sales_invoice": reference},
            order_by="creation desc",
        )
        if doc:
            return doc

    # 4. Partial match on transaction_id field
    if reference:
        name = frappe.db.get_value(
            "Mobile Payment Transaction Log",
            {"provider": provider, "transaction_id": ["like", f"%{reference}%"]},
            "name",
        )
        if name:
            return frappe.get_doc("Mobile Payment Transaction Log", name)

    # 5. Last resort: most recent pending transaction for this provider
    if not reference and not transaction_id:
        doc = _get(
            {
                "provider": provider,
                "status": ["in", ("Initiated", "Pending", "Processing")],
            },
            order_by="creation desc",
        )
        if doc:
            return doc

    return None
