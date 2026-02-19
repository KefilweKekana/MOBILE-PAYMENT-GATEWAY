"""
Mobile Payment Settings - Single DocType
Stores API credentials, configuration, and security settings for
WaafiPay and Edahab mobile payment integrations.
"""
from __future__ import unicode_literals

import json

import frappe
from frappe import _
from frappe.model.document import Document
from mobile_payments.utils.encryption import encrypt_value, decrypt_value


class MobilePaymentSettings(Document):
    """Configuration settings for mobile payment integrations."""

    def validate(self):
        """Validate settings before save."""
        self._validate_urls()
        self._validate_waafipay_config()
        self._validate_edahab_config()
        self._validate_security_settings()

    def _validate_urls(self):
        """Ensure URL fields have proper format."""
        url_fields = [
            "waafipay_base_url",
            "waafipay_hpp_base_url",
            "edahab_base_url",
            "edahab_hpp_base_url",
            "callback_base_url",
        ]
        for field in url_fields:
            value = self.get(field)
            if value and not value.startswith("http"):
                frappe.throw(
                    _("Field {0} must be a valid URL starting with http:// or https://").format(
                        field
                    )
                )
            # Remove trailing slash
            if value and value.endswith("/"):
                self.set(field, value.rstrip("/"))

    def _validate_waafipay_config(self):
        """Validate WaafiPay configuration."""
        if self.waafipay_enabled:
            required = ["waafipay_merchant_uid", "waafipay_api_user_id", "waafipay_api_key"]
            for field in required:
                if not self.get(field):
                    frappe.throw(
                        _("WaafiPay is enabled but {0} is not set").format(
                            self.meta.get_label(field)
                        )
                    )

    def _validate_edahab_config(self):
        """Validate Edahab configuration."""
        if self.edahab_enabled:
            required = ["edahab_api_key", "edahab_api_secret"]
            for field in required:
                if not self.get(field):
                    frappe.throw(
                        _("Edahab is enabled but {0} is not set").format(
                            self.meta.get_label(field)
                        )
                    )

    def _validate_security_settings(self):
        """Validate security settings."""
        if self.enable_replay_protection:
            if not self.replay_protection_window or self.replay_protection_window < 60:
                frappe.throw(
                    _("Replay protection window must be at least 60 seconds")
                )

    def get_waafipay_credentials(self):
        """Return WaafiPay API credentials."""
        if not self.waafipay_enabled:
            frappe.throw(_("WaafiPay is not enabled"))

        hpp_key = ""
        if hasattr(self, "waafipay_hpp_key") and self.waafipay_hpp_key:
            try:
                hpp_key = self.get_password("waafipay_hpp_key")
            except Exception:
                hpp_key = ""

        return {
            "merchant_uid": self.waafipay_merchant_uid,
            "store_id": getattr(self, "waafipay_store_id", "") or "",
            "api_user_id": self.waafipay_api_user_id,
            "api_key": self.get_password("waafipay_api_key"),
            "base_url": self.waafipay_base_url,
            "hpp_base_url": self.waafipay_hpp_base_url,
            "hpp_key": hpp_key,
        }

    def get_edahab_credentials(self):
        """Return Edahab API credentials."""
        if not self.edahab_enabled:
            frappe.throw(_("Edahab is not enabled"))

        return {
            "api_key": self.get_password("edahab_api_key"),
            "api_secret": self.get_password("edahab_api_secret"),
            "base_url": self.edahab_base_url,
            "hpp_base_url": self.edahab_hpp_base_url,
            "agent_code": self.edahab_agent_code,
        }

    def get_callback_url(self, path=""):
        """Build full callback URL."""
        base = self.callback_base_url or frappe.utils.get_url()
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    def get_supported_waafipay_methods(self):
        """Return list of supported WaafiPay payment methods."""
        if not self.waafipay_supported_methods:
            return ["ZAAD", "SAHAL", "EVCPlus"]
        return [m.strip() for m in self.waafipay_supported_methods.split(",")]

    @staticmethod
    def get_settings():
        """Get the singleton settings document."""
        return frappe.get_single("Mobile Payment Settings")


