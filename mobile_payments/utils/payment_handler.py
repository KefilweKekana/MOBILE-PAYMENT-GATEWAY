"""
Payment Handler
Core module for processing successful payments:
- Creates Payment Entries in ERPNext
- Updates Sales Invoice status
- Handles polling for pending transactions
- Manages retry logic for failed transactions
"""
from __future__ import unicode_literals

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date, flt, getdate


# ──────────────────────────────────────────────
# Payment Processing
# ──────────────────────────────────────────────

def process_successful_payment(transaction_log, invoice_id=None):
    """
    Process a successful mobile payment.
    Creates Payment Entry and updates Sales Invoice.

    Called via frappe.enqueue after webhook/callback confirms payment.

    Args:
        transaction_log: Name of Mobile Payment Transaction Log
        invoice_id: Name of Sales Invoice (optional, read from log if not provided)
    """
    try:
        log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

        if log.status != "Completed":
            frappe.logger("mobile_payments").warning(
                f"Skipping payment processing for {log.name}: status is {log.status}"
            )
            return

        # Already processed?
        if log.payment_entry:
            frappe.logger("mobile_payments").info(
                f"Payment Entry already exists for {log.name}: {log.payment_entry}"
            )
            return

        invoice_id = invoice_id or log.sales_invoice
        if not invoice_id:
            frappe.logger("mobile_payments").warning(
                f"No Sales Invoice linked to transaction {log.name}"
            )
            return

        settings = frappe.get_single("Mobile Payment Settings")

        # Never touch a Draft invoice — POS Awesome may still be submitting it.
        # wait until it's submitted before creating payment entry.
        for doctype in ("POS Invoice", "Sales Invoice"):
            if frappe.db.exists(doctype, invoice_id):
                doc_status = frappe.db.get_value(doctype, invoice_id, "docstatus")
                if doc_status == 0:  # Draft
                    frappe.logger("mobile_payments").info(
                        f"Skipping payment processing for {log.name}: "
                        f"{doctype} {invoice_id} is still Draft"
                    )
                    return

        # Always create the payment entry / mark invoice paid.
        # auto_create_payment_entry being False only skips creation; we treat
        # its absence as enabled so HPP payments are never silently swallowed.
        result = _create_paid_invoice(log, invoice_id, settings)
        if result:
            log.payment_entry = result.get("reference", "")
            log.save(ignore_permissions=True)

            # Update Sales Invoice custom fields
            _update_sales_invoice(log, invoice_id, settings)

            frappe.logger("mobile_payments").info(
                f"Payment processed: {log.name} → {result.get('type')}: {result.get('reference')}"
            )
        else:
            frappe.logger("mobile_payments").warning(
                f"_create_paid_invoice returned None for {log.name} / {invoice_id} — "
                f"check logs for errors"
            )

        frappe.db.commit()

    except Exception as e:
        frappe.log_error(
            message=(
                f"Error processing payment for {transaction_log}: {str(e)}\n"
                f"{frappe.get_traceback()}"
            ),
            title="Payment Processing Error",
        )


