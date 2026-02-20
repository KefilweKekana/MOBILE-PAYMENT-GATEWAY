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
        self.base_url = (self.credentials["base_url"] or "").strip().rstrip("/").removesuffix("/api").removesuffix("/API")
        self.hpp_base_url = (self.credentials["hpp_base_url"] or "").strip().rstrip("/")
        self.api_key = (self.credentials["api_key"] or "").strip()
        self.api_secret = (self.credentials["api_secret"] or "").strip()
        self.agent_code = (self.credentials.get("agent_code", "") or "").strip()
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

    def _generate_hash(self, payload_dict):
        """
        Generate the Edahab API request hash.
        Per the official Edahab C# reference:
            hash = SHA256(JsonSerializer.Serialize(requestBody) + secretKey)

        The payload is serialised with compact separators (',', ':') to match
        C#'s System.Text.Json / JavaScript's JSON.stringify output.

        Args:
            payload_dict: The request payload dict (will be JSON-serialised)

        Returns:
            tuple(hash_hex, json_body_bytes)
        """
        body_string = json.dumps(payload_dict, separators=(",", ":"))
        hash_input = body_string + self.api_secret
        hash_hex = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        body_bytes = body_string.encode("utf-8")

        frappe.logger("mobile_payments").info(
            f"Edahab hash debug | body_len={len(body_string)} "
            f"hash={hash_hex[:16]}... | body_preview={body_string[:120]}"
        )

        return hash_hex, body_bytes

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

        # Convert amount: use int for whole numbers to match JS JSON.stringify
        amount_val = int(float(amount)) if float(amount) == int(float(amount)) else float(amount)

        # Edahab C# reference field names (camelCase) – must match for hash
        payload = {
            "apiKey": self.api_key,
            "edahabNumber": phone,
            "amount": amount_val,
            "agentCode": int(self.agent_code),
            "currency": currency,
        }

        # Per official Edahab C# reference: hash = SHA256(JSON(payload) + secret)
        # Hash is sent as a URL query parameter, NOT in the body.
        request_hash, body_bytes = self._generate_hash(payload)

        # Create transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone=phone,
            amount=amount,
            invoice_id=invoice_id,
            flow_type="Purchase API",
            reference=reference,
            currency=currency,
        )
        log.log_request(payload)

        try:
            frappe.logger("mobile_payments").info(
                f"Edahab Purchase API request: {reference} | "
                f"Phone: {phone} | Amount: {amount} {currency}"
            )

            api_url = f"{self.base_url}/IssueInvoice?hash={request_hash}"
            frappe.logger("mobile_payments").info(
                f"Edahab Purchase POST → {api_url}"
            )
            response = requests.post(
                api_url,
                data=body_bytes,
                headers=self._get_headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            frappe.logger("mobile_payments").info(
                f"Edahab Purchase response: {data}"
            )

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
        # Per C# reference model: StatusCode (0-6, 0=success),
        # StatusDescription, InvoiceId, TransactionId, InvoiceStatus
        status_code = data.get("StatusCode", data.get("statusCode",
                     data.get("ResponseCode", data.get("responseCode", -1))))
        transaction_id = data.get("TransactionId", data.get("transactionId", ""))
        invoice_id = data.get("InvoiceId", data.get("invoiceId", ""))
        status_text = data.get("StatusDescription", data.get("statusDescription", ""))
        invoice_status = data.get("InvoiceStatus", data.get("invoiceStatus", ""))

        frappe.logger("mobile_payments").info(
            f"Edahab response parsed | StatusCode={status_code} "
            f"InvoiceStatus={invoice_status} StatusDesc={status_text}"
        )

        # Edahab success: StatusCode = 0 (per C# reference: 0-6 where 0 is success)
        if int(status_code) == 0:
            # Check if payment is already confirmed
            if invoice_status == "Paid" or data.get("TransactionStatus") == "Approved":
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
                    "status_code": status_code,
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
            error_msg = f"Payment failed: {status_text} (StatusCode: {status_code})"
            log.update_status(
                "Failed",
                error_message=error_msg,
                error_code=str(status_code),
                response_payload=data,
            )
            return {
                "success": False,
                "message": error_msg,
                "status_code": status_code,
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

        # Convert amount: use int for whole numbers to match JS JSON.stringify
        amount_val = int(float(amount)) if float(amount) == int(float(amount)) else float(amount)

        # HPP payload — same structure as purchase but edahabNumber is empty.
        # Edahab detects HPP mode from the empty edahabNumber field.
        # DO NOT include ReturnUrl in the body — it is not in Edahab's C# model
        # so including it changes our hash vs the server's hash → StatusCode 4.
        # The customer redirect is constructed from the returned InvoiceId.
        payload = {
            "apiKey": self.api_key,
            "edahabNumber": "",
            "amount": amount_val,
            "agentCode": int(self.agent_code),
            "currency": currency,
        }

        # Per Edahab C# reference: hash = SHA256(JSON(payload) + secret)
        # Hash is sent as a URL query parameter, NOT in the body.
        request_hash, body_bytes = self._generate_hash(payload)

        # Create transaction log
        log = self._get_or_create_log(
            transaction_log=transaction_log,
            phone="HPP",
            amount=amount,
            invoice_id=invoice_id,
            flow_type="Hosted Payment Page (HPP)",
            reference=reference,
            currency=currency,
        )
        log.log_request(payload)

        try:
            api_url = f"{self.base_url}/IssueInvoice?hash={request_hash}"  # Same endpoint as purchase; hpp_base_url is only for the customer redirect
            frappe.logger("mobile_payments").info(
                f"Edahab HPP POST → {api_url} | ref={reference} | "
                f"Amount: {amount} {currency} | agentCode={self.agent_code}"
            )

            response = requests.post(
                api_url,
                data=body_bytes,
                headers=self._get_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            frappe.logger("mobile_payments").info(
                f"Edahab HPP response: {data}"
            )

            edahab_invoice_id = data.get("InvoiceId", data.get("invoiceId", ""))
            # Per C# model: field is StatusCode (0-6), NOT ResponseCode
            status_code = data.get("StatusCode", data.get("statusCode",
                         data.get("ResponseCode", data.get("responseCode", -1))))

            if int(status_code) == 0:
                # Construct HPP URL
                hpp_url = (
                    f"{self.hpp_base_url}/API/Payment?invoiceId={edahab_invoice_id}"
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
                status_desc = data.get(
                    "StatusDescription",
                    data.get("statusDescription", "Failed to create HPP session"),
                )
                validation_errors = data.get("ValidationErrors", [])
                error_msg = (
                    f"{status_desc} (StatusCode: {status_code})"
                    + (f" | Validation: {validation_errors}" if validation_errors else "")
                )
                frappe.logger("mobile_payments").error(
                    f"Edahab HPP FAILED | StatusCode={status_code} | "
                    f"Desc={status_desc} | ValidationErrors={validation_errors} | "
                    f"Full response: {data}"
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

        # Per official Edahab SDK: hash = SHA256(JSON(payload) + secret)
        request_hash, body_bytes = self._generate_hash(payload)

        try:
            api_url = f"{self.base_url}/CheckInvoiceStatus?hash={request_hash}"
            response = requests.post(
                api_url,
                data=body_bytes,
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
                           reference="", currency="USD"):
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

    # Auto-detect currency from the Sales Invoice if not explicitly specified.
    # This prevents sending wrong currency amounts (e.g. 10 SLSH on a 10 USD invoice).
    if not currency and invoice_id and frappe.db.exists("Sales Invoice", invoice_id):
        currency = frappe.db.get_value("Sales Invoice", invoice_id, "currency") or "USD"
    currency = currency or "USD"

    client = EdahabClient()
    result = client.purchase_request(
        phone=phone,
        amount=float(amount),
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

    # Auto-detect currency from the Sales Invoice
    if not currency and invoice_id and frappe.db.exists("Sales Invoice", invoice_id):
        currency = frappe.db.get_value("Sales Invoice", invoice_id, "currency") or "USD"
    currency = currency or "USD"

    client = EdahabClient()
    return client.create_hpp_session(
        amount=float(amount),
        invoice_id=invoice_id,
        description=description,
        currency=currency,
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


@frappe.whitelist()
def debug_edahab_hash():
    """
    Diagnostic endpoint: shows the exact JSON body, hash, and URL
    that would be sent to Edahab, WITHOUT actually calling the API.
    Use from browser console:
        frappe.call({method: 'mobile_payments.api.edahab.debug_edahab_hash',
                     callback: r => console.log(r.message)})
    """
    import hashlib as hl

    frappe.has_permission("Mobile Payment Settings", "read", throw=True)

    settings = frappe.get_single("Mobile Payment Settings")
    creds = settings.get_edahab_credentials()

    api_key = (creds["api_key"] or "").strip()
    api_secret = (creds["api_secret"] or "").strip()
    agent_code = (creds.get("agent_code", "") or "").strip()
    base_url = (creds["base_url"] or "").strip().rstrip("/")
    hpp_base_url = (creds["hpp_base_url"] or "").strip().rstrip("/")

    # Build a test Purchase payload (matches C# reference field order)
    purchase_payload = {
        "apiKey": api_key,
        "edahabNumber": "252000000000",
        "amount": 1,
        "agentCode": int(agent_code),
        "currency": "USD",
    }
    purchase_body = json.dumps(purchase_payload, separators=(",", ":"))
    purchase_hash = hl.sha256(
        (purchase_body + api_secret).encode("utf-8")
    ).hexdigest()

    # Build a test HPP payload
    hpp_payload = {
        "apiKey": api_key,
        "edahabNumber": "",
        "amount": 1,
        "agentCode": int(agent_code),
        "currency": "USD",
        "ReturnUrl": "https://example.com/callback",
    }
    hpp_body = json.dumps(hpp_payload, separators=(",", ":"))
    hpp_hash = hl.sha256(
        (hpp_body + api_secret).encode("utf-8")
    ).hexdigest()

    return {
        "api_key_len": len(api_key),
        "api_key_preview": api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else api_key,
        "secret_len": len(api_secret),
        "secret_preview": api_secret[:4] + "..." + api_secret[-4:] if len(api_secret) > 8 else api_secret,
        "agent_code": agent_code,
        "base_url": base_url,
        "hpp_base_url": hpp_base_url,
        "purchase": {
            "body": purchase_body,
            "hash": purchase_hash,
            "url": f"{base_url}/IssueInvoice?hash={purchase_hash}",
        },
        "hpp": {
            "body": hpp_body,
            "hash": hpp_hash,
            "url": f"{base_url}/IssueInvoice?hash={hpp_hash}",
        },
    }
