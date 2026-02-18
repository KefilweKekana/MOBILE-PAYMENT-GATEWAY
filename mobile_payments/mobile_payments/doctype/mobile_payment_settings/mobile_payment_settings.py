"""
Mobile Payment Settings - Single DocType
Stores API credentials, configuration, and security settings for
WaafiPay and Edahab mobile payment integrations.
"""
from __future__ import unicode_literals

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

        return {
            "merchant_uid": self.waafipay_merchant_uid,
            "api_user_id": self.waafipay_api_user_id,
            "api_key": self.get_password("waafipay_api_key"),
            "base_url": self.waafipay_base_url,
            "hpp_base_url": self.waafipay_hpp_base_url,
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


def get_settings():
    """Shortcut to get Mobile Payment Settings."""
    return MobilePaymentSettings.get_settings()