def _create_paid_invoice(log, invoice_id, settings):
    """
    Record a successful mobile payment against a Sales Invoice using ERPNext's
    standard accounting documents.

    Accounting flow:
      Draft invoice   → submit it cleanly first, then create a Payment Entry.
      Submitted invoice → create a Payment Entry directly.
      POS invoice (is_pos=1, already has a payments child table) → update the
                         payments child table row AND create a Payment Entry so
                         the GL is always correct (Dr Cash/Bank, Cr AR).

    A Payment Entry is the only ERPNext document that correctly posts:
        Dr  Cash / Bank account (paid_to)
        Cr  Accounts Receivable (paid_from = invoice.debit_to)
    and reconciles the outstanding_amount on the Sales Invoice via the
    Payment Entry Reference child table.

    The Sales Invoice payments child table (paid_amount button) is a POS-only
    display feature — it does NOT post GL entries on its own. Setting it on a
    non-POS invoice would corrupt the general ledger. We therefore always
    create a Payment Entry for the accounting side, and only update the
    payments child table when the invoice was already flagged as a POS invoice.

    Args:
        log: Mobile Payment Transaction Log document
        invoice_id: Sales Invoice name
        settings: Mobile Payment Settings document

    Returns:
        dict with 'type' and 'reference' keys, or None on failure
    """
    try:
        invoice = frappe.get_doc("Sales Invoice", invoice_id)

        if invoice.docstatus == 2:
            frappe.logger("mobile_payments").warning(
                f"Sales Invoice {invoice_id} is cancelled — cannot post payment"
            )
            return None

        # ── Step 1: Resolve Mode of Payment and target account ──────────────
        mode_of_payment = _get_mode_of_payment(log.payment_method or log.provider)
        payment_account = _get_payment_account(mode_of_payment, invoice.company, settings)

        # ── Step 2: Submit draft invoices before creating the Payment Entry ──
        # A Payment Entry can only be linked to a submitted (docstatus=1) invoice.
        if invoice.docstatus == 0:
            try:
                invoice.flags.ignore_permissions = True
                invoice.submit()
                frappe.db.commit()
                # Reload to get updated docstatus and outstanding_amount
                invoice = frappe.get_doc("Sales Invoice", invoice_id)
                frappe.logger("mobile_payments").info(
                    f"Submitted draft Sales Invoice {invoice_id} before payment entry"
                )
            except Exception as e:
                frappe.log_error(
                    message=(
                        f"Failed to submit draft Sales Invoice {invoice_id}: {str(e)}\n"
                        f"{frappe.get_traceback()}"
                    ),
                    title="Invoice Submit Error",
                )
                return None

        # ── Step 3: Guard against double-payment ────────────────────────────
        outstanding = flt(invoice.outstanding_amount)
        if outstanding <= 0:
            frappe.logger("mobile_payments").info(
                f"Sales Invoice {invoice_id} already fully paid (outstanding={outstanding})"
            )
            return None

        payment_amount = min(flt(log.amount) or outstanding, outstanding)

        # ── Step 4: Build and submit the Payment Entry ──────────────────────
        # This is the canonical ERPNext way to record a payment and reconcile AR.
        # It posts:
        #   Dr  payment_account  (cash/bank)    payment_amount
        #   Cr  invoice.debit_to (AR account)   payment_amount
        # and sets outstanding_amount = 0 on the Sales Invoice via the
        # Payment Entry Reference row.
        remarks = (
            f"Mobile Payment via {log.provider}"
            + (f" / {log.payment_method}" if log.payment_method else "")
            + f"\nProvider Ref: {log.provider_transaction_id or log.name}"
            + (f"\nPhone: {log.phone_number}" if log.phone_number and log.phone_number != 'HPP' else "")
        )

        pe = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "posting_date": getdate(now_datetime()),
            "company": invoice.company,
            "cost_center": frappe.db.get_value("Company", invoice.company, "cost_center"),
            "mode_of_payment": mode_of_payment,
            "party_type": "Customer",
            "party": invoice.customer,
            "party_name": invoice.customer_name,
            # paid_from = the AR account on the invoice (credit side)
            "paid_from": invoice.debit_to,
            "paid_from_account_currency": invoice.currency,
            # paid_to = the cash/bank account linked to the Mode of Payment (debit side)
            "paid_to": payment_account,
            "paid_to_account_currency": frappe.db.get_value("Account", payment_account, "account_currency"),
            "paid_amount": payment_amount,
            "received_amount": payment_amount,
            "source_exchange_rate": flt(invoice.conversion_rate) or 1,
            "target_exchange_rate": 1,
            "reference_no": log.provider_transaction_id or log.name,
            "reference_date": getdate(now_datetime()),
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_id,
                "total_amount": flt(invoice.grand_total),
                "outstanding_amount": outstanding,
                "allocated_amount": payment_amount,
                "exchange_rate": flt(invoice.conversion_rate) or 1,
            }],
            "remarks": remarks,
        })

        pe.flags.ignore_permissions = True
        pe.insert()
        pe.submit()
        frappe.db.commit()

        frappe.logger("mobile_payments").info(
            f"Payment Entry {pe.name} submitted for {invoice_id} "
            f"| {mode_of_payment} | Amount: {payment_amount} {invoice.currency}"
        )

        # ── Step 5: Update the Sales Invoice payments child table ────────────
        # If the invoice is a POS invoice (is_pos=1), ERPNext shows a
        # "paid_amount" button and a payments child table on the form.
        # We sync that row so the POS view reflects the correct payment method
        # and amount — purely cosmetic, the GL truth is in the Payment Entry.
        if invoice.is_pos:
            _sync_pos_payments_row(invoice, mode_of_payment, payment_account, payment_amount, log)

        return {"type": "Payment Entry", "reference": pe.name}

    except Exception as e:
        frappe.log_error(
            message=(
                f"Failed to process payment for {invoice_id}: {str(e)}\n"
                f"{frappe.get_traceback()}"
            ),
            title="Payment Processing Error",
        )
        return None


def _sync_pos_payments_row(invoice, mode_of_payment, payment_account, payment_amount, log):
    """
    Sync the Sales Invoice payments child table for POS invoices.

    For POS invoices (is_pos=1) ERPNext displays a payments child table that
    shows which Mode of Payment was used. This is a display/reconciliation aid
    only — the actual GL posting is done by the Payment Entry. We update this
    table so the POS closing screen and reports show the correct MOP breakdown.

    Args:
        invoice: Sales Invoice document (already submitted)
        mode_of_payment: Mode of Payment name
        payment_account: Account name
        payment_amount: Amount paid
        log: Mobile Payment Transaction Log
    """
    try:
        # Reload fresh to avoid stale data after PE submission
        inv = frappe.get_doc("Sales Invoice", invoice.name)

        # Find existing row for this MOP or add a new one
        existing = next(
            (row for row in inv.payments if row.mode_of_payment == mode_of_payment),
            None,
        )

        ref_no = log.provider_transaction_id or log.name

        if existing:
            existing.amount = payment_amount
            existing.account = payment_account
            existing.base_amount = payment_amount
        else:
            inv.append("payments", {
                "mode_of_payment": mode_of_payment,
                "account": payment_account,
                "type": "Phone",
                "amount": payment_amount,
                "base_amount": payment_amount,
            })

        inv.paid_amount = payment_amount
        inv.base_paid_amount = payment_amount
        inv.flags.ignore_permissions = True
        inv.flags.ignore_validate_update_after_submit = True
        inv.save()
        frappe.db.commit()

        frappe.logger("mobile_payments").info(
            f"Synced POS payments row on {invoice.name} | MOP: {mode_of_payment} | {payment_amount}"
        )

    except Exception as e:
        # Non-fatal — the Payment Entry is already submitted and the GL is correct.
        # A failure here only affects the POS display row.
        frappe.log_error(
            message=f"Failed to sync POS payments row on {invoice.name}: {str(e)}",
            title="POS Payments Sync Warning",
        )


def _update_sales_invoice(log, invoice_id, settings):
    """
    Update Sales Invoice with mobile payment details.

    Args:
        log: Mobile Payment Transaction Log document
        invoice_id: Sales Invoice name
        settings: Mobile Payment Settings document
    """
    try:
        # Only update submitted invoices — never touch a Draft
        doc_status = frappe.db.get_value("Sales Invoice", invoice_id, "docstatus")
        if doc_status != 1:
            frappe.logger("mobile_payments").info(
                f"Skipping invoice update for {invoice_id}: docstatus={doc_status} (not submitted)"
            )
            return

        frappe.db.set_value(
            "Sales Invoice",
            invoice_id,
            {
                "mobile_payment_status": "Completed",
                "mobile_payment_provider": log.provider,
                "mobile_payment_method": log.payment_method,
                "mobile_payment_phone": log.phone_number,
                "mobile_payment_reference": log.provider_transaction_id or log.transaction_id,
                "mobile_payment_transaction_id": log.name,
            },
            update_modified=False,
        )

    except Exception as e:
        frappe.log_error(
            message=f"Failed to update Sales Invoice {invoice_id}: {str(e)}",
            title="Invoice Update Error",
        )


