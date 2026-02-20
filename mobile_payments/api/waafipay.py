"""
WaafiPay API Integration
Supports both Purchase API (server-to-server) and Hosted Payment Page (HPP) flows.
Compatible with ZAAD, SAHAL, and EVCPlus payment methods.
"""
from __future__ import unicode_literals

import json
import uuid

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from mobile_payments.utils.security import sanitize_phone_number


# ──────────────────────────────────────────────
# WaafiPay Payment Method Mapping
# ──────────────────────────────────────────────
# Documented paymentMethod values (from https://docs.waafipay.com/api-introduction):
#   MWALLET_ACCOUNT     — mobile wallet (ZAAD, EVC, SAHAL)
#   MWALLET_BANKACCOUNT — bank account
# Note: "MERCHANT_ACCOUNT" is NOT a valid paymentMethod for receiving payments.
# Sending money to merchant numbers requires a separate creditAccount/API_CREDIT flow.
WAAFIPAY_CHANNELS = {
    "ZAAD":    {"channel": "MWALLET_ACCOUNT", "provider": "ZAAD"},
    "SAHAL":   {"channel": "MWALLET_ACCOUNT", "provider": "SAHAL"},
    "EVCPlus": {"channel": "MWALLET_ACCOUNT", "provider": "EVCPlus"},
}


