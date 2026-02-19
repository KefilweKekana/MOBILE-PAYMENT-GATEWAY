from __future__ import unicode_literals

app_name = "mobile_payments"
app_title = "Mobile Payments"
app_publisher = "Mobile Payments Team"
app_description = "ERPNext Mobile Payment Gateway Integration (WaafiPay & Edahab)"
app_email = "dev@mobilepayments.so"
app_license = "MIT"
app_version = "1.0.0"

# Includes in <head>
# --------------------

app_include_css = "/assets/mobile_payments/css/mobile_payment.css"
app_include_js = [
    "/assets/mobile_payments/js/mobile_payment.js",
    "/assets/mobile_payments/js/pos_awesome_integration.js",
]

# Website Pages
# --------------------
website_route_rules = [
    {
        "from_route": "/mobile-payment-callback/<path:app_path>",
        "to_route": "mobile_payment_callback",
    },
]

# DocType Events
# --------------------
doc_events = {
    "Sales Invoice": {
        "on_submit": [
            "mobile_payments.utils.payment_handler.on_sales_invoice_submit",
            "mobile_payments.api.woocommerce.on_woocommerce_order_sync",
            "mobile_payments.utils.payment_links.generate_payment_links_if_due",
        ],
        "on_update_after_submit": [
            "mobile_payments.utils.payment_links.generate_payment_links_if_due",
        ],
    },
    "Sales Order": {
        "on_submit": "mobile_payments.api.woocommerce.on_woocommerce_order_sync",
    },
    "POS Invoice": {
        "on_submit": "mobile_payments.utils.payment_handler.on_sales_invoice_submit",
    },
    "Payment Entry": {
        "on_submit": "mobile_payments.utils.payment_handler.on_payment_entry_submit",
    },
}

# Scheduled Tasks
# --------------------
scheduler_events = {
    "cron": {
        # Poll pending transactions every 2 minutes
        "*/2 * * * *": [
            "mobile_payments.utils.payment_handler.poll_pending_transactions",
        ],
        # Daily reconciliation at 1 AM
        "0 1 * * *": [
            "mobile_payments.utils.reconciliation.run_daily_reconciliation",
        ],
        # Generate payment links for invoices due today (runs at 7 AM)
        "0 7 * * *": [
            "mobile_payments.utils.payment_links.daily_generate_payment_links",
        ],
        # Retry failed transactions every 5 minutes
        "*/5 * * * *": [
            "mobile_payments.utils.payment_handler.retry_failed_transactions",
        ],
        # Process pending WooCommerce orders every 10 minutes
        "*/10 * * * *": [
            "mobile_payments.api.woocommerce.process_pending_wc_orders",
        ],
    },
}

# Jinja Environment Customizations
# --------------------
jinja = {
    "methods": [],
    "filters": [],
}

# Fixtures
# --------------------
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [["module", "=", "Mobile Payments"]],
    },
    {
        "dt": "Property Setter",
        "filters": [["module", "=", "Mobile Payments"]],
    },
]

# Override Whitelisted Methods
# --------------------
override_whitelisted_methods = {}

# Exempt Linked Doctypes from Cancellation
# --------------------
auto_cancel_exempted_doctypes = ["Mobile Payment Transaction Log"]

# API Endpoints (whitelisted)
# These are exposed via frappe.call or REST API
# --------------------

# Permission Query Conditions
# --------------------
# permission_query_conditions = {
#     "Mobile Payment Transaction Log": "mobile_payments.permissions.get_permission_query",
# }

# Webhook URLs (no auth required)
# --------------------
guest_allowed_routes = [
    "mobile_payments.api.webhooks.waafipay_callback",
    "mobile_payments.api.webhooks.edahab_callback",
    "mobile_payments.api.webhooks.waafipay_hpp_return",
    "mobile_payments.api.webhooks.edahab_hpp_return",
    "mobile_payments.api.woocommerce.woocommerce_payment_webhook",
    "mobile_payments.api.woocommerce.initiate_wc_payment",
    "mobile_payments.api.woocommerce.check_wc_payment_status",
    "mobile_payments.api.payment_link.pay",
]

# After Install
# --------------------
after_install = "mobile_payments.install.after_install"
after_uninstall = "mobile_payments.install.after_uninstall"

# Override DocType Class
# --------------------
# override_doctype_class = {
#     "Sales Invoice": "mobile_payments.overrides.sales_invoice.CustomSalesInvoice",
# }