def _get_mode_of_payment(method):
    """
    Map a mobile payment method name to an ERPNext Mode of Payment.

    Resolution order:
    1. Exact match (ZAAD, SAHAL, EVCPlus, Edahab, WaafiPay)
    2. Any existing MOP whose name contains the method keyword
    3. First Phone-type MOP in the system
    4. Raise — a missing MOP means accounting would be incomplete
    """
    method_map = {
        "ZAAD": "ZAAD",
        "SAHAL": "SAHAL",
        "EVCPlus": "EVCPlus",
        "Edahab": "Edahab",
        "WaafiPay": "WaafiPay",
    }

    mode = method_map.get(method, method or "WaafiPay")

    if frappe.db.exists("Mode of Payment", mode):
        return mode

    # Try case-insensitive partial match
    match = frappe.db.get_value(
        "Mode of Payment",
        {"name": ["like", f"%{mode}%"]},
        "name",
    )
    if match:
        frappe.logger("mobile_payments").warning(
            f"Mode of Payment '{mode}' not found exactly, using '{match}'"
        )
        return match

    # Try first Phone-type MOP
    phone_mop = frappe.db.get_value(
        "Mode of Payment", {"type": "Phone"}, "name"
    )
    if phone_mop:
        frappe.logger("mobile_payments").warning(
            f"Mode of Payment '{mode}' not found, falling back to Phone MOP '{phone_mop}'"
        )
        return phone_mop

    frappe.throw(
        _(
            "Mode of Payment '{0}' does not exist in ERPNext. "
            "Please create it under Accounts → Mode of Payment and "
            "link it to the correct cash/bank account for your company."
        ).format(mode)
    )


def _get_payment_account(mode_of_payment, company, settings):
    """
    Get the payment account for the given mode of payment and company.

    Args:
        mode_of_payment: Mode of Payment name
        company: Company name
        settings: Mobile Payment Settings document

    Returns:
        Account name
    """
    # Try getting account from Mode of Payment
    account = frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mode_of_payment, "company": company},
        "default_account",
    )

    if account:
        return account

    # Fall back to settings default
    if settings.default_payment_account:
        return settings.default_payment_account

    # Fall back to company default receivable account
    default = frappe.db.get_value(
        "Company", company, "default_cash_account"
    ) or frappe.db.get_value(
        "Company", company, "default_bank_account"
    )

    if not default:
        frappe.throw(
            _(
                "No payment account configured. Please set a default account "
                "in Mobile Payment Settings or Mode of Payment: {0}"
            ).format(mode_of_payment)
        )

    return default


# ──────────────────────────────────────────────
# Polling for Pending Transactions
# ──────────────────────────────────────────────

def poll_pending_transactions():
    """
    Scheduled task: Poll payment providers for status of pending transactions.
    Runs every 2 minutes via scheduler.
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return

    # Find pending/processing transactions older than 30 seconds
    cutoff = add_to_date(now_datetime(), seconds=-30)
    pending_logs = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "status": ["in", ("Pending", "Processing", "Initiated")],
            "initiated_at": ["<", cutoff],
        },
        fields=["name", "provider", "provider_reference", "provider_transaction_id",
                "flow_type", "initiated_at", "last_polled_at"],
        order_by="initiated_at asc",
        limit=20,
    )

    for txn in pending_logs:
        try:
            _poll_single_transaction(txn, settings)
        except Exception as e:
            frappe.log_error(
                message=f"Error polling transaction {txn['name']}: {str(e)}",
                title="Payment Polling Error",
            )

    frappe.db.commit()


def _poll_single_transaction(txn, settings):
    """Poll a single pending transaction for status update."""
    log = frappe.get_doc("Mobile Payment Transaction Log", txn["name"])

    # Check timeout
    timeout_seconds = settings.transaction_timeout or 120
    timeout_at = add_to_date(log.initiated_at, seconds=timeout_seconds * 3)

    if now_datetime() > timeout_at:
        log.update_status("Timeout", error_message="Transaction timed out during polling")
        return

    # Update last polled timestamp
    log.last_polled_at = now_datetime()
    log.save(ignore_permissions=True)

    # Poll based on provider
    if log.provider == "WaafiPay":
        _poll_waafipay(log)
    elif log.provider == "Edahab":
        _poll_edahab(log)


def _poll_waafipay(log):
    """Poll WaafiPay for transaction status."""
    from mobile_payments.api.waafipay import WaafiPayClient

    try:
        client = WaafiPayClient()
        reference = log.provider_transaction_id or log.provider_reference

        if not reference:
            return

        result = client.check_transaction_status(reference)

        if result.get("success"):
            state = result.get("state", "")

            if state == "APPROVED":
                log.update_status(
                    "Completed",
                    provider_transaction_id=result.get("transaction_id"),
                    response_payload=result.get("raw_response"),
                )

                # Route to the correct handler
                if log.patient_appointment:
                    frappe.enqueue(
                        process_successful_appointment_payment,
                        queue="short",
                        transaction_log=log.name,
                    )
                else:
                    frappe.enqueue(
                        process_successful_payment,
                        queue="short",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )

            elif state in ("DECLINED", "CANCELLED"):
                log.update_status(
                    "Failed",
                    error_message=f"Payment {state.lower()} (polled)",
                    response_payload=result.get("raw_response"),
                )

    except Exception as e:
        frappe.log_error(
            message=f"WaafiPay poll error for {log.name}: {str(e)}",
            title="WaafiPay Polling Error",
        )


def _poll_edahab(log):
    """Poll Edahab for transaction status."""
    from mobile_payments.api.edahab import EdahabClient

    try:
        client = EdahabClient()
        invoice_id = log.provider_reference

        if not invoice_id:
            return

        result = client.check_transaction_status(invoice_id)

        if result.get("success"):
            invoice_status = result.get("invoice_status", "")

            if invoice_status == "Paid" or result.get("status_code") == 0:
                log.update_status(
                    "Completed",
                    provider_transaction_id=result.get("transaction_id"),
                    response_payload=result.get("raw_response"),
                )

                # Route to the correct handler
                if log.patient_appointment:
                    frappe.enqueue(
                        process_successful_appointment_payment,
                        queue="short",
                        transaction_log=log.name,
                    )
                else:
                    frappe.enqueue(
                        process_successful_payment,
                        queue="short",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                    )

            elif invoice_status in ("Cancelled", "Expired"):
                log.update_status(
                    "Cancelled",
                    error_message=f"Payment {invoice_status.lower()} (polled)",
                    response_payload=result.get("raw_response"),
                )

    except Exception as e:
        frappe.log_error(
            message=f"Edahab poll error for {log.name}: {str(e)}",
            title="Edahab Polling Error",
        )


# ──────────────────────────────────────────────
# Retry Failed Transactions
# ──────────────────────────────────────────────

def retry_failed_transactions():
    """
    Scheduled task: Retry failed transactions that are eligible.
    Runs every 5 minutes via scheduler.
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return

    max_retries = settings.max_retry_attempts or 3

    # Find transactions scheduled for retry
    retryable = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "status": "Retrying",
            "retry_count": ["<", max_retries],
            "next_retry_at": ["<=", now_datetime()],
        },
        fields=["name", "provider", "payment_method", "phone_number",
                "amount", "sales_invoice", "flow_type"],
        order_by="next_retry_at asc",
        limit=10,
    )

    for txn in retryable:
        try:
            _retry_single_transaction(txn)
        except Exception as e:
            frappe.log_error(
                message=f"Error retrying transaction {txn['name']}: {str(e)}",
                title="Payment Retry Error",
            )

    frappe.db.commit()


