"""
Mobile Payment Transaction Log
Tracks all mobile payment transactions with full audit trail.
"""
from __future__ import unicode_literals

import json
import uuid

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, add_to_date


class MobilePaymentTransactionLog(Document):
    """Audit log for every mobile payment transaction."""

    def before_insert(self):
        """Generate unique transaction ID before insert."""
        if not self.transaction_id:
            self.transaction_id = self._generate_transaction_id()
        if not self.initiated_at:
            self.initiated_at = now_datetime()

    def validate(self):
        """Validate transaction data."""
        self._validate_phone_number()
        self._validate_amount()

    def _generate_transaction_id(self):
        """Generate a unique transaction reference ID."""
        prefix = "WP" if self.provider == "WaafiPay" else "ED"
        short_uuid = uuid.uuid4().hex[:8].upper()
        return f"{prefix}-{frappe.utils.today().replace('-', '')}-{short_uuid}"

    def _validate_phone_number(self):
        """Validate phone number format."""
        if self.phone_number:
            # Skip validation for HPP flow placeholder
            if self.phone_number.upper() == "HPP":
                return

            # Remove spaces and dashes
            phone = self.phone_number.replace(" ", "").replace("-", "")

            # Skip validation for short merchant till numbers (typically 4-6 digits)
            if len(phone) <= 6:
                self.phone_number = phone
                return

            # Ensure it starts with country code or local format
            if not (phone.startswith("252") or phone.startswith("+252") or
                    phone.startswith("61") or phone.startswith("62") or
                    phone.startswith("63") or phone.startswith("65") or
                    phone.startswith("66") or phone.startswith("68") or
                    phone.startswith("69") or phone.startswith("76") or
                    phone.startswith("90")):
                frappe.msgprint(
                    _("Phone number {0} may not be a valid Somali mobile number").format(phone),
                    indicator="orange",
                    alert=True,
                )
            self.phone_number = phone

    def _validate_amount(self):
        """Ensure amount is positive."""
        if self.amount and self.amount <= 0:
            frappe.throw(_("Payment amount must be greater than zero"))

    def update_status(self, status, error_message=None, error_code=None,
                      provider_transaction_id=None, response_payload=None,
                      callback_payload=None):
        """Update transaction status with optional details."""
        self.status = status

        if error_message:
            self.error_message = error_message
        if error_code:
            self.error_code = error_code
        if provider_transaction_id:
            self.provider_transaction_id = provider_transaction_id
        if response_payload:
            self.response_payload = (
                json.dumps(response_payload, indent=2)
                if isinstance(response_payload, dict) else response_payload
            )
        if callback_payload:
            self.callback_payload = (
                json.dumps(callback_payload, indent=2)
                if isinstance(callback_payload, dict) else callback_payload
            )

        if status in ("Completed", "Failed", "Cancelled", "Timeout", "Refunded"):
            self.completed_at = now_datetime()

        self.save(ignore_permissions=True)
        frappe.db.commit()

    def schedule_retry(self):
        """Schedule a retry for this transaction."""
        settings = frappe.get_single("Mobile Payment Settings")
        max_retries = settings.max_retry_attempts or 3

        if self.retry_count >= max_retries:
            self.update_status("Failed", error_message="Max retry attempts exceeded")
            return False

        self.retry_count = (self.retry_count or 0) + 1
        # Exponential backoff: 30s, 60s, 120s, ...
        delay_seconds = 30 * (2 ** (self.retry_count - 1))
        self.next_retry_at = add_to_date(now_datetime(), seconds=delay_seconds)
        self.status = "Retrying"
        self.save(ignore_permissions=True)
        frappe.db.commit()
        return True

    def log_request(self, payload):
        """Log the outgoing request payload."""
        self.request_payload = (
            json.dumps(payload, indent=2)
            if isinstance(payload, dict) else payload
        )
        self.save(ignore_permissions=True)

    def set_hpp_url(self, url):
        """Store the HPP redirect URL."""
        self.hpp_url = url
        self.save(ignore_permissions=True)
