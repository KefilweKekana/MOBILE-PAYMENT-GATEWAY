"""
Edahab API Integration
Supports both Purchase API (server-to-server) and Hosted Payment Page (HPP) flows.
Independent API integration for Edahab mobile money.
"""
from __future__ import unicode_literals

import hashlib
import json
import uuid

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from mobile_payments.utils.security import sanitize_phone_number


class EdahabClient:
    """
    Edahab API client for Purchase API and HPP flows.

    Usage:
        client = EdahabClient()
        result = client.purchase_request(
            phone="252652345678",
            amount=10.00,
            invoice_id="SINV-00001",
            description="Payment for Invoice SINV-00001"
        )
    """

    def __init__(self):
        """Initialize with credentials from Mobile Payment Settings."""
        self.settings = frappe.get_single("Mobile Payment Settings")
        self.credentials = self.settings.get_edahab_credentials()
        self.base_url = self.credentials["base_url"]
        self.hpp_base_url = self.credentials["hpp_base_url"]
        self.api_key = self.credentials["api_key"]
        self.api_secret = self.credentials["api_secret"]
        self.agent_code = self.credentials.get("agent_code", "")
        self.timeout = self.settings.transaction_timeout or 120

    def _get_headers(self):
        """Return standard request headers."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _generate_reference(self):
        """Generate unique reference ID."""
        return uuid.uuid4().hex[:12].upper()

    def _generate_hash(self, params_string):
        """
        Generate the Edahab API request hash.
        Edahab uses SHA256(params + secret) for request signing.

        Args:
            params_string: Concatenated parameter string

        Returns:
            SHA256 hash hex string
        """
        hash_input = params_string + self.api_secret
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    # ──────────────────────────────────────────────
    # Purchase API (Server-to-Server / USSD Flow)
    # ──────────────────────────────────────────────
    def purchase_request(self, phone, amount, invoice_id=None,
                         description="", currency="USD", transaction_log=None):
        """
        Initiate an Edahab Purchase API payment (server-to-server).
        Sends a USSD push to the customer's Edahab wallet.

        Args:
            phone: Customer's Edahab wallet phone number
            amount: Payment amount
            invoice_id: Optional Sales Invoice reference
            description: Payment description
            currency: Currency code
            transaction_log: Optional existing transaction log name

        Returns:
            dict with keys: success, transaction_id, message, transaction_log, reference
        """
        phone = sanitize_phone_number(phone)
        reference = self._generate_reference()

        # Edahab API expects a specific payload format
        payload = {
            "apiKey": self.api_key,
            "edahabNumber": phone,
            "amount": float(amount),
            "currency": currency,
            "agentCode": self.agent_code,
            "description": description or f"Payment for {invoice_id}",
        }

        # Generate request hash for authentication
        # Hash = SHA256(apiKey + edahabNumber + amount + currency + agentCode + secret)
        hash_string = (
            f"{self.api_key}{phone}{amount}{currency}{self.agent_code}"
        )
        request_hash = self._generate_hash(hash_string)
        payload["hash"] = request_hash

        # Create transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone=phone,
            amount=amount,
            invoice_id=invoice_id,
            flow_type="Purchase API",
            reference=reference,
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"Edahab Purchase API request: {reference} | "
                f"Phone: {phone} | Amount: {amount} {currency}"
            )

            api_url = f"{self.base_url}/api/issueinvoice"
            response = requests.post(
                api_url,
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
                error_message="Request timed out waiting for Edahab response",
            )
            return {
                "success": False,
                "message": "Payment request timed out",
                "transaction_log": log.name,
                "reference": reference,
            }

        except requests.RequestException as e:
            error_msg = f"Edahab API request failed: {str(e)}"
            frappe.log_error(message=error_msg, title="Edahab API Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            frappe.log_error(message=frappe.get_traceback(), title="Edahab Error")
            log.update_status("Failed", error_message=error_msg)
            return {
                "success": False,
                "message": error_msg,
                "transaction_log": log.name,
                "reference": reference,
            }

    def _process_purchase_response(self, data, log, reference):
        """Process Edahab Purchase API response."""
        # Edahab returns different response structures
        response_code = data.get("ResponseCode", data.get("responseCode", 0))
        transaction_id = data.get("TransactionId", data.get("transactionId", ""))
        invoice_id = data.get("InvoiceId", data.get("invoiceId", ""))
        status_text = data.get("StatusDescription", data.get("statusDescription", ""))

        # Edahab success: ResponseCode = 0 typically means success
        if response_code == 0 or str(response_code) == "0":
            # For Edahab purchase API, the payment may still be pending
            # customer USSD confirmation
            if data.get("TransactionStatus") == "Approved" or data.get("StatusCode") == 0:
                log.update_status(
                    "Completed",
                    provider_transaction_id=str(transaction_id),
                    response_payload=data,
                )
                log.provider_reference = str(invoice_id or reference)
                log.save(ignore_permissions=True)

                frappe.logger("mobile_payments").info(
                    f"Edahab payment APPROVED: {reference} | TxnID: {transaction_id}"
                )

                return {
                    "success": True,
                    "transaction_id": str(transaction_id),
                    "provider_reference": str(invoice_id),
                    "response_code": response_code,
                    "message": "Payment successful",
                    "transaction_log": log.name,
                    "reference": reference,
                    "raw_response": data,
                }

            else:
                # Payment initiated, waiting for USSD confirmation
                log.update_status("Pending", response_payload=data)
                log.provider_reference = str(invoice_id or reference)
                log.provider_transaction_id = str(transaction_id) if transaction_id else ""
                log.save(ignore_permissions=True)

                return {
                    "success": False,
                    "pending": True,
                    "invoice_id": str(invoice_id),
                    "transaction_id": str(transaction_id),
                    "message": "Payment initiated. Waiting for customer confirmation via USSD.",
                    "transaction_log": log.name,
                    "reference": reference,
                    "raw_response": data,
                }

        else:
            error_msg = f"Payment failed: {status_text} (Code: {response_code})"
            log.update_status(
                "Failed",
                error_message=error_msg,
                error_code=str(response_code),
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
                           currency="USD", return_url=None, transaction_log=None):
        """
        Create an Edahab Hosted Payment Page session.

        Args:
            amount: Payment amount
            invoice_id: Optional Sales Invoice reference
            description: Payment description
            currency: Currency code
            return_url: URL to redirect after payment
            transaction_log: Optional existing transaction log name

        Returns:
            dict with keys: success, hpp_url, invoice_id, reference, transaction_log
        """
        reference = self._generate_reference()
        settings = self.settings

        # Build callback URL
        base_callback = settings.get_callback_url("")
        if not return_url:
            return_url = (
                f"{base_callback}api/method/"
                f"mobile_payments.api.webhooks.edahab_hpp_return"
            )

        payload = {
            "apiKey": self.api_key,
            "EdahabNumber": "",  # Left blank for HPP - customer enters on page
            "Amount": float(amount),
            "Currency": currency,
            "AgentCode": self.agent_code,
            "ReturnUrl": return_url,
            "Description": description or f"Payment for {invoice_id}",
        }

        # Generate hash
        hash_string = (
            f"{self.api_key}{amount}{currency}{self.agent_code}"
        )
        request_hash = self._generate_hash(hash_string)
        payload["Hash"] = request_hash

        # Create transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone="HPP",
            amount=amount,
            invoice_id=invoice_id,
            flow_type="Hosted Payment Page (HPP)",
            reference=reference,
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"Edahab HPP request: {reference} | Amount: {amount} {currency}"
            )

            api_url = f"{self.hpp_base_url}/api/api/IssueInvoice"
            response = requests.post(
                api_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            edahab_invoice_id = data.get("InvoiceId", data.get("invoiceId", ""))
            response_code = data.get("ResponseCode", data.get("responseCode", -1))

            if response_code == 0 or str(response_code) == "0":
                # Construct HPP URL
                hpp_url = (
                    f"{self.hpp_base_url}/payment?invoiceId={edahab_invoice_id}"
                )

                log.update_status("Pending", response_payload=data)
                log.set_hpp_url(hpp_url)
                log.provider_reference = str(edahab_invoice_id)
                log.save(ignore_permissions=True)

                return {
                    "success": True,
                    "hpp_url": hpp_url,
                    "invoice_id": str(edahab_invoice_id),
                    "reference": reference,
                    "transaction_log": log.name,
                }
            else:
                error_msg = data.get(
                    "StatusDescription",
                    data.get("statusDescription", "Failed to create HPP session"),
                )
                log.update_status("Failed", error_message=error_msg, response_payload=data)
                return {
                    "success": False,
                    "message": error_msg,
                    "transaction_log": log.name,
                    "reference": reference,
                }

        except Exception as e:
            error_msg = f"Edahab HPP session creation failed: {str(e)}"
            frappe.log_error(message=frappe.get_traceback(), title="Edahab HPP Error")
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
    def check_transaction_status(self, invoice_id):
        """
        Check the status of an Edahab transaction.

        Args:
            invoice_id: The Edahab invoice ID

        Returns:
            dict with transaction status details
        """
        payload = {
            "apiKey": self.api_key,
            "invoiceId": invoice_id,
        }

        hash_string = f"{self.api_key}{invoice_id}"
        payload["hash"] = self._generate_hash(hash_string)

        try:
            api_url = f"{self.base_url}/api/checkInvoiceStatus"
            response = requests.post(
                api_url,
                json=payload,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            status_code = data.get("StatusCode", data.get("statusCode", -1))
            invoice_status = data.get("InvoiceStatus", data.get("invoiceStatus", ""))

            return {
                "success": True,
                "status_code": status_code,
                "invoice_status": invoice_status,
                "transaction_id": data.get("TransactionId", ""),
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
                           invoice_id=None, flow_type="Purchase API",
                           reference=""):
        """Get existing or create new transaction log entry."""
        if transaction_log:
            return frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

        log = frappe.get_doc(
            {
                "doctype": "Mobile Payment Transaction Log",
                "provider": "Edahab",
                "payment_method": "Edahab",
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
def initiate_edahab_payment(phone, amount, invoice_id=None, description="",
                             currency="USD"):
    """
    Whitelisted API endpoint to initiate an Edahab payment.

    Args:
        phone: Customer phone number
        amount: Payment amount
        invoice_id: Sales Invoice name
        description: Payment description
        currency: Currency code (USD or SLSH)

    Returns:
        Payment result dict
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    client = EdahabClient()
    result = client.purchase_request(
        phone=phone,
        amount=float(amount),
        invoice_id=invoice_id,
        description=description,
        currency=currency or "USD",
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
def initiate_edahab_hpp(amount, invoice_id=None, description="", currency="USD"):
    """
    Whitelisted API endpoint to create an Edahab HPP session.

    Args:
        amount: Payment amount
        invoice_id: Sales Invoice name
        description: Payment description
        currency: Currency code (USD or SLSH)

    Returns:
        HPP session details with redirect URL
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    client = EdahabClient()
    return client.create_hpp_session(
        amount=float(amount),
        invoice_id=invoice_id,
        description=description,
        currency=currency or "USD",
    )


@frappe.whitelist()
def check_edahab_status(invoice_id):
    """
    Check Edahab transaction status.

    Args:
        invoice_id: Edahab invoice ID

    Returns:
        Transaction status details
    """
    frappe.has_permission("Sales Invoice", "read", throw=True)
    client = EdahabClient()
    return client.check_transaction_status(invoice_id)