def _retry_single_transaction(txn):
    """Retry a single failed transaction."""
    if txn["flow_type"] != "Purchase API":
        # HPP transactions cannot be auto-retried
        return

    if txn["provider"] == "WaafiPay":
        from mobile_payments.api.waafipay import WaafiPayClient
        client = WaafiPayClient()
        # Fetch currency from the original transaction log
        txn_currency = frappe.db.get_value(
            "Mobile Payment Transaction Log", txn["name"], "currency"
        ) or "USD"
        result = client.purchase_request(
            phone=txn["phone_number"],
            amount=txn["amount"],
            method=txn["payment_method"],
            invoice_id=txn["sales_invoice"],
            currency=txn_currency,
            transaction_log=txn["name"],
        )
    elif txn["provider"] == "Edahab":
        from mobile_payments.api.edahab import EdahabClient
        client = EdahabClient()
        txn_currency = frappe.db.get_value(
            "Mobile Payment Transaction Log", txn["name"], "currency"
        ) or "USD"
        result = client.purchase_request(
            phone=txn["phone_number"],
            amount=txn["amount"],
            invoice_id=txn["sales_invoice"],
            currency=txn_currency,
            transaction_log=txn["name"],
        )
    else:
        return

    if result.get("success"):
        frappe.enqueue(
            process_successful_payment,
            queue="short",
            transaction_log=txn["name"],
            invoice_id=txn["sales_invoice"],
        )

    frappe.logger("mobile_payments").info(
        f"Retry result for {txn['name']}: {'Success' if result.get('success') else 'Failed'}"
    )


# ──────────────────────────────────────────────
# Doc Event Hooks
# ──────────────────────────────────────────────

def on_sales_invoice_submit(doc, method):
    """
    Hook: Called when a Sales Invoice / POS Invoice is submitted.
    Finds any completed mobile payment transaction for this invoice
    and updates the invoice's mobile payment fields.
    """
    try:
        # Search by doc.name AND by any reference_id / pos_invoice fields
        # The transaction log may store the POS Invoice name in sales_invoice
        log_data = None

        # Try direct match first
        logs = frappe.get_all(
            "Mobile Payment Transaction Log",
            filters={"sales_invoice": doc.name, "status": "Completed"},
            fields=["name", "provider", "payment_method", "phone_number",
                    "provider_transaction_id", "transaction_id", "sales_invoice"],
            order_by="modified desc",
            limit=1,
        )
        if logs:
            log_data = logs[0]

        # If not found, check if this Sales Invoice was consolidated from a POS Invoice
        if not log_data and doc.doctype == "Sales Invoice":
            pos_invoices = frappe.get_all(
                "POS Invoice Merge Log Detail",
                filters={"sales_invoice": doc.name},
                fields=["pos_invoice"],
                limit=10,
            )
            pos_names = [r.pos_invoice for r in pos_invoices]

            # Also check pos_invoice field directly on doc if it exists
            if hasattr(doc, "pos_invoice") and doc.pos_invoice:
                pos_names.append(doc.pos_invoice)

            if pos_names:
                logs2 = frappe.get_all(
                    "Mobile Payment Transaction Log",
                    filters={"sales_invoice": ["in", pos_names], "status": "Completed"},
                    fields=["name", "provider", "payment_method", "phone_number",
                            "provider_transaction_id", "transaction_id", "sales_invoice"],
                    order_by="modified desc",
                    limit=1,
                )
                if logs2:
                    log_data = logs2[0]

        if not log_data:
            frappe.logger("mobile_payments").info(
                f"on_sales_invoice_submit: no completed transaction found for {doc.name}"
            )
            return

        # Update fields — doc is now submitted (docstatus=1)
        frappe.db.set_value(
            doc.doctype,
            doc.name,
            {
                "mobile_payment_status":         "Completed",
                "mobile_payment_provider":       log_data.provider,
                "mobile_payment_method":         log_data.payment_method,
                "mobile_payment_phone":          log_data.phone_number,
                "mobile_payment_reference":      log_data.provider_transaction_id or log_data.transaction_id,
                "mobile_payment_transaction_id": log_data.name,
            },
            update_modified=False,
        )

        frappe.logger("mobile_payments").info(
            f"on_sales_invoice_submit: updated fields on {doc.name} from log {log_data.name}"
        )

        # Enqueue payment processing (creates Payment Entry etc.)
        frappe.enqueue(
            process_successful_payment,
            queue="short",
            transaction_log=log_data.name,
            invoice_id=doc.name,
        )

    except Exception as e:
        frappe.log_error(
            message=f"on_sales_invoice_submit failed for {doc.name}: {str(e)}",
            title="Mobile Payment Hook Error",
        )