class WaafiPayClient:
    """
    WaafiPay API client for Purchase API and HPP flows.
    
    Usage:
        client = WaafiPayClient()
        result = client.purchase_request(
            phone="252612345678",
            amount=10.00,
            method="ZAAD",
            invoice_id="SINV-00001",
            description="Payment for Invoice SINV-00001"
        )
    """

    def __init__(self):
        """Initialize with credentials from Mobile Payment Settings."""
        self.settings = frappe.get_single("Mobile Payment Settings")
        self.credentials = self.settings.get_waafipay_credentials()
        self.base_url = self.credentials["base_url"]
        self.hpp_base_url = self.credentials["hpp_base_url"]
        self.hpp_key = self.credentials.get("hpp_key", "") or self.credentials["api_key"]
        self.merchant_uid = self.credentials["merchant_uid"]
        self.store_id = self.credentials.get("store_id", "") or ""
        self.api_user_id = self.credentials["api_user_id"]
        self.api_key = self.credentials["api_key"]
        self.timeout = self.settings.transaction_timeout or 120

    def _get_headers(self):
        """Return standard request headers."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _generate_reference(self):
        """Generate unique reference ID for the transaction."""
        return uuid.uuid4().hex[:12].upper()

    def _make_reference_id(self, value):
        """
        Sanitize and enforce referenceId rules per WaafiPay spec:
        - Only letters, numbers, dashes, underscores, dots
        - Length: 10–30 characters
        """
        import re
        # Strip invalid chars
        clean = re.sub(r"[^A-Za-z0-9\-_.]", "-", str(value))
        # Enforce max 30 chars
        clean = clean[:30]
        # Enforce min 10 chars by padding with reference suffix
        if len(clean) < 10:
            clean = clean + "-" + uuid.uuid4().hex[:max(9 - len(clean), 1)]
            clean = clean[:30]
        return clean

    # ──────────────────────────────────────────────
    # Purchase API (Server-to-Server / USSD Flow)
    # ──────────────────────────────────────────────
    def purchase_request(self, phone, amount, method="ZAAD", invoice_id=None,
                         description="", currency="USD", transaction_log=None):
        """
        Initiate a Purchase API payment request (server-to-server).
        This triggers a USSD prompt on the customer's phone.

        Args:
            phone: Customer's wallet phone number
            amount: Payment amount
            method: Payment method (ZAAD, SAHAL, EVCPlus)
            invoice_id: Optional Sales Invoice reference
            description: Payment description
            currency: Currency code (default USD)
            transaction_log: Optional existing transaction log name

        Returns:
            dict with keys: success, transaction_id, provider_reference,
                           response_code, message, raw_response
        """
        phone = sanitize_phone_number(phone)
        channel_info = WAAFIPAY_CHANNELS.get(method)
        if not channel_info:
            return {
                "success": False,
                "message": f"Unsupported payment method: {method}. Valid: {list(WAAFIPAY_CHANNELS.keys())}",
            }
        reference = self._generate_reference()

        # Build WaafiPay API_PREAUTHORIZE payload
        # paymentMethod is always MWALLET_ACCOUNT per docs — the wallet type
        # (ZAAD/SAHAL/EVC) is identified by the phone number itself, not the channel.
        payload = {
            "schemaVersion": "1.0",
            "requestId": reference,
            "timestamp": now_datetime().isoformat(),
            "channelName": "WEB",
            "serviceName": "API_PREAUTHORIZE",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "apiUserId": int(self.api_user_id),
                "apiKey": self.api_key,
                "paymentMethod": channel_info["channel"],
                "payerInfo": {
                    "accountNo": phone,
                },
                "transactionInfo": {
                    # referenceId: 10-30 chars, alphanumeric/dash/underscore/dot only
                    "referenceId": self._make_reference_id(invoice_id or reference),
                    # amount must be numeric per spec (not a string)
                    "amount": round(float(amount), 2),
                    "currency": currency,
                    "description": description or f"Payment for {invoice_id}",
                },
            },
        }

        # Create or update transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone=phone,
            amount=amount,
            method=method,
            invoice_id=invoice_id,
            flow_type="Purchase API",
            reference=reference,
            currency=currency,
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"WaafiPay PREAUTHORIZE request: {reference} | "
                f"Phone: {phone} | Amount: {amount} {currency} | Method: {method}"
            )

            response = requests.post(
                self.base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            return self._process_purchase_response(data, log, reference)

        except requests.Timeout:
            # Attempt to cancel the preauth to release reserved funds
            # We don't have a transactionId yet on timeout, so log and move on
            error_msg = "Request timed out waiting for payment confirmation"
            log.update_status("Timeout", error_message=error_msg)
            frappe.logger("mobile_payments").warning(
                f"WaafiPay timeout on {reference} — preauth may still be pending on customer account"
            )
            return {
                "success": False,
                "message": "Payment request timed out. The customer may not have responded to the USSD prompt.",
                "transaction_log": log.name,
                "reference": reference,
            }

        except requests.RequestException as e:
            error_msg = f"WaafiPay API request failed: {str(e)}"
            frappe.log_error(message=error_msg, title="WaafiPay API Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            frappe.log_error(message=frappe.get_traceback(), title="WaafiPay Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

    def _process_purchase_response(self, data, log, reference):
        """Process WaafiPay PREAUTHORIZE response — auto-commit if APPROVED."""
        response_code = data.get("responseCode")
        response_msg = data.get("responseMsg", "")
        params = data.get("params", {})
        txn_id = params.get("transactionId", "")
        state = params.get("state", "")

        if response_code == "2001" and state in ("APPROVED", "RCS_SUCCESS"):
            # Auto-commit the preauthorized payment
            commit_ok, commit_data = self._commit_preauthorized_payment(txn_id)

            if commit_ok:
                log.update_status(
                    "Completed",
                    provider_transaction_id=txn_id,
                    response_payload=commit_data,
                )
                log.provider_reference = params.get("referenceId", reference)
                log.save(ignore_permissions=True)

                frappe.logger("mobile_payments").info(
                    f"WaafiPay payment COMMITTED: {reference} | TxnID: {txn_id}"
                )
                return {
                    "success": True,
                    "transaction_id": txn_id,
                    "provider_reference": params.get("referenceId", reference),
                    "response_code": response_code,
                    "message": "Payment successful",
                    "transaction_log": log.name,
                    "reference": reference,
                    "raw_response": commit_data,
                }
            else:
                # Commit failed — cancel the preauth so customer funds are released
                self._cancel_preauthorized_payment(
                    txn_id,
                    description="Auto-cancel: commit failed"
                )
                error_msg = f"Preauthorize commit failed: {commit_data.get('responseMsg', 'Unknown')}"
                log.update_status("Failed", error_message=error_msg, response_payload=commit_data)
                return {
                    "success": False,
                    "message": error_msg,
                    "transaction_log": log.name,
                    "reference": reference,
                }

        elif state == "PENDING":
            log.update_status("Pending", response_payload=data)
            return {
                "success": False,
                "pending": True,
                "message": "Payment is pending customer authorization",
                "transaction_log": log.name,
                "reference": reference,
                "raw_response": data,
            }

        else:
            error_msg = f"Payment declined: {response_msg} (Code: {response_code})"
            log.update_status(
                "Failed",
                error_message=error_msg,
                error_code=response_code,
                response_payload=data,
            )
            return {
                "success": False,
                "message": error_msg,
                "response_code": response_code,
                "transaction_log": log.name,
                "reference": reference,
                "raw_response": data,
            }

    def _commit_preauthorized_payment(self, transaction_id):
        """Commit a preauthorized WaafiPay transaction (API_PREAUTHORIZE_COMMIT)."""
        import datetime
        payload = {
            "schemaVersion": "1.0",
            "requestId": self._generate_reference(),
            "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channelName": "WEB",
            "serviceName": "API_PREAUTHORIZE_COMMIT",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "apiUserId": int(self.api_user_id),
                "apiKey": self.api_key,
                "transactionId": transaction_id,
                "description": f"Commit for transaction {transaction_id}",
            },
        }
        try:
            response = requests.post(
                self.base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            success = data.get("responseCode") == "2001" or data.get("responseMsg") == "RCS_SUCCESS"
            return success, data
        except Exception as e:
            frappe.log_error(message=str(e), title="WaafiPay Commit Error")
            return False, {"responseMsg": str(e)}

    def _cancel_preauthorized_payment(self, transaction_id, description="Payment cancelled"):
        """
        Cancel a preauthorized WaafiPay transaction (API_PREAUTHORIZE_CANCEL).
        Call this on timeout or failure to release the customer's reserved funds.
        """
        import datetime
        payload = {
            "schemaVersion": "1.0",
            "requestId": self._generate_reference(),
            "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channelName": "WEB",
            "serviceName": "API_PREAUTHORIZE_CANCEL",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "apiUserId": int(self.api_user_id),
                "apiKey": self.api_key,
                "transactionId": transaction_id,
                "description": description,
            },
        }
        try:
            response = requests.post(
                self.base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            success = data.get("responseCode") == "2001"
            frappe.logger("mobile_payments").info(
                f"WaafiPay CANCEL {'OK' if success else 'FAILED'}: txn={transaction_id}"
            )
            return success, data
        except Exception as e:
            frappe.log_error(message=str(e), title="WaafiPay Cancel Error")
            return False, {"responseMsg": str(e)}

    # ──────────────────────────────────────────────
    # Hosted Payment Page (HPP) Flow
    # ──────────────────────────────────────────────
    def create_hpp_session(self, amount, invoice_id=None, description="",
                           currency="USD", return_url=None, cancel_url=None,
                           transaction_log=None):
        """
        Create a Hosted Payment Page session.
        Returns a URL to redirect the customer to for payment.

        Args:
            amount: Payment amount
            invoice_id: Optional Sales Invoice reference
            description: Payment description
            currency: Currency code
            return_url: URL to redirect after successful payment
            cancel_url: URL to redirect on cancellation
            transaction_log: Optional existing transaction log name

        Returns:
            dict with keys: success, hpp_url, session_id, reference, transaction_log
        """
        reference = self._generate_reference()
        settings = self.settings

        # Build callback URLs
        base_callback = settings.get_callback_url("")
        if not return_url:
            return_url = f"{base_callback}api/method/mobile_payments.api.webhooks.waafipay_hpp_return"
        if not cancel_url:
            cancel_url = return_url

        # Per docs: HPP_PURCHASE serviceParams uses merchantUid/storeId/hppKey for auth.
        # apiUserId and apiKey are NOT part of the HPP spec — including them can cause rejection.
        # storeId and amount must be numeric (not strings).
        # transactionInfo does NOT include invoiceId per the spec.
        payload = {
            "schemaVersion": "1.0",
            "requestId": reference,
            "timestamp": now_datetime().isoformat(),
            "channelName": "WEB",
            "serviceName": "HPP_PURCHASE",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "storeId": int(self.store_id) if self.store_id else self.store_id,
                "hppKey": self.hpp_key,
                "paymentMethod": "MWALLET_ACCOUNT",
                "hppSuccessCallbackUrl": return_url,
                "hppFailureCallbackUrl": cancel_url,
                "hppRespDataFormat": 1,
                "transactionInfo": {
                    "referenceId": invoice_id or reference,
                    "amount": float(amount),
                    "currency": currency,
                    "description": description or f"Payment for {invoice_id}",
                },
            },
        }

        # Create transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone="HPP",
            amount=amount,
            method="WaafiPay",
            invoice_id=invoice_id,
            flow_type="Hosted Payment Page (HPP)",
            reference=reference,
            currency=currency,
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"WaafiPay HPP request: {reference} | Amount: {amount} {currency}"
            )

            # HPP_PURCHASE API call goes to the SAME /asm endpoint as all other calls.
            # hpp_base_url is only the domain for the customer-facing redirect URL.
            response = requests.post(
                self.base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            response_code = data.get("responseCode")
            params = data.get("params", {})
            # Prefer hppUrl; fall back to directPaymentLink (both are valid per docs)
            hpp_url = params.get("hppUrl") or params.get("directPaymentLink", "")
            session_id = params.get("orderId", "")  # docs return orderId, not sessionId

            if response_code == "2001" and hpp_url:
                log.update_status("Pending", response_payload=data)
                log.set_hpp_url(hpp_url)

                return {
                    "success": True,
                    "hpp_url": hpp_url,
                    "session_id": session_id,
                    "reference": reference,
                    "transaction_log": log.name,
                }
            else:
                error_msg = data.get("responseMsg", "Failed to create HPP session")
                log.update_status("Failed", error_message=error_msg, response_payload=data)
                return {
                    "success": False,
                    "message": error_msg,
                    "transaction_log": log.name,
                    "reference": reference,
                }

        except Exception as e:
            error_msg = f"HPP session creation failed: {str(e)}"
            frappe.log_error(message=frappe.get_traceback(), title="WaafiPay HPP Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

    # ──────────────────────────────────────────────
    # Status Check / Polling
    # ──────────────────────────────────────────────
    def check_transaction_status(self, reference_id):
        """
        Check the status of a transaction via WaafiPay API.

        Args:
            reference_id: The original transaction reference ID

        Returns:
            dict with transaction status details
        """
        payload = {
            "schemaVersion": "1.0",
            "requestId": self._generate_reference(),
            "timestamp": now_datetime().isoformat(),
            "channelName": "WEB",
            "serviceName": "API_TXNSTATUS",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "storeId": self.store_id,
                "apiUserId": int(self.api_user_id),
                "apiKey": self.api_key,
                "transactionId": reference_id,
            },
        }

        try:
            response = requests.post(
                self.base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            params = data.get("params", {})
            state = params.get("state", "")

            return {
                "success": True,
                "state": state,
                "transaction_id": params.get("transactionId"),
                "response_code": data.get("responseCode"),
                "raw_response": data,
            }

        except Exception as e:
            return {
                "success": False,
                "message": f"Status check failed: {str(e)}",
            }

    # ──────────────────────────────────────────────
    # Helper Methods
    # ──────────────────────────────────────────────
    def _get_or_create_log(self, transaction_log=None, phone="", amount=0,
                           method="", invoice_id=None, flow_type="Purchase API",
                           reference="", currency="USD"):
        """Get existing or create new transaction log entry."""
        if transaction_log:
            return frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

        log = frappe.get_doc(
            {
                "doctype": "Mobile Payment Transaction Log",
                "provider": "WaafiPay",
                "payment_method": method if method in ("ZAAD", "SAHAL", "EVCPlus") else "",
                "flow_type": flow_type,
                "status": "Initiated",
                "amount": amount,
                "currency": currency or "USD",
                "phone_number": phone,
                "sales_invoice": invoice_id,
                "initiated_at": now_datetime(),
            }
        )
        log.insert(ignore_permissions=True)
        frappe.db.commit()
        return log


# ──────────────────────────────────────────────
# Frappe Whitelisted API Endpoints
# ──────────────────────────────────────────────

@frappe.whitelist()
def initiate_waafipay_payment(phone, amount, method="ZAAD", invoice_id=None,
                               description="", currency="USD"):
    """
    Whitelisted API endpoint to initiate a WaafiPay payment.
    Called from frontend JS.

    Args:
        phone: Customer phone number
        amount: Payment amount
        method: Payment method (ZAAD, SAHAL, EVCPlus)
        invoice_id: Sales Invoice name
        description: Payment description
        currency: Currency code (USD or SLSH)

    Returns:
        Payment result dict
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    # Auto-detect currency from the Sales Invoice if not explicitly specified.
    # This prevents the bug where the salesrep selects SLSH on a USD invoice
    # and the API receives 10 SLSH instead of 10 USD.
    if not currency and invoice_id and frappe.db.exists("Sales Invoice", invoice_id):
        currency = frappe.db.get_value("Sales Invoice", invoice_id, "currency") or "USD"
    currency = currency or "USD"

    client = WaafiPayClient()
    result = client.purchase_request(
        phone=phone,
        amount=float(amount),
        method=method,
        invoice_id=invoice_id,
        description=description,
        currency=currency,
    )

    # If successful, trigger Payment Entry creation
    if result.get("success"):
        from mobile_payments.utils.payment_handler import process_successful_payment
        frappe.enqueue(
            process_successful_payment,
            queue="short",
            transaction_log=result.get("transaction_log"),
            invoice_id=invoice_id,
            now=frappe.conf.developer_mode,
        )

    return result


@frappe.whitelist()
def initiate_waafipay_hpp(amount, invoice_id=None, description="", currency="USD"):
    """
    Whitelisted API endpoint to create a WaafiPay HPP session.

    Args:
        amount: Payment amount
        invoice_id: Sales Invoice name
        description: Payment description
        currency: Currency code (USD or SLSH)

    Returns:
        HPP session details with redirect URL
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    # Auto-detect currency from the Sales Invoice
    if not currency and invoice_id and frappe.db.exists("Sales Invoice", invoice_id):
        currency = frappe.db.get_value("Sales Invoice", invoice_id, "currency") or "USD"
    currency = currency or "USD"

    client = WaafiPayClient()
    return client.create_hpp_session(
        amount=float(amount),
        invoice_id=invoice_id,
        description=description,
        currency=currency,
    )


@frappe.whitelist()
def check_waafipay_status(reference_id):
    """
    Check WaafiPay transaction status.

    Args:
        reference_id: Transaction reference ID

    Returns:
        Transaction status details
    """
    frappe.has_permission("Sales Invoice", "read", throw=True)
    client = WaafiPayClient()
    return client.check_transaction_status(reference_id)