@frappe.whitelist()
def test_waafipay_connection():
    """Test WaafiPay API credentials by making a lightweight API call."""
    import requests as req

    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.waafipay_enabled:
        frappe.throw(_("WaafiPay is not enabled"))

    creds = settings.get_waafipay_credentials()

    # Use a zero-amount pre-authorize or a health-check style call.
    # WaafiPay's API will validate credentials and return an auth error
    # if they are wrong, or a business-logic error if they are correct.
    payload = {
        "schemaVersion": "1.0",
        "requestId": frappe.generate_hash(length=16),
        "timestamp": frappe.utils.now_datetime().isoformat(),
        "channelName": "WEB",
        "serviceName": "API_PURCHASE",
        "serviceParams": {
            "merchantUid": creds["merchant_uid"],
            "storeId": creds.get("store_id", ""),
            "apiUserId": creds["api_user_id"],
            "apiKey": creds["api_key"],
            "transactionInfo": {
                "referenceId": f"TEST-{frappe.generate_hash(length=8)}",
                "invoiceId": "CONNECTION-TEST",
                "amount": "0",
                "currency": "USD",
                "description": "Connection test - ignore",
            },
            "paymentInfo": {
                "accountNo": "0000000000",
            },
        },
    }

    try:
        url = creds["base_url"]
        resp = req.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
        code = data.get("responseCode", "")
        msg = data.get("responseMsg", "")

        # 5310 = invalid phone/amount (means credentials are valid, request reached business logic)
        # 2001 = success (unlikely for zero amount but possible)
        # 5206/5001 = auth failure
        auth_failure_codes = ["5206", "5001", "4001", "E0001"]

        if code in auth_failure_codes:
            return {
                "success": False,
                "message": f"Authentication failed: {msg} (code {code}). Check your Merchant UID, API User ID, and API Key.",
            }
        else:
            return {
                "success": True,
                "message": f"WaafiPay credentials are valid! API responded with code {code}: {msg}",
            }
    except req.exceptions.ConnectionError:
        return {"success": False, "message": f"Cannot connect to {creds['base_url']}. Check the Base URL."}
    except req.exceptions.Timeout:
        return {"success": False, "message": "Connection timed out. The WaafiPay server is not responding."}
    except Exception as e:
        return {"success": False, "message": f"Connection test failed: {str(e)}"}


@frappe.whitelist()
def test_edahab_connection():
    """Test Edahab API credentials by making a lightweight API call."""
    import hashlib
    import requests as req

    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.edahab_enabled:
        frappe.throw(_("Edahab is not enabled"))

    creds = settings.get_edahab_credentials()

    # Edahab uses hash-based auth. Send a minimal IssueInvoice request
    # with amount=0 to validate credentials without charging anyone.
    request_data = {
        "apiKey": creds["api_key"],
        "EdahabNumber": "",
        "Amount": 0,
        "Currency": "USD",
        "AgentCode": creds.get("agent_code", ""),
        "Description": "Connection test - ignore",
    }

    # Edahab hash: SHA256(apiKey + amount + currency + agentCode + apiSecret)
    hash_input = (
        f"{creds['api_key']}0USD{creds.get('agent_code', '')}"
    )
    request_hash = hashlib.sha256(
        (hash_input + creds["api_secret"]).encode()
    ).hexdigest()
    request_data["Hash"] = request_hash

    try:
        url = f"{creds['base_url']}/api/issueinvoice"
        resp = req.post(
            url,
            json=request_data,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )

        # Handle non-JSON responses (HTML error pages, empty responses)
        try:
            data = resp.json()
        except (ValueError, req.exceptions.JSONDecodeError):
            return {
                "success": False,
                "message": (
                    f"Edahab API returned non-JSON response (HTTP {resp.status_code}). "
                    f"Check your Base URL: {creds['base_url']}"
                ),
            }
        response_code = data.get("ResponseCode", data.get("responseCode", ""))
        response_msg = data.get("ResponseMessage", data.get("responseMessage", ""))

        auth_failure_codes = ["E10003", "E10004", "E10005", "401"]

        if str(response_code) in auth_failure_codes:
            return {
                "success": False,
                "message": f"Authentication failed: {response_msg} (code {response_code}). Check your API Key and Secret.",
            }
        else:
            return {
                "success": True,
                "message": f"Edahab credentials are valid! API responded with code {response_code}: {response_msg}",
            }
    except req.exceptions.ConnectionError:
        return {"success": False, "message": f"Cannot connect to {creds['base_url']}. Check the Base URL."}
    except req.exceptions.Timeout:
        return {"success": False, "message": "Connection timed out. The Edahab server is not responding."}
    except Exception as e:
        return {"success": False, "message": f"Connection test failed: {str(e)}"}


def get_settings():
    """Shortcut to get Mobile Payment Settings."""
    return MobilePaymentSettings.get_settings()