def on_payment_entry_submit(doc, method):
    """
    Hook: Called when a Payment Entry is submitted.
    Updates linked Mobile Payment Transaction Log.
    """
    if doc.mobile_payment_transaction_id:
        try:
            log = frappe.get_doc(
                "Mobile Payment Transaction Log",
                doc.mobile_payment_transaction_id,
            )
            if not log.payment_entry:
                log.payment_entry = doc.name
                log.save(ignore_permissions=True)
        except Exception:
            pass


# ──────────────────────────────────────────────
# Patient Appointment Payment API
# ──────────────────────────────────────────────

@frappe.whitelist()
def initiate_appointment_payment(phone, amount, appointment_name,
                                  description="", currency="USD",
                                  provider="WaafiPay", method="ZAAD"):
    """
    Initiate a USSD Push mobile payment for a Patient Appointment.
    On success, auto-creates a Sales Invoice with the mode of payment
    set to the provider used (ZAAD, SAHAL, EVCPlus, Edahab).

    Args:
        phone: Customer's mobile wallet number
        amount: Payment amount
        appointment_name: Patient Appointment name
        description: Payment description
        currency: Currency code (USD or SLSH)
        provider: WaafiPay or Edahab
        method: Payment method (ZAAD, SAHAL, EVCPlus, Edahab)

    Returns:
        dict with payment result (includes sales_invoice on success)
    """
    frappe.has_permission("Patient Appointment", "read", throw=True)

    appointment = frappe.get_doc("Patient Appointment", appointment_name)
    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Payment amount must be greater than zero"))

    # Auto-detect currency from appointment or system default
    currency = _normalize_currency(currency)
    if currency == "USD":
        # Try to get from appointment
        appt_currency = getattr(appointment, "currency", None)
        if appt_currency:
            currency = _normalize_currency(appt_currency)

    # Auto-fetch phone if not supplied or empty
    if not phone or not str(phone).strip():
        details = get_appointment_payment_details(appointment_name)
        phone = details.get("phone", "")

    if not phone:
        frappe.throw(
            _(
                "No mobile number found for this patient. "
                "Please add a mobile number to the Patient or linked Customer record."
            )
        )

    desc = description or f"Appointment payment - {appointment.patient_name or appointment_name}"

    # Do NOT pass invoice_id=appointment_name to prevent it being stored
    # in the sales_invoice Link field (which expects a Sales Invoice name).
    if provider == "Edahab":
        from mobile_payments.api.edahab import EdahabClient
        client = EdahabClient()
        result = client.purchase_request(
            phone=phone,
            amount=amount,
            invoice_id=None,
            description=desc,
            currency=currency,
        )
    else:
        from mobile_payments.api.waafipay import WaafiPayClient
        client = WaafiPayClient()
        result = client.purchase_request(
            phone=phone,
            amount=amount,
            method=method,
            invoice_id=None,
            description=desc,
            currency=currency,
        )

    # Tag the transaction log with the appointment reference
    log_name = result.get("transaction_log")
    if log_name:
        update_fields = {"custom_source": "Patient Appointment"}
        # patient_appointment column may not exist until bench migrate runs
        if frappe.db.has_column("Mobile Payment Transaction Log", "patient_appointment"):
            update_fields["patient_appointment"] = appointment_name
        frappe.db.set_value(
            "Mobile Payment Transaction Log", log_name,
            update_fields,
            update_modified=False,
        )

    # Handle result
    if result.get("success"):
        # Create Sales Invoice + Payment Entry synchronously
        si = _create_appointment_invoice(
            appointment_name=appointment_name,
            provider=provider,
            method=method if provider == "WaafiPay" else "Edahab",
            phone=phone,
            amount=amount,
            currency=currency,
            transaction_log_name=log_name,
        )
        if si:
            result["sales_invoice"] = si.name

        frappe.db.set_value("Patient Appointment", appointment_name, {
            "mobile_payment_status": "Completed",
            "mobile_payment_provider": provider,
            "mobile_payment_method": method if provider == "WaafiPay" else "Edahab",
            "mobile_payment_phone": phone,
            "mobile_payment_reference": result.get("transaction_id", ""),
            "mobile_payment_transaction_id": log_name or "",
        }, update_modified=False)

    elif result.get("pending"):
        frappe.db.set_value("Patient Appointment", appointment_name, {
            "mobile_payment_status": "Pending",
            "mobile_payment_provider": provider,
            "mobile_payment_method": method if provider == "WaafiPay" else "Edahab",
            "mobile_payment_phone": phone,
            "mobile_payment_transaction_id": log_name or "",
        }, update_modified=False)

    return result


