# Mobile Payments — How It Works

## Overview

This Frappe app integrates **WaafiPay** (ZAAD, SAHAL, EVCPlus) and **Edahab** mobile money into ERPNext. It supports three payment flows: **POS**, **Patient Appointment**, and **Sales Invoice / E-Commerce**.

---

## 1. POS Payment Flow

**When the cashier clicks MODE OF PAYMENT in POS Awesome:**

1. Amount auto-fills from the POS transaction total
2. Currency auto-detects from the Sales Invoice / POS Invoice (no manual selection)
3. Phone number auto-fetches from Customer Contact → Customer → Patient
4. Phone is validated (min 9 digits)
5. API credentials are validated before sending
6. USSD prompt is sent to customer's phone via WaafiPay/Edahab API
7. System polls every 2 seconds for confirmation
8. On success → Sales Invoice auto-submitted → Payment Entry created
9. On rejection → Invoice stays as draft, failure reason logged

**Key files:**
- `public/js/pos_awesome_integration.js` — Button injection, dialog, polling UI
- `api/pos.py` — `initiate_pos_payment()`, `check_pos_payment_status()`, `get_customer_phone()`
- `utils/payment_handler.py` — `process_successful_payment()`, creates Payment Entry

**Button behavior:**
- Clicking **MODE OF PAYMENT** fills amount + opens dialog
- Clicking any other payment button (REC CASH, etc.) clears the mobile amount field

---

## 2. Patient Appointment Flow

**When "Pay with Mobile" is clicked on a Patient Appointment:**

1. Patient mobile number auto-fetched from `Patient.mobile`
2. Appointment amount auto-fetched
3. USSD sent to patient's phone
4. On confirmed payment:
   - Sales Invoice auto-created with Mode of Payment = ZAAD/Edahab
   - Payment Entry auto-created and linked
   - Sales Invoice reference stored on the Appointment (`mobile_payment_sales_invoice` field)
5. On rejection → no invoice created, failure logged

**Key files:**
- `public/js/mobile_payment.js` — Dashboard button on Patient Appointment
- `utils/payment_handler.py` — `initiate_appointment_payment()`, `process_successful_appointment_payment()`, `_create_appointment_invoice()`

---

## 3. Sales Invoice Payment Links (HPP)

**Auto-generates WaafiPay + Edahab hosted payment page links when:**
- Sales Invoice is submitted with `due_date = today`
- Daily scheduled task at 7 AM checks all due invoices
- Manual button click on the Sales Invoice form

Links are stored in custom fields: `waafi_payment_link` and `edahab_payment_link`.

**Key files:**
- `utils/payment_links.py` — `generate_payment_links_if_due()`, `daily_generate_payment_links()`
- `install.py` — Creates the custom fields on Sales Invoice

---

## 4. Currency Handling

Currency is **never manually selected**. It's read from:
- **POS:** The Sales Invoice / POS Invoice `currency` field
- **Appointment:** The invoice currency at creation time
- **Payment Links:** The Sales Invoice `currency` field

Whatever currency is on the ERPNext transaction is what gets sent to WaafiPay/Edahab (USD or SLSH).

---

## 5. Webhook & Callback Handling

Payment providers send confirmations back to these endpoints:

| Provider | Endpoint | File |
|----------|----------|------|
| WaafiPay webhook | `/api/method/mobile_payments.api.webhooks.waafipay_callback` | `api/webhooks.py` |
| WaafiPay HPP return | `/api/method/mobile_payments.api.webhooks.waafipay_hpp_return` | `api/webhooks.py` |
| Edahab webhook | `/api/method/mobile_payments.api.webhooks.edahab_callback` | `api/webhooks.py` |
| Edahab HPP return | `/api/method/mobile_payments.api.webhooks.edahab_hpp_return` | `api/webhooks.py` |

On webhook confirmation → `process_successful_payment()` is enqueued → auto-submits SI → creates Payment Entry.

---

## 6. Phone Number Fetching

