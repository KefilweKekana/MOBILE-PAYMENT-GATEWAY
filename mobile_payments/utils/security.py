"""
Security Utilities
IP whitelisting, webhook validation, and replay protection.
"""
from __future__ import unicode_literals

import time

import frappe
from frappe import _
from mobile_payments.utils.encryption import verify_hmac_signature


# In-memory cache for replay protection (per worker process)
_processed_webhooks = {}


def validate_ip_whitelist(request_ip):
    """
    Validate that the request IP is in the whitelist.

    Args:
        request_ip: IP address of the incoming request

    Returns:
        True if IP is allowed, raises exception otherwise
    """
    settings = frappe.get_single("Mobile Payment Settings")
    whitelist = settings.ip_whitelist

    if not whitelist:
        # No whitelist configured - allow all
        return True

    allowed_ips = [ip.strip() for ip in whitelist.split(",") if ip.strip()]

    if not allowed_ips:
        return True

    if request_ip not in allowed_ips:
        frappe.log_error(
            f"Blocked webhook from unauthorized IP: {request_ip}",
            "Mobile Payment Security",
        )
        frappe.throw(
            _("Unauthorized IP address: {0}").format(request_ip),
            exc=frappe.AuthenticationError,
        )

    return True


def validate_webhook_signature(payload, signature, provider="WaafiPay"):
    """
    Validate webhook signature from payment provider.

    Args:
        payload: The raw request body/payload string
        signature: The signature from the request header
        provider: Payment provider name

    Returns:
        True if signature is valid
    """
    settings = frappe.get_single("Mobile Payment Settings")

    if not settings.enable_webhook_validation:
        return True

    secret = settings.get_password("webhook_secret_key")
    if not secret:
        frappe.log_error(
            "Webhook validation enabled but no secret key configured",
            "Mobile Payment Security",
        )
        return True

    if not verify_hmac_signature(payload, signature, secret):
        frappe.log_error(
            f"Invalid webhook signature from {provider}. "
            f"Signature: {signature[:20]}...",
            "Mobile Payment Security",
        )
        frappe.throw(
            _("Invalid webhook signature"),
            exc=frappe.AuthenticationError,
        )

    return True


def check_replay_protection(event_id, timestamp=None):
    """
    Check for webhook replay attacks.
    Prevents the same webhook event from being processed twice.

    Args:
        event_id: Unique identifier for the webhook event
        timestamp: Unix timestamp of the event (optional)

    Returns:
        True if event should be processed (not a replay)
    """
    global _processed_webhooks

    settings = frappe.get_single("Mobile Payment Settings")

    if not settings.enable_replay_protection:
        return True

    window = settings.replay_protection_window or 300

    # Check timestamp freshness if provided
    if timestamp:
        current_time = time.time()
        if abs(current_time - float(timestamp)) > window:
            frappe.log_error(
                f"Webhook replay detected (stale timestamp): event_id={event_id}",
                "Mobile Payment Security",
            )
            return False

    # Check if event was already processed (in-memory + database check)
    if event_id in _processed_webhooks:
        frappe.log_error(
            f"Webhook replay detected (duplicate): event_id={event_id}",
            "Mobile Payment Security",
        )
        return False

    # Also check database for cross-worker protection
    existing = frappe.db.exists(
        "Mobile Payment Transaction Log",
        {"provider_transaction_id": event_id, "status": "Completed"},
    )
    if existing:
        frappe.log_error(
            f"Webhook replay detected (already completed in DB): event_id={event_id}",
            "Mobile Payment Security",
        )
        return False

    # Mark as processed
    _processed_webhooks[event_id] = time.time()

    # Cleanup old entries from memory cache
    cutoff = time.time() - window
    _processed_webhooks = {
        k: v for k, v in _processed_webhooks.items() if v > cutoff
    }

    return True


def get_client_ip():
    """Get the real client IP address from the request."""
    if hasattr(frappe, "request") and frappe.request:
        # Check X-Forwarded-For header (common behind reverse proxy)
        forwarded_for = frappe.request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header (nginx)
        real_ip = frappe.request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()

        # Fall back to remote address
        return frappe.request.remote_addr

    return "0.0.0.0"


def sanitize_phone_number(phone, country_code="252"):
    """
    Normalize phone number to international format.

    Args:
        phone: Raw phone number input
        country_code: Default country code (Somalia = 252)

    Returns:
        Normalized phone number (e.g., 252XXXXXXXXX)
    """
    if not phone:
        return ""

    # Remove all non-digit characters except leading +
    cleaned = "".join(c for c in phone if c.isdigit() or (c == "+" and phone.index(c) == 0))

    # Remove leading +
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    # Add country code if not present
    if not cleaned.startswith(country_code):
        # Remove leading 0 if present
        if cleaned.startswith("0"):
            cleaned = cleaned[1:]
        cleaned = country_code + cleaned

    return cleaned