@frappe.whitelist()
def initiate_appointment_hpp(appointment_name, provider, method="ZAAD",
                              amount=0, currency="USD"):
    """
    Create an HPP session for a Patient Appointment.

    Args:
        appointment_name: Patient Appointment name
        provider: WaafiPay or Edahab
        method: Payment method
        amount: Payment amount
        currency: Currency code (USD or SLSH)

    Returns:
        dict with HPP URL and transaction info
    """
    frappe.has_permission("Patient Appointment", "read", throw=True)

    appointment = frappe.get_doc("Patient Appointment", appointment_name)
    amount = flt(amount)
    if amount <= 0:
        amount = flt(appointment.paid_amount or appointment.billing_amount
                     or appointment.consultation_fee or 0)
    if amount <= 0:
        frappe.throw(_("Payment amount must be greater than zero"))

    currency = _normalize_currency(currency)
    description = f"Appointment payment - {appointment.patient_name or appointment_name}"

    # Do NOT pass appointment_name as invoice_id (Link field to Sales Invoice).
    if provider == "Edahab":
        from mobile_payments.api.edahab import EdahabClient
        client = EdahabClient()
        result = client.create_hpp_session(
            amount=amount,
            invoice_id=None,
            description=description,
            currency=currency,
        )
    else:
        from mobile_payments.api.waafipay import WaafiPayClient
        client = WaafiPayClient()
        result = client.create_hpp_session(
            amount=amount,
            invoice_id=None,
            description=description,
            currency=currency,
        )

    if result.get("success"):
        # Tag the log with the appointment reference
        log_name = result.get("transaction_log")
        if log_name:
            update_fields = {"custom_source": "Patient Appointment"}
            if frappe.db.has_column("Mobile Payment Transaction Log", "patient_appointment"):
                update_fields["patient_appointment"] = appointment_name
            frappe.db.set_value(
                "Mobile Payment Transaction Log", log_name,
                update_fields,
                update_modified=False,
            )
        frappe.db.set_value("Patient Appointment", appointment_name, {
            "mobile_payment_status": "Pending",
            "mobile_payment_provider": provider,
            "mobile_payment_method": method if provider == "WaafiPay" else "Edahab",
            "mobile_payment_transaction_id": log_name or "",
        }, update_modified=False)

    return result


# ──────────────────────────────────────────────
# Appointment → Sales Invoice Creation
# ──────────────────────────────────────────────

def _create_appointment_invoice(appointment_name, provider, method,
                                 phone, amount, currency="USD",
                                 transaction_log_name=None):
    """
    Create a Sales Invoice for a Patient Appointment after successful
    mobile payment.  Also creates and submits a Payment Entry so the
    invoice is fully paid.

    Args:
        appointment_name: Patient Appointment name
        provider: Payment provider (WaafiPay / Edahab)
        method: Payment method (ZAAD / SAHAL / EVCPlus / Edahab)
        phone: Customer phone number
        amount: Payment amount
        currency: Currency code
        transaction_log_name: Mobile Payment Transaction Log name

    Returns:
        Sales Invoice doc on success, None on failure
    """
    try:
        appointment = frappe.get_doc("Patient Appointment", appointment_name)
        company = appointment.company or frappe.defaults.get_default("company")

        # ── Get or create Customer from Patient ──
        customer = _get_customer_for_patient(appointment.patient)
        if not customer:
            frappe.log_error(
                message=(
                    f"Cannot create Sales Invoice for appointment {appointment_name}: "
                    f"no Customer linked to Patient {appointment.patient}. "
                    f"Please link a Customer to the Patient record."
                ),
                title="Appointment Invoice Error",
            )
            return None

        # ── Determine billing item ──
        item_code = _get_appointment_billing_item(appointment, company)
        if not item_code:
            frappe.log_error(
                message=(
                    f"Cannot create Sales Invoice for appointment {appointment_name}: "
                    f"no billing item found. Set a billing_item on the Appointment Type "
                    f"or create an Item in the 'Healthcare Services' item group."
                ),
                title="Appointment Invoice Error",
            )
            return None

        # ── Resolve mode of payment ──
        mode_of_payment = _get_mode_of_payment(method or provider)

        # ── Create Sales Invoice as PAID (with payment inside) ──
        si = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": customer,
            "company": company,
            "posting_date": getdate(now_datetime()),
            "due_date": getdate(now_datetime()),
            "is_pos": 1,
            "items": [{
                "item_code": item_code,
                "qty": 1,
                "rate": flt(amount),
                "description": (
                    f"Consultation – {appointment.patient_name or appointment.patient}"
                ),
            }],
            "payments": [{
                "mode_of_payment": mode_of_payment,
                "account": _get_payment_account(
                    mode_of_payment, company,
                    frappe.get_single("Mobile Payment Settings")
                ),
                "amount": flt(amount),
                "type": "Phone",
            }],
            # Mobile payment custom fields
            "mobile_payment_status": "Completed",
            "mobile_payment_provider": provider,
            "mobile_payment_method": method,
            "mobile_payment_phone": phone,
            "mobile_payment_transaction_id": transaction_log_name or "",
            "remarks": (
                f"Mobile Payment via {provider} ({method})\n"
                f"Phone: {phone}"
            ),
        })

        # Set POS Profile if available
        pos_profile = frappe.db.get_value(
            "POS Profile",
            {"company": company, "disabled": 0},
            "name"
        )
        if pos_profile:
            si.pos_profile = pos_profile

        # Set patient field if it exists on Sales Invoice (Healthcare module)
        if "patient" in [f.fieldname for f in frappe.get_meta("Sales Invoice").fields]:
            si.patient = appointment.patient

        si.insert(ignore_permissions=True)
        si.submit()

        frappe.logger("mobile_payments").info(
            f"Created paid Sales Invoice {si.name} for appointment {appointment_name}"
        )

        # Link transaction log to the invoice
        if transaction_log_name:
            log = frappe.get_doc(
                "Mobile Payment Transaction Log", transaction_log_name
            )
            log.db_set("sales_invoice", si.name, update_modified=False)

        # ── Link invoice back to appointment ──
        frappe.db.set_value(
            "Patient Appointment", appointment_name,
            "mobile_payment_sales_invoice", si.name,
            update_modified=False,
        )

        frappe.db.commit()
        return si

    except Exception as e:
        frappe.log_error(
            message=(
                f"Failed to create Sales Invoice for appointment "
                f"{appointment_name}: {str(e)}\n{frappe.get_traceback()}"
            ),
            title="Appointment Invoice Error",
        )
        return None


