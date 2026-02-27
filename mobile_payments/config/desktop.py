from frappe import _


def get_data():
    return [
        {
            "module_name": "Mobile Payments",
            "color": "#2ecc71",
            "icon": "octicon octicon-device-mobile",
            "type": "module",
            "label": _("Mobile Payments"),
            "description": _("WaafiPay & Edahab Mobile Payment Gateway Integration"),
        }
    ]
