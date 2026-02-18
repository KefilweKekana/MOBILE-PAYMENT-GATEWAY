# Mobile Payments - ERPNext Mobile Payment Gateway Integration

> **WaafiPay & Edahab** mobile payment integration for ERPNext v13–v15

A fully functional Frappe/ERPNext app that integrates WaafiPay (ZAAD, SAHAL, EVCPlus) and Edahab mobile money payment gateways into ERPNext. Supports both **Purchase API** (server-to-server USSD push) and **Hosted Payment Page (HPP)** flows.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Webhook Setup](#webhook-setup)
- [Security](#security)
- [Reports & Dashboard](#reports--dashboard)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## Features

### Payment Providers
- **WaafiPay**: ZAAD, SAHAL, EVCPlus wallets
- **Edahab**: Independent Edahab wallet API

### Payment Flows
- **Purchase API (USSD Push)**: Server-to-server payment request → USSD prompt on customer phone → Automatic confirmation
- **Hosted Payment Page (HPP)**: Generate payment link → Customer redirected to payment page → Callback on completion

### ERPNext Integration
- **Pay with Mobile** button on submitted Sales Invoices
- **POS Awesome** integration — mobile payment methods in POS interface
- **WooCommerce** integration — process mobile payments from WooCommerce orders
- Auto-create Payment Entry on successful payment
- Auto-mark Sales Invoice as Paid
- Custom fields on Sales Invoice and Payment Entry
- Custom Modes of Payment (ZAAD, SAHAL, EVCPlus, Edahab)

### Security
- AES-256-CBC encryption for API credentials
- HMAC-SHA256 webhook signature validation
- IP whitelist for webhook callbacks
- Replay protection for duplicate webhooks
- Full transaction audit trail

### Monitoring & Reporting
- Mobile Payment Dashboard with live metrics
- Daily transaction volume chart
- Provider breakdown statistics
- Settlement reconciliation report
- Exportable CSV/Excel transaction reports
- Retry queue management

### Reliability
- Automatic status polling for pending transactions
- Exponential backoff retry logic for failed transactions
- Background job processing via `frappe.enqueue`
- Transaction timeout handling

---

## Architecture

```
mobile_payments/
├── mobile_payments/
│   ├── __init__.py              # App version
│   ├── hooks.py                 # Frappe hooks configuration
│   ├── install.py               # Post-install setup
│   │
│   ├── api/                     # API integrations
│   │   ├── waafipay.py          # WaafiPay client (Purchase API + HPP)
│   │   ├── edahab.py            # Edahab client (Purchase API + HPP)
│   │   ├── webhooks.py          # Webhook/callback handlers
│   │   ├── pos.py               # POS Awesome backend API
│   │   └── woocommerce.py       # WooCommerce integration API
│   │
│   ├── utils/                   # Utilities
│   │   ├── encryption.py        # AES encryption utilities
│   │   ├── security.py          # IP whitelist, signature validation
│   │   ├── payment_handler.py   # Payment Entry creation, polling, retries
│   │   └── reconciliation.py    # Daily reconciliation, reports
│   │
│   ├── mobile_payments/         # Doctypes, Pages, Reports
│   │   ├── doctype/
│   │   │   ├── mobile_payment_settings/     # Settings (Single)
│   │   │   └── mobile_payment_transaction_log/  # Transaction audit log
│   │   ├── page/
│   │   │   └── mobile_payment_dashboard/    # Dashboard page
│   │   └── report/
│   │       └── mobile_payment_settlement/   # Settlement report
│   │
│   ├── public/                  # Frontend assets
│   │   ├── js/
│   │   │   ├── mobile_payment.js             # Sales Invoice UI integration
│   │   │   └── pos_awesome_integration.js    # POS Awesome UI integration
│   │   └── css/mobile_payment.css             # Custom styles
│   │
│   └── templates/               # HTML templates
│       └── pages/
│           └── mobile_payment_redirect.html
│
├── setup.py
├── requirements.txt
├── README.md
└── license.txt
```

---

## Installation

### Prerequisites
- ERPNext v13, v14, or v15
- Python 3.8+
- PyCryptodome library

### Install Steps

```bash
# Navigate to your bench directory
cd /path/to/frappe-bench

# Get the app
bench get-app mobile_payments /path/to/mobile_payments
# OR from git repository:
# bench get-app mobile_payments https://github.com/your-org/mobile_payments.git

# Install on your site
bench --site your-site.localhost install-app mobile_payments

# Run migrations
bench --site your-site.localhost migrate

# Build assets
bench build --app mobile_payments

# Restart
bench restart
```

### Post-Installation
The install script automatically creates:
- Modes of Payment: ZAAD, SAHAL, EVCPlus, Edahab, WaafiPay
- Payment Gateways: WaafiPay, Edahab
- Custom fields on Sales Invoice and Payment Entry

---

## Configuration

### 1. Mobile Payment Settings

Navigate to: **Mobile Payment Settings** (search bar)

#### General Settings
| Field | Description |
|-------|-------------|
| Enabled | Master switch for mobile payments |
| Default Provider | WaafiPay or Edahab |
| Environment | `sandbox` (testing) or `production` (live) |
| Callback Base URL | Your server URL (e.g., `https://erp.yourcompany.com`) |
| Auto Create Payment Entry | Auto-create PE on success (recommended) |
| Auto Mark Invoice as Paid | Auto-update invoice status |
| Default Payment Account | Bank/Cash account for payment entries |
| Transaction Timeout | Seconds to wait for response (default: 120) |
| Max Retry Attempts | Retry count for failed transactions (default: 3) |

#### WaafiPay Configuration
| Field | Description |
|-------|-------------|
| Enable WaafiPay | Toggle WaafiPay integration |
| Merchant UID | Your WaafiPay Merchant UID |
| API User ID | Your WaafiPay API User ID |
| API Key | Your WaafiPay API Key (stored encrypted) |
| API Base URL | `https://api.waafipay.net/asm` (production) |
| HPP Base URL | `https://hpp.waafipay.net` (production) |
| Supported Methods | Comma-separated: `ZAAD,SAHAL,EVCPlus` |

#### Edahab Configuration
| Field | Description |
|-------|-------------|
| Enable Edahab | Toggle Edahab integration |
| API Key | Your Edahab API Key (stored encrypted) |
| API Secret | Your Edahab API Secret (stored encrypted) |
| API Base URL | `https://edahab.net/api` (production) |
| HPP Base URL | `https://hpp.edahab.net` (production) |
| Agent Code | Your Edahab Agent/Merchant Code |

#### Security Settings
| Field | Description |
|-------|-------------|
| IP Whitelist | Allowed IPs for webhooks (comma-separated) |
| Webhook Secret Key | Secret for HMAC signature validation |
| Enable Webhook Validation | Validate incoming webhook signatures |
| Enable Replay Protection | Block duplicate webhook events |
| Replay Protection Window | Time window in seconds (default: 300) |

### 2. Payment Account Setup

Ensure each Mode of Payment has a default account:

1. Go to **Mode of Payment** → ZAAD (or SAHAL, EVCPlus, Edahab)
2. Add a row in the **Default Account** table
3. Select your Company and the appropriate Bank/Cash account
4. Save

---

## Usage

### Making a Payment (Sales Invoice)

1. Create and **Submit** a Sales Invoice
2. Click **Payment** → **Pay with Mobile**
3. Select payment method (ZAAD, SAHAL, EVCPlus, or Edahab)
4. Choose flow:
   - **USSD Push**: Enter customer's phone number → payment prompt sent to their phone
   - **HPP**: A payment page link is generated → share with customer or open in browser
5. Wait for confirmation (auto-polling)
6. Payment Entry is created automatically
7. Invoice is marked as Paid

### POS Awesome Integration

The app seamlessly integrates with **POS Awesome** (the popular ERPNext POS application):

1. Install POS Awesome as usual and configure a POS Profile
2. Add mobile money payment methods (ZAAD, SAHAL, etc.) to your POS Profile under "Payments"
3. When you open POS Awesome, a **Mobile Money** button automatically appears in the payment section
4. Click it, select provider/method, enter the customer phone number
5. Payment is processed via USSD push (customer gets a prompt on their phone)
6. On success, the payment is recorded and linked to the POS Invoice

**How it works:**
- The `pos_awesome_integration.js` automatically detects when POS Awesome loads
- It injects a Mobile Money payment button into the POS payment area
- Payments are initiated via `mobile_payments.api.pos.initiate_pos_payment`
- After POS Invoice submission, the transaction is auto-linked via `link_pos_invoice`
- Payment Entry is created in the background

### WooCommerce Integration

For shops using the **ERPNext WooCommerce connector**, the app processes mobile money payments from online orders:

#### Option A: WooCommerce Webhook (Recommended)
1. In WooCommerce → Settings → Advanced → Webhooks, create a webhook:
   - **Delivery URL**: `https://your-erpnext.com/api/method/mobile_payments.api.woocommerce.woocommerce_payment_webhook`
   - **Topic**: Order updated
   - **Secret**: Match the "WooCommerce Webhook Secret" in Mobile Payment Settings
2. Install a WooCommerce mobile money payment plugin (or custom gateway) that sets order meta:
   - `_mobile_payment_provider`: "WaafiPay" or "Edahab"
   - `_mobile_payment_method`: "ZAAD", "SAHAL", "EVCPlus", or "Edahab"
   - `_mobile_payment_phone`: Customer phone number

#### Option B: Direct REST API
WooCommerce can call ERPNext directly to initiate payment:
```
POST /api/method/mobile_payments.api.woocommerce.initiate_wc_payment
{
    "order_id": 12345,
    "provider": "WaafiPay",
    "method": "ZAAD",
    "phone": "252612345678",
    "amount": 100.00,
    "currency": "USD",
    "api_key": "your_configured_api_key"
}
```

Then poll for status:
```
GET /api/method/mobile_payments.api.woocommerce.check_wc_payment_status?transaction_id=WP-...&api_key=your_key
```

#### Option C: Automatic Sync
When the WooCommerce connector syncs an order to ERPNext:
- If the order's payment method is a mobile money type, the app auto-detects it
- A transaction log is created and linked to the Sales Invoice/Order
- Payment Entry is created automatically if the payment was already completed in WooCommerce

**Configure in:** Mobile Payment Settings → WooCommerce Integration section

### Payment Flow Diagram

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Sales Invoice │────▶│  Pay Mobile  │────▶│ Select Method│
│  (Submitted)  │     │   Button     │     │ ZAAD/SAHAL/  │
│               │     │              │     │ EVC/Edahab   │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                              ┌────────────────────┴────────────────┐
                              │                                     │
                    ┌─────────▼─────────┐               ┌──────────▼──────────┐
                    │  Purchase API     │               │  HPP Flow           │
                    │  (USSD Push)      │               │  (Payment Page)     │
                    │                   │               │                     │
                    │  Enter Phone →    │               │  Generate Link →    │
                    │  Send Request →   │               │  Customer Pays →    │
                    │  USSD Prompt →    │               │  Callback →         │
                    │  Customer Enters  │               │                     │
                    │  PIN →            │               │                     │
                    └────────┬──────────┘               └──────────┬──────────┘
                             │                                     │
                             └───────────────┬─────────────────────┘
                                             │
                                   ┌─────────▼─────────┐
                                   │  Webhook/Callback  │
                                   │  Received          │
                                   └─────────┬──────────┘
                                             │
                                   ┌─────────▼──────────┐
                                   │ Create Payment     │
                                   │ Entry + Update      │
                                   │ Sales Invoice       │
                                   └────────────────────┘
```

---

## API Reference

### Initiate WaafiPay Payment (Purchase API)
```python
# POST /api/method/mobile_payments.api.waafipay.initiate_waafipay_payment
{
    "phone": "252612345678",
    "amount": 100.00,
    "method": "ZAAD",       # ZAAD, SAHAL, or EVCPlus
    "invoice_id": "SINV-00001",
    "description": "Payment for Invoice"
}
```

### Initiate WaafiPay HPP
```python
# POST /api/method/mobile_payments.api.waafipay.initiate_waafipay_hpp
{
    "amount": 100.00,
    "invoice_id": "SINV-00001",
    "description": "Payment for Invoice"
}
# Returns: { "hpp_url": "https://...", "session_id": "...", ... }
```

### Initiate Edahab Payment (Purchase API)
```python
# POST /api/method/mobile_payments.api.edahab.initiate_edahab_payment
{
    "phone": "252652345678",
    "amount": 100.00,
    "invoice_id": "SINV-00001",
    "description": "Payment for Invoice"
}
```

### Initiate Edahab HPP
```python
# POST /api/method/mobile_payments.api.edahab.initiate_edahab_hpp
{
    "amount": 100.00,
    "invoice_id": "SINV-00001",
    "description": "Payment for Invoice"
}
```

### Check Payment Status
```python
# POST /api/method/mobile_payments.utils.payment_handler.get_payment_status
{
    "transaction_log": "MPAY-2026-00001"
}
```

### Get Available Methods
```python
# POST /api/method/mobile_payments.utils.payment_handler.get_available_methods
# Returns list of enabled payment methods
```

### Cancel Pending Payment
```python
# POST /api/method/mobile_payments.utils.payment_handler.cancel_pending_payment
{
    "transaction_log": "MPAY-2026-00001"
}
```

### POS Awesome — Get Mobile Methods
```python
# POST /api/method/mobile_payments.api.pos.get_mobile_payment_methods
# Returns: { "enabled": true, "methods": [{"provider":"WaafiPay","method":"ZAAD",...}] }
```

### POS Awesome — Initiate POS Payment
```python
# POST /api/method/mobile_payments.api.pos.initiate_pos_payment
{
    "provider": "WaafiPay",
    "method": "ZAAD",
    "phone": "252612345678",
    "amount": 50.00,
    "pos_profile": "POS Profile Name",
    "customer": "Walk-in Customer"
}
```

### POS Awesome — Link Invoice
```python
# POST /api/method/mobile_payments.api.pos.link_pos_invoice
{
    "transaction_log": "MPAY-2026-00001",
    "invoice_name": "POS-INV-00001"
}
```

### WooCommerce — Initiate Payment (Guest/REST)
```python
# POST /api/method/mobile_payments.api.woocommerce.initiate_wc_payment
{
    "order_id": 12345,
    "provider": "WaafiPay",
    "method": "ZAAD",
    "phone": "252612345678",
    "amount": 100.00,
    "currency": "USD",
    "api_key": "your_api_key"
}
```

### WooCommerce — Check Payment Status (Guest/REST)
```python
# GET /api/method/mobile_payments.api.woocommerce.check_wc_payment_status?transaction_id=WP-...&api_key=your_key
```

### WooCommerce — Webhook Endpoint
```
POST https://your-site.com/api/method/mobile_payments.api.woocommerce.woocommerce_payment_webhook
# Receives WooCommerce order webhook notifications
```

---

## Webhook Setup

### WaafiPay Webhook URL
```
POST https://your-site.com/api/method/mobile_payments.api.webhooks.waafipay_callback
```

### WaafiPay HPP Return URL
```
GET https://your-site.com/api/method/mobile_payments.api.webhooks.waafipay_hpp_return
```

### Edahab Webhook URL
```
POST https://your-site.com/api/method/mobile_payments.api.webhooks.edahab_callback
```

### Edahab HPP Return URL
```
GET https://your-site.com/api/method/mobile_payments.api.webhooks.edahab_hpp_return
```

**Important**: These endpoints are guest-accessible (no auth required) so payment providers can reach them. Security is enforced via:
- IP whitelist validation
- Webhook signature validation
- Replay protection

---

## Security

### Credential Storage
- API keys and secrets are stored using Frappe's `Password` field type (encrypted at rest)
- Additional AES-256-CBC encryption available via `encryption.py` utilities
- Never log or expose API credentials

### Webhook Security
1. **IP Whitelist**: Only accept webhooks from known provider IPs
2. **Signature Validation**: HMAC-SHA256 signature verification on incoming webhooks
3. **Replay Protection**: Duplicate webhook events are rejected within the configured time window
4. **HTTPS Required**: Always use HTTPS in production

### Best Practices
- Use separate API credentials for sandbox and production
- Rotate webhook secret keys periodically
- Monitor the Error Log for security-related entries
- Enable all security features in production

---

## Reports & Dashboard

### Mobile Payment Dashboard
Navigate to: **Mobile Payment Dashboard** (sidebar or search)

Features:
- Summary cards (Successful, Failed, Pending, Success Rate)
- Daily transaction volume chart
- Provider breakdown table
- Recent transactions list
- Export and reconciliation actions

### Settlement Reconciliation Report
Navigate to: **Mobile Payment Settlement** report

Features:
- Filter by date range, provider, status, reconciliation status
- Totals row for amount column
- Links to Transaction Log, Sales Invoice, Payment Entry
- Export to CSV/Excel

### Scheduled Tasks
| Task | Schedule | Description |
|------|----------|-------------|
| Poll Pending | Every 2 min | Check status of pending transactions |
| Retry Failed | Every 5 min | Retry eligible failed transactions |
| Reconciliation | Daily 1 AM | Compare transactions with Payment Entries |

---

## Troubleshooting

### Common Issues

**Payment button not showing on Sales Invoice**
- Ensure the invoice is Submitted (docstatus = 1)
- Ensure there is outstanding amount > 0
- Run `bench build --app mobile_payments`
- Clear browser cache

**Webhook not received**
- Check that callback URLs are correct in Mobile Payment Settings
- Verify IP whitelist includes provider IPs
- Check Error Log for security-related blocks
- Ensure your server is accessible from the internet

**Payment Entry not created**
- Check "Auto Create Payment Entry" is enabled in settings
- Verify Mode of Payment has a default account configured
- Check Error Log for any exceptions
- Verify the Sales Invoice is in Submitted state

**Transaction stuck in Pending**
- The polling job runs every 2 minutes automatically
- Check Worker/Scheduler logs: `bench --site your-site doctor`
- Manually check status via the Transaction Log

### Logs
- **Frappe Error Log**: Check for exceptions
- **mobile_payments logger**: `frappe.logger("mobile_payments")` writes to site logs
- **Transaction Log**: Full request/response payloads stored per transaction

---

## Development

### Running Tests
```bash
bench --site your-site run-tests --app mobile_payments
```

### Sandbox Testing
1. Set Environment to `sandbox` in Mobile Payment Settings
2. Use test API credentials from WaafiPay/Edahab
3. Test both Purchase API and HPP flows
4. Verify webhooks using tools like ngrok for local development

### Adding a New Payment Provider
1. Create a new client class in `api/` (follow `waafipay.py` pattern)
2. Add webhook handlers in `webhooks.py`
3. Update `Mobile Payment Settings` doctype with new provider fields
4. Add frontend method option in `mobile_payment.js`
5. Update `install.py` to create Mode of Payment

---

## Version Compatibility

| ERPNext Version | Status |
|----------------|--------|
| v13 | ✅ Compatible |
| v14 | ✅ Compatible |
| v15 | ✅ Compatible |

---

## License

MIT License - See [license.txt](license.txt)