def _create_payment_entry_for_appointment(si, mode_of_payment, amount,
                                           company, settings,
                                           transaction_log_name=None,
                                           phone="", provider="",
                                           method=""):
    """Create and submit a Payment Entry against an appointment Sales Invoice."""
    try:
        payment_account = _get_payment_account(mode_of_payment, company, settings)

        pe = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "posting_date": getdate(now_datetime()),
            "company": company,
            "mode_of_payment": mode_of_payment,
            "party_type": "Customer",
            "party": si.customer,
            "paid_from": si.debit_to,
            "paid_to": payment_account,
            "paid_amount": flt(amount),
            "received_amount": flt(amount),
            "source_exchange_rate": 1,
            "target_exchange_rate": 1,
            "reference_no": transaction_log_name or method,
            "reference_date": getdate(now_datetime()),
            "mobile_payment_reference": transaction_log_name or "",
            "mobile_payment_transaction_id": transaction_log_name or "",
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": si.name,
                "total_amount": si.grand_total,
                "outstanding_amount": si.outstanding_amount,
                "allocated_amount": flt(amount),
            }],
            "remarks": (
                f"Mobile Payment via {provider} ({method})\n"
                f"Patient Appointment payment\n"
                f"Phone: {phone}"
            ),
        })
        pe.insert(ignore_permissions=True)
        pe.submit()

        frappe.logger("mobile_payments").info(
            f"Created Payment Entry {pe.name} for appointment invoice {si.name}"
        )
        return pe

    except Exception as e:
        frappe.log_error(
            message=(
                f"Failed to create Payment Entry for appointment invoice "
                f"{si.name}: {str(e)}\n{frappe.get_traceback()}"
            ),
            title="Appointment PE Error",
        )
        return None


def process_successful_appointment_payment(transaction_log):
    """
    Process a successful appointment payment that was confirmed via polling.
    Creates a Sales Invoice + Payment Entry, same as sync path.

    Called by frappe.enqueue from _poll_waafipay / _poll_edahab when
    a transaction with patient_appointment is completed.
    """
    try:
        log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

        if log.status != "Completed":
            return
        if log.sales_invoice:
            # Invoice already created
            return
        if not log.patient_appointment:
            return

        si = _create_appointment_invoice(
            appointment_name=log.patient_appointment,
            provider=log.provider,
            method=log.payment_method,
            phone=log.phone_number,
            amount=log.amount,
            currency=log.currency or "USD",
            transaction_log_name=log.name,
        )

        if si:
            # Update appointment status
            frappe.db.set_value("Patient Appointment", log.patient_appointment, {
                "mobile_payment_status": "Completed",
                "mobile_payment_reference": log.provider_transaction_id or log.transaction_id or "",
            }, update_modified=False)

        frappe.db.commit()

    except Exception as e:
        frappe.log_error(
            message=(
                f"Error processing appointment payment for {transaction_log}: "
                f"{str(e)}\n{frappe.get_traceback()}"
            ),
            title="Appointment Payment Error",
        )


def _get_customer_for_patient(patient_name):
    """
    Get the Customer linked to a Patient.
    If none exists, try Healthcare's built-in helper or create one.

    Args:
        patient_name: Patient document name

    Returns:
        Customer name (str) or None
    """
    if not patient_name:
        return None

    patient = frappe.get_doc("Patient", patient_name)

    # 1. Direct link
    if patient.customer:
        return patient.customer

    # 2. Try Healthcare module helpers
    for module_path in (
        "healthcare.healthcare.utils",
        "erpnext.healthcare.utils",
    ):
        try:
            utils = frappe.get_module(module_path)
            fn = getattr(utils, "create_customer", None) or getattr(
                utils, "get_customer", None
            )
            if fn:
                customer = fn(patient_name)
                if customer:
                    return customer
        except (ImportError, AttributeError, Exception):
            pass

    # 3. Auto-create a basic Customer
    try:
        customer_group = (
            frappe.db.get_single_value("Selling Settings", "customer_group")
            or "Individual"
        )
        territory = (
            frappe.db.get_single_value("Selling Settings", "territory")
            or "All Territories"
        )

        cust = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": patient.patient_name or patient_name,
            "customer_type": "Individual",
            "customer_group": customer_group,
            "territory": territory,
        })
        cust.insert(ignore_permissions=True)

        # Link back to patient
        frappe.db.set_value(
            "Patient", patient_name, "customer", cust.name,
            update_modified=False,
        )

        frappe.logger("mobile_payments").info(
            f"Auto-created Customer {cust.name} for Patient {patient_name}"
        )
        return cust.name

    except Exception as e:
        frappe.log_error(
            message=f"Failed to create Customer for Patient {patient_name}: {e}",
            title="Patient Customer Error",
        )
        return None


def _get_appointment_billing_item(appointment, company=None):
    """
    Resolve the billing item for a Patient Appointment.

    Priority:
        1. appointment.billing_item (Healthcare standard field)
        2. Appointment Type → billing_item
        3. First item in 'Healthcare Services' item group
        4. First non-stock service item

    Returns:
        Item code (str) or None
    """
    # 1. Direct field on appointment
    billing_item = getattr(appointment, "billing_item", None) or ""
    if billing_item and frappe.db.exists("Item", billing_item):
        return billing_item

    # 2. From Appointment Type
    if appointment.appointment_type:
        try:
            at_item = frappe.db.get_value(
                "Appointment Type",
                appointment.appointment_type,
                "billing_item",
            )
            if at_item and frappe.db.exists("Item", at_item):
                return at_item
        except Exception:
            pass

    # 3. Healthcare Services item group
    healthcare_item = frappe.db.get_value(
        "Item",
        {"item_group": "Healthcare Services", "disabled": 0},
        "name",
    )
    if healthcare_item:
        return healthcare_item

    # 4. Any Services item group
    service_item = frappe.db.get_value(
        "Item",
        {"item_group": "Services", "disabled": 0},
        "name",
    )
    if service_item:
        return service_item

    # 5. Any non-stock item
    fallback = frappe.db.get_value(
        "Item",
        {"is_stock_item": 0, "disabled": 0},
        "name",
    )
    return fallback