Server-side endpoint: `mobile_payments.api.pos.get_customer_phone`

Lookup order (first match wins):
1. **Contact** linked to Customer via Dynamic Link (primary contact preferred)
2. **Contact Phone** child table entries
3. **Customer.mobile_no** field
4. **Patient** linked to Customer (Healthcare module)

---

## 7. Settings & Credentials

**Location:** Mobile Payment Settings (`/app/mobile-payment-settings`)

| Field | Section |
|-------|---------|
| Merchant UID | WaafiPay Configuration |
| Store ID | WaafiPay Configuration |
| API User ID | WaafiPay Configuration |
| API Key (encrypted) | WaafiPay Configuration |
| API Base URL | WaafiPay Configuration |
| HPP Base URL | WaafiPay Configuration |
| Supported Methods | WaafiPay Configuration |
| API Key (encrypted) | Edahab Configuration |
| API Secret (encrypted) | Edahab Configuration |
| Agent Code | Edahab Configuration |

Credentials are **validated before every payment request**. If Store ID, API Key, or Merchant UID is missing, the payment is blocked with a clear error message.

---

## 8. Transaction Logging

Every payment attempt is logged in **Mobile Payment Transaction Log** (`/app/mobile-payment-transaction-log`).

Each log stores:
- Provider, method, phone, amount, currency
- Request payload and provider response
- Status (Initiated → Pending → Completed/Failed/Cancelled)
- Payment Entry link (after success)
- Sales Invoice link
- Error message (on failure)
- Retry count

---

## 9. Scheduled Tasks

| Task | Frequency | File |
|------|-----------|------|
| Poll pending transactions | Every 2 min | `utils/payment_handler.py` |
| Retry failed transactions | Every 5 min | `utils/payment_handler.py` |
| Generate payment links for due invoices | Daily 7 AM | `utils/payment_links.py` |
| Daily reconciliation | Daily 1 AM | `utils/reconciliation.py` |
| Process WooCommerce orders | Every 10 min | `api/woocommerce.py` |

---

## 10. Custom Fields Added to ERPNext

**Sales Invoice:**
- Mobile Payment section (status, provider, method, phone, reference, transaction log)
- Payment Links section (waafi_payment_link, edahab_payment_link)

**Patient Appointment:**
- Mobile Payment section (same as SI + `mobile_payment_sales_invoice` link)

**Payment Entry:**
- `mobile_payment_reference`, `mobile_payment_transaction_id`

---

## 11. File Map

```
mobile_payments/
├── api/
│   ├── pos.py                  # POS payment API + phone fetch
│   ├── waafipay.py             # WaafiPay client (Purchase, HPP, Status)
│   ├── edahab.py               # Edahab client (Purchase, HPP, Status)
│   ├── webhooks.py             # Webhook/callback handlers
│   ├── payment_link.py         # Manual payment link API
│   └── woocommerce.py          # WooCommerce integration
├── utils/
│   ├── payment_handler.py      # Core: process payments, create PE, auto-submit SI
│   ├── payment_links.py        # Auto-generate HPP links on due invoices
│   ├── encryption.py           # Value encryption utilities
│   ├── notifications.py        # Payment notifications
│   ├── reconciliation.py       # Daily reconciliation
│   └── security.py             # IP whitelist, webhook signatures, replay protection
├── public/
│   ├── js/
│   │   ├── mobile_payment.js           # Sales Invoice + Appointment UI
│   │   └── pos_awesome_integration.js  # POS Awesome button injection
│   └── css/
│       └── mobile_payment.css          # Styles
├── mobile_payments/
│   └── doctype/
│       ├── mobile_payment_settings/    # Settings DocType
│       └── mobile_payment_transaction_log/  # Transaction Log DocType
├── hooks.py                    # Doc events, scheduler, includes
└── install.py                  # Custom fields, modes of payment
```

---

## 12. Deployment

After any code changes:
```bash
bench build --app mobile_payments
bench clear-cache
bench restart
```

After adding new DocType fields:
```bash
bench migrate
```
