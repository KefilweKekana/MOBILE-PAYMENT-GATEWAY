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
WAAFIPAY_CHANNELS = {
    "ZAAD": {"channel": "MWALLET_ACCOUNT", "provider": "ZAAD"},
    "SAHAL": {"channel": "MWALLET_ACCOUNT", "provider": "SAHAL"},
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
        self.merchant_uid = self.credentials["merchant_uid"]
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
                "message": f"Unsupported payment method: {method}",
            }

        reference = self._generate_reference()

        # Build WaafiPay API request payload
        payload = {
            "schemaVersion": "1.0",
            "requestId": reference,
            "timestamp": now_datetime().isoformat(),
            "channelName": "WEB",
            "serviceName": "API_PURCHASE",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "apiUserId": self.api_user_id,
                "apiKey": self.api_key,
                "paymentMethod": channel_info["channel"],
                "payerInfo": {
                    "accountNo": phone,
                },
                "transactionInfo": {
                    "referenceId": reference,
                    "invoiceId": invoice_id or reference,
                    "amount": str(amount),
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
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"WaafiPay Purchase API request: {reference} | "
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
            log.update_status(
                "Timeout",
                error_message="Request timed out waiting for payment confirmation",
            )
            return {
                "success": False,
                "message": "Payment request timed out. The customer may not have responded.",
                "transaction_log": log.name,
                "reference": reference,
            }

        except requests.RequestException as e:
            error_msg = f"WaafiPay API request failed: {str(e)}"
            frappe.log_error(error_msg, "WaafiPay API Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            frappe.log_error(frappe.get_traceback(), "WaafiPay Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

    def _process_purchase_response(self, data, log, reference):
        """Process WaafiPay Purchase API response."""
        response_code = data.get("responseCode")
        response_msg = data.get("responseMsg", "")
        params = data.get("params", {})
        txn_id = params.get("transactionId", "")
        state = params.get("state", "")

        # WaafiPay success response codes
        if response_code == "2001" and state == "APPROVED":
            log.update_status(
                "Completed",
                provider_transaction_id=txn_id,
                response_payload=data,
            )
            log.provider_reference = params.get("referenceId", reference)
            log.save(ignore_permissions=True)

            frappe.logger("mobile_payments").info(
                f"WaafiPay payment APPROVED: {reference} | TxnID: {txn_id}"
            )

            return {
                "success": True,
                "transaction_id": txn_id,
                "provider_reference": params.get("referenceId", reference),
                "response_code": response_code,
                "message": "Payment successful",
                "transaction_log": log.name,
                "reference": reference,
                "raw_response": data,
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

        payload = {
            "schemaVersion": "1.0",
            "requestId": reference,
            "timestamp": now_datetime().isoformat(),
            "channelName": "WEB",
            "serviceName": "HPP_PURCHASE",
            "serviceParams": {
                "merchantUid": self.merchant_uid,
                "apiUserId": self.api_user_id,
                "apiKey": self.api_key,
                "transactionInfo": {
                    "referenceId": reference,
                    "invoiceId": invoice_id or reference,
                    "amount": str(amount),
                    "currency": currency,
                    "description": description or f"Payment for {invoice_id}",
                },
                "hppInfo": {
                    "returnUrl": return_url,
                    "cancelUrl": cancel_url,
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
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"WaafiPay HPP request: {reference} | Amount: {amount} {currency}"
            )

            response = requests.post(
                self.hpp_base_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            response_code = data.get("responseCode")
            params = data.get("params", {})
            hpp_url = params.get("hppUrl", "")
            session_id = params.get("sessionId", "")

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
            frappe.log_error(frappe.get_traceback(), "WaafiPay HPP Error")
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
                "apiUserId": self.api_user_id,
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
                           reference=""):
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
                "currency": "USD",
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
                               description=""):
    """
    Whitelisted API endpoint to initiate a WaafiPay payment.
    Called from frontend JS.

    Args:
        phone: Customer phone number
        amount: Payment amount
        method: Payment method (ZAAD, SAHAL, EVCPlus)
        invoice_id: Sales Invoice name
        description: Payment description

    Returns:
        Payment result dict
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    client = WaafiPayClient()
    result = client.purchase_request(
        phone=phone,
        amount=float(amount),
        method=method,
        invoice_id=invoice_id,
        description=description,
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
def initiate_waafipay_hpp(amount, invoice_id=None, description=""):
    """
    Whitelisted API endpoint to create a WaafiPay HPP session.

    Args:
        amount: Payment amount
        invoice_id: Sales Invoice name
        description: Payment description

    Returns:
        HPP session details with redirect URL
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    client = WaafiPayClient()
    return client.create_hpp_session(
        amount=float(amount),
        invoice_id=invoice_id,
        description=description,
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