def _normalize_currency(currency):
    """Normalize currency codes. Map Somali Shilling variants to SLSH."""
    if not currency:
        return "USD"
    currency = currency.strip().upper()
    # Common aliases for Somali Shilling
    if currency in ("SLSH", "SOS", "SHILLING", "SH", "SOMALI SHILLING"):
        return "SLSH"
    return currency


# ──────────────────────────────────────────────
# Utility API Endpoints
# ──────────────────────────────────────────────

@frappe.whitelist()
def get_appointment_payment_details(appointment_name):
    """
    Fetch appointment amount, currency, and patient mobile for auto-population
    in the 'Pay with Mobile' dialog.

    Args:
        appointment_name: Patient Appointment name

    Returns:
        dict: {amount, currency, phone, patient_name}
    """
    if not appointment_name or not frappe.db.exists("Patient Appointment", appointment_name):
        return {}

    appointment = frappe.get_doc("Patient Appointment", appointment_name)

    amount = flt(
        getattr(appointment, "paid_amount", 0)
        or getattr(appointment, "billing_amount", 0)
        or getattr(appointment, "consultation_fee", 0)
        or 0
    )

    # Try to get currency from appointment, else use system default
    currency = (
        getattr(appointment, "currency", None)
        or frappe.defaults.get_global_default("currency")
        or "USD"
    )

    # Auto-fetch patient mobile number - multiple strategies
    phone = ""
    source = ""

    # Strategy 1: mobile_no directly on the appointment
    if getattr(appointment, "mobile_no", None):
        phone = appointment.mobile_no
        source = "Appointment.mobile_no"

    # Strategy 2: Patient.mobile
    if not phone and appointment.patient:
        try:
            patient = frappe.get_doc("Patient", appointment.patient)
            phone = patient.mobile or patient.phone or ""
            if phone:
                source = "Patient"
        except Exception:
            pass

    # Strategy 3: Customer linked to Patient → Contact
    if not phone and appointment.patient:
        try:
            customer = frappe.db.get_value("Patient", appointment.patient, "customer")
            if customer:
                from mobile_payments.api.pos import get_customer_phone
                phone_data = get_customer_phone(customer)
                phone = phone_data.get("phone", "")
                source = phone_data.get("source", "")
        except Exception:
            pass

    return {
        "amount": amount,
        "currency": currency,
        "phone": phone,
        "source": source,
        "patient_name": appointment.patient_name or appointment.patient or "",
    }



    """
    Get the current status of a mobile payment transaction.
    Called by frontend to poll for updates.

    Args:
        transaction_log: Name of Mobile Payment Transaction Log

    Returns:
        dict with status and details
    """
    frappe.has_permission("Sales Invoice", "read", throw=True)

    log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)
    return {
        "status": log.status,
        "provider": log.provider,
        "payment_method": log.payment_method,
        "amount": log.amount,
        "provider_transaction_id": log.provider_transaction_id,
        "payment_entry": log.payment_entry,
        "sales_invoice": log.sales_invoice,
        "error_message": log.error_message,
        "initiated_at": str(log.initiated_at) if log.initiated_at else None,
        "completed_at": str(log.completed_at) if log.completed_at else None,
    }


@frappe.whitelist()
def get_available_methods():
    """
    Get available mobile payment methods based on settings.

    Returns:
        dict with available providers and methods
    """
    settings = frappe.get_single("Mobile Payment Settings")

    methods = []

    if settings.waafipay_enabled:
        for method in settings.get_supported_waafipay_methods():
            methods.append({
                "provider": "WaafiPay",
                "method": method,
                "label": method,
                "flow_types": ["Purchase API", "HPP"],
            })

    if settings.edahab_enabled:
        methods.append({
            "provider": "Edahab",
            "method": "Edahab",
            "label": "Edahab",
            "flow_types": ["Purchase API", "HPP"],
        })

    return {
        "enabled": settings.enabled,
        "methods": methods,
        "default_provider": settings.default_provider,
    }


@frappe.whitelist()
def cancel_pending_payment(transaction_log):
    """
    Cancel a pending mobile payment transaction.

    Args:
        transaction_log: Name of Mobile Payment Transaction Log

    Returns:
        dict with result
    """
    frappe.has_permission("Sales Invoice", "write", throw=True)

    log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

    if log.status not in ("Initiated", "Pending", "Processing", "Retrying"):
        return {
            "success": False,
            "message": f"Cannot cancel transaction in '{log.status}' status",
        }

    log.update_status("Cancelled", error_message="Cancelled by user")

    # Update Sales Invoice
    if log.sales_invoice:
        frappe.db.set_value(
            "Sales Invoice",
            log.sales_invoice,
            "mobile_payment_status",
            "Cancelled",
            update_modified=False,
        )

    return {"success": True, "message": "Payment cancelled"}


@frappe.whitelist()
def get_payment_status(transaction_log):
    """
    Check payment status for a transaction (called from Sales Invoice form).

    Delegates to the POS payment status checker which handles both
    WaafiPay and Edahab provider status lookups.

    Args:
        transaction_log: Mobile Payment Transaction Log name

    Returns:
        dict with status, transaction_id, provider_transaction_id, etc.
    """
    from mobile_payments.api.pos import check_pos_payment_status
    return check_pos_payment_status(transaction_log)
