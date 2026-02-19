# Mobile Payments - Complete Testing Guide

## Table of Contents

1. [Pre-requisites](#1-pre-requisites)
2. [Install & Deploy](#2-install--deploy)
3. [Configure Settings](#3-configure-settings)
4. [Test: Sales Invoice Payment](#4-test-sales-invoice-payment)
5. [Test: POS Awesome Payment](#5-test-pos-awesome-payment)
6. [Test: WooCommerce Payment](#6-test-woocommerce-payment)
7. [Test: Hosted Payment Page (HPP)](#7-test-hosted-payment-page-hpp)
8. [Test: Dashboard & Reports](#8-test-dashboard--reports)
9. [Test: Security Features](#9-test-security-features)
10. [Test: Error Handling & Retry](#10-test-error-handling--retry)
11. [Test: Test Connection Button](#11-test-test-connection-button)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Pre-requisites

### Required
- ERPNext site (v13, v14, or v15) running on Frappe Cloud, VPS, or local
- mobile_payments app installed (`bench install-app mobile_payments`)
- `bench build` run after install
- At least one customer and one item in ERPNext

### For WaafiPay Testing
- WaafiPay Merchant UID (from WaafiPay merchant portal)
- WaafiPay API User ID (from WaafiPay merchant portal)
- WaafiPay API Key (from WaafiPay merchant portal)
- A Somali phone number with ZAAD, SAHAL, or EVCPlus active

### For Edahab Testing
- Edahab API Key (from Edahab merchant portal)
- Edahab API Secret (from Edahab merchant portal)
- A Somali phone number with Edahab active

### For POS Awesome Testing
- POS Awesome app installed (`bench get-app posawesome && bench install-app posawesome`)
- A POS Profile configured with ZAAD/SAHAL/EVCPlus/Edahab as payment methods

### For WooCommerce Testing
- WooCommerce ERPNext connector app installed
- A WooCommerce store connected and syncing orders

---

## 2. Install & Deploy

```bash
# Install the app
bench get-app https://github.com/YOUR_REPO/mobile_payments.git
bench --site YOUR_SITE install-app mobile_payments

# Build assets (IMPORTANT - buttons won't appear without this)
bench build

# Clear cache
bench --site YOUR_SITE clear-cache

# Restart workers
bench restart
```

### Verify Installation
After install, check:
- [ ] Go to `/app/mobile-payment-settings` → Settings page loads
- [ ] Go to `/app/mobile-payment-transaction-log` → List view loads (empty)
- [ ] Go to `/app/mode-of-payment` → ZAAD, SAHAL, EVCPlus, Edahab, WaafiPay exist
- [ ] Open any submitted Sales Invoice → Scroll down → "Mobile Payment" section visible
- [ ] Open any submitted Sales Invoice with outstanding > 0 → "Pay with Mobile" button visible under Payment dropdown

---

## 3. Configure Settings

1. Go to: **YOUR_SITE/app/mobile-payment-settings**

2. **General Settings:**
   - Default Currency: `USD` (or `SOS`)
   - Default Provider: `WaafiPay` or `Edahab`
   - Transaction Timeout: `120` (seconds)
   - Environment: `sandbox` (for testing) or `production` (for live)

3. **WaafiPay Configuration:**
   - [x] Enable WaafiPay
   - Merchant UID: `(from WaafiPay)`
   - API User ID: `(from WaafiPay)`
   - API Key: `(from WaafiPay)`
   - Base URL: `https://api.waafipay.net/asm` (pre-filled)
   - HPP Base URL: `https://api.waafipay.net/asm` (pre-filled)

4. **Edahab Configuration:**
   - [x] Enable Edahab
   - API Key: `(from Edahab)`
   - API Secret: `(from Edahab)`
   - Base URL: `https://edahab.net` (pre-filled)

5. **Security Settings:**
   - [x] Enable Webhook Validation
   - Webhook Secret Key: `(any strong secret you choose)`
   - [x] Enable Replay Protection
   - Replay Protection Window: `300` (seconds)
   - IP Whitelist: `(leave blank for testing, add WaafiPay/Edahab IPs for production)`

6. Click **Save**

---

## 4. Test: Sales Invoice Payment

This is the primary payment flow.

### Step 1: Create a Sales Invoice
1. Go to `/app/sales-invoice/new`
2. Select a **Customer**
3. Add an **Item** (e.g., quantity: 1, rate: 10 USD)
4. Click **Save**
5. Click **Submit** → Confirm

### Step 2: Initiate Payment
1. On the submitted invoice, click the **Payment** dropdown in the toolbar
2. Click **"Pay with Mobile"**
3. A dialog appears showing:
   - The outstanding amount in bold
   - **Payment Method** dropdown (e.g., `WaafiPay|ZAAD`, `WaafiPay|SAHAL`, `Edahab|Edahab`)
   - **Payment Flow** selector: `Purchase API (USSD Push)` or `Hosted Payment Page (HPP)`
   - **Phone Number** field (for USSD Push)

### Step 3: USSD Push Payment
1. Select a payment method (e.g., `WaafiPay|ZAAD`)
2. Keep flow as **"Purchase API (USSD Push)"**
3. Enter the customer's phone number (e.g., `252612345678`)
4. Click **"Proceed to Pay"**

### Step 4: What Happens Next
1. A processing dialog appears: *"A payment prompt has been sent to the customer's phone. Waiting for confirmation..."*
2. The WaafiPay/Edahab API sends a USSD push to the customer's phone
3. Customer sees a pop-up on their phone: "Pay $10.00 to [Business]? Enter PIN:"
4. Customer enters their PIN
5. The dialog polls every 2 seconds for up to 2 minutes

### Step 5: Verify Success
After the customer confirms:
- [ ] Dialog shows **"Payment Successful"** with green indicator
- [ ] Invoice reloads automatically
- [ ] **Mobile Payment Status** field → `Completed`
- [ ] **Payment Provider** → `WaafiPay` or `Edahab`
- [ ] **Payment Method** → `ZAAD`, `SAHAL`, `EVCPlus`, or `Edahab`
- [ ] **Payment Phone Number** → the phone number used
- [ ] **Payment Reference ID** → transaction reference from provider
- [ ] **Transaction Log** → link to the transaction log entry
- [ ] **Outstanding Amount** → `0.00`
- [ ] A **Payment Entry** was auto-created (check `/app/payment-entry`)
- [ ] A **Transaction Log** entry exists (check `/app/mobile-payment-transaction-log`)

### Step 6: Verify Payment Entry
1. Go to `/app/payment-entry`
2. Find the latest entry
3. Check:
   - [ ] Mode of Payment = ZAAD/SAHAL/EVCPlus/Edahab
   - [ ] Paid Amount matches invoice
   - [ ] Reference links to the Sales Invoice
   - [ ] Mobile Payment Reference field is populated
   - [ ] Mobile Transaction Log field links to the log

---

## 5. Test: POS Awesome Payment

### Pre-requisites
- POS Awesome installed
- POS Profile created with at least one mobile payment mode (ZAAD, SAHAL, etc.)

### Step 1: Open POS
1. Go to `/app/point-of-sale` or `/app/pos-awesome`
2. Select your POS Profile

### Step 2: Create a Sale
1. Add items to the cart
2. Click **Pay** / **Checkout**

### Step 3: Use Mobile Money
1. In the payment screen, look for the green **"Mobile Money"** button
   - If the button doesn't appear, check that `bench build` was run
2. Click **"Mobile Money"**
3. A dialog appears — same as Sales Invoice flow
4. Select provider, method, enter phone number
5. Click **"Proceed to Pay"**

### Step 4: Verify
- [ ] USSD push sent to customer's phone
- [ ] Payment confirmed after PIN entry
- [ ] POS Invoice created and linked
- [ ] Transaction logged

### If the Mobile Money Button Doesn't Appear:
```bash
bench build
bench --site YOUR_SITE clear-cache
```
Then hard-refresh the browser (Ctrl+Shift+R).

Also ensure the POS Profile has ZAAD/SAHAL/EVCPlus/Edahab as payment methods.

---

## 6. Test: WooCommerce Payment

### Pre-requisites
- WooCommerce ERPNext connector app installed and configured
- WooCommerce store connected and syncing orders
- In Mobile Payment Settings → WooCommerce section:
  - [x] Enable WooCommerce Integration
  - Auto Initiate Payment: `Yes` (to auto-trigger payment on sync)
  - Default Provider: `WaafiPay` or `Edahab`

### Step 1: Create an Order in WooCommerce
1. Go to your WooCommerce store
2. Place an order (or create one manually in WooCommerce admin)
3. Make sure the customer has a phone number

### Step 2: Wait for Sync
1. The WooCommerce connector syncs the order into ERPNext as a Sales Order → Sales Invoice
2. Our app's `on_submit` hook detects the WooCommerce order

### Step 3: Auto-Payment (if enabled)
If "Auto Initiate Payment" is enabled:
1. The app automatically sends a USSD push to the customer's phone
2. Customer confirms on their phone
3. Payment Entry created in ERPNext
4. Invoice marked as paid
5. Payment status synced back to WooCommerce

### Step 4: Manual Payment (if auto not enabled)
1. Open the synced Sales Invoice in ERPNext
2. Use "Pay with Mobile" as described in Section 4

### Step 5: Verify
- [ ] WooCommerce order synced to ERPNext
- [ ] Sales Invoice created
- [ ] Payment initiated (auto or manual)
- [ ] Payment Entry created on success
- [ ] Transaction Log shows `Source: WooCommerce`
- [ ] WooCommerce order ID stored in transaction log

### Scheduler Jobs (automatic):
- Every 10 minutes: `process_pending_wc_orders()` picks up unprocessed WooCommerce orders
- Every 2 minutes: `poll_pending_transactions()` checks pending payment statuses
- Every 5 minutes: `retry_failed_transactions()` retries failed payments

---

## 7. Test: Hosted Payment Page (HPP)

HPP generates a payment link instead of sending a USSD push. Useful for remote customers.

### Step 1: Start Payment
1. On a submitted Sales Invoice, click **Payment → Pay with Mobile**
2. Select a payment method
3. Change **Payment Flow** to **"Hosted Payment Page (HPP)"**
4. Click **"Proceed to Pay"** (no phone number needed)

### Step 2: Payment Page
1. A dialog appears with the payment URL
2. Two buttons:
   - **"Open Payment Page"** → opens the WaafiPay/Edahab page in a new tab
   - **"Copy Link"** → copies the URL to clipboard (send via WhatsApp/SMS)

### Step 3: Customer Completes Payment
1. Customer opens the link
2. Sees WaafiPay/Edahab's hosted payment page
3. Selects wallet, enters phone + PIN
4. Confirms payment

### Step 4: Webhook Callback
1. WaafiPay/Edahab sends a webhook to your site:
   - WaafiPay: `YOUR_SITE/api/method/mobile_payments.api.webhooks.waafipay_hpp_return`
   - Edahab: `YOUR_SITE/api/method/mobile_payments.api.webhooks.edahab_hpp_return`
2. Our app processes the callback
3. Payment Entry created, invoice updated

### Step 5: Verify
- [ ] HPP URL generated and displayed
- [ ] Payment page opens correctly
- [ ] Webhook received after payment
- [ ] Invoice marked as paid
- [ ] Transaction Log shows `Flow Type: Hosted Payment Page (HPP)`

---

## 8. Test: Dashboard & Reports

### Mobile Payment Dashboard
1. Go to `/app/mobile-payments-dashboard`
2. Check:
   - [ ] Summary cards show: Total Transactions, Successful, Failed, Pending
   - [ ] Daily transaction chart renders
   - [ ] Provider breakdown (WaafiPay vs Edahab) displays
   - [ ] Recent transactions list shows latest entries
   - [ ] "Export Transactions" option works

### Transaction Log List
1. Go to `/app/mobile-payment-transaction-log`
2. Check:
   - [ ] List view shows all transactions
   - [ ] Filters work: by status, provider, date range
   - [ ] Click a transaction → detail view shows full info
   - [ ] Request/response payloads visible
   - [ ] Timeline shows status changes

### Settlement Reconciliation Report
1. Go to `/app/query-report/Mobile Payment Settlement`
2. Set date range and filters
3. Check:
   - [ ] Report generates with transaction data
   - [ ] Filters by provider, status, reconciliation status work
   - [ ] Can export to CSV/Excel (download button)

### Modes of Payment
1. Go to `/app/mode-of-payment`
2. Check: ZAAD, SAHAL, EVCPlus, Edahab, WaafiPay all exist with Type = Phone

---

## 9. Test: Security Features

### Webhook Signature Validation
1. Enable "Webhook Validation" in Settings
2. Set a Webhook Secret Key
3. Send a test request to the webhook endpoint without a valid signature
4. Expected: Request rejected with 403 error

### IP Whitelist
1. In Settings, add a specific IP to the whitelist (e.g., `1.2.3.4`)
2. Send a webhook from a different IP
3. Expected: Request rejected
4. Remove the whitelist or add your server's IP to restore access

### Replay Protection
1. Enable "Replay Protection" in Settings
2. Send the same webhook payload twice
3. Expected: Second request rejected as a replay

### Encrypted Credentials
1. In Settings, enter API keys and save
2. Check the database directly: `SELECT waafipay_api_key FROM tabSingles WHERE doctype='Mobile Payment Settings'`
3. Expected: Value is encrypted (Frappe Password field), not plain text

---

## 10. Test: Error Handling & Retry

### Wrong Phone Number
1. Initiate a payment with an invalid phone number (e.g., `0000000000`)
2. Expected: Error dialog shows "Payment failed" with provider's error message
3. Check Transaction Log: Status = `Failed`, error stored in response payload

### Customer Rejects Payment
1. Initiate a USSD push payment
2. Customer declines the prompt on their phone
3. Expected: Dialog shows failure after polling. Transaction Log status = `Failed`

### Customer Doesn't Respond (Timeout)
1. Initiate a USSD push payment
2. Don't respond on the phone
3. Expected: After 2 minutes of polling, timeout dialog appears
4. Transaction Log status = `Pending` → will be retried by background job

### Retry Logic
1. Check `/app/mobile-payment-transaction-log` for failed transactions
2. The scheduler retries failed transactions every 5 minutes
3. Retry uses exponential backoff (1st retry: 5 min, 2nd: 10 min, 3rd: 20 min)
4. Max retry count is configurable in settings

### Network Error
1. Set an invalid Base URL in Settings (e.g., `https://invalid-url.example.com`)
2. Try to initiate a payment
3. Expected: Error dialog shows connection error
4. Transaction Log shows the error

---

## 11. Test: Test Connection Button

1. Go to `/app/mobile-payment-settings`
2. Enable WaafiPay and fill in credentials
3. Click **Save**
4. After page reloads, click **Test Connection → Test WaafiPay Connection**
5. Expected results:
   - **Valid credentials**: Green message — "WaafiPay credentials are valid!"
   - **Invalid credentials**: Red message — "Authentication failed: ..."
   - **Wrong URL**: Red message — "Cannot connect to ..."
6. Repeat for Edahab if enabled

---

## 12. Troubleshooting

### "Pay with Mobile" button doesn't appear
```bash
bench build
bench --site YOUR_SITE clear-cache
```
Then hard-refresh browser (Ctrl+Shift+R). Also check:
- Invoice must be **submitted** (docstatus = 1)
- Invoice must have **outstanding amount > 0**

### "Mobile payments are not enabled" message
- Go to Mobile Payment Settings
- Enable at least one provider (WaafiPay or Edahab)
- Fill in all required credentials
- Save

### Payment stuck on "Processing"
- Check Transaction Log for the transaction
- The background poller runs every 2 minutes
- The retry job runs every 5 minutes
- You can manually check status via the Transaction Log's action buttons

### Webhook not received
- Ensure your site is publicly accessible (not localhost)
- Check the callback URL in Settings matches your site URL
- Verify webhook secret matches what's configured at the provider
- Check Error Log (`/app/error-log`) for webhook processing errors

### Custom fields not showing on Sales Invoice
```bash
bench --site YOUR_SITE migrate
bench clear-cache
```

### POS Awesome button not showing
- Verify POS Awesome app is installed
- Add ZAAD/SAHAL/EVCPlus/Edahab to POS Profile payment methods
- Run `bench build` and clear cache

### WooCommerce orders not auto-processing
- Check WooCommerce Integration is enabled in Settings
- Check Auto Initiate Payment is enabled
- Verify WooCommerce connector is syncing orders
- Check scheduler is running: `bench doctor`
- Check Error Log for any processing errors

---

## Quick Reference: URLs

| Page | URL |
|------|-----|
| Mobile Payment Settings | `/app/mobile-payment-settings` |
| Transaction Log | `/app/mobile-payment-transaction-log` |
| Dashboard | `/app/mobile-payments-dashboard` |
| Settlement Report | `/app/query-report/Mobile Payment Settlement` |
| Mode of Payment | `/app/mode-of-payment` |
| Error Log | `/app/error-log` |
| Sales Invoice | `/app/sales-invoice` |
| Payment Entry | `/app/payment-entry` |
| POS | `/app/point-of-sale` |

---

## Quick Reference: Webhook Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/api/method/mobile_payments.api.webhooks.waafipay_callback` | WaafiPay USSD payment callback |
| `/api/method/mobile_payments.api.webhooks.edahab_callback` | Edahab USSD payment callback |
| `/api/method/mobile_payments.api.webhooks.waafipay_hpp_return` | WaafiPay HPP redirect callback |
| `/api/method/mobile_payments.api.webhooks.edahab_hpp_return` | Edahab HPP redirect callback |
| `/api/method/mobile_payments.api.woocommerce.woocommerce_payment_webhook` | WooCommerce payment webhook |

---

## Quick Reference: Background Jobs

| Job | Frequency | Purpose |
|-----|-----------|---------|
| `poll_pending_transactions` | Every 2 minutes | Check status of pending payments |
| `retry_failed_transactions` | Every 5 minutes | Retry failed payments with backoff |
| `process_pending_wc_orders` | Every 10 minutes | Process WooCommerce orders |
| `run_daily_reconciliation` | Daily at 1:00 AM | Reconcile transactions with Payment Entries |
