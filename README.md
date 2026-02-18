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
│   │   └── webhooks.py          # Webhook/callback handlers
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
│   │   ├── js/mobile_payment.js   # Sales Invoice UI integration
│   │   └── css/mobile_payment.css # Custom styles
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
