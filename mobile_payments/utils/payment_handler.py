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

        # Create Payment Entry
        if settings.auto_create_payment_entry:
            payment_entry = _create_payment_entry(log, invoice_id, settings)
            if payment_entry:
                log.payment_entry = payment_entry.name
                log.save(ignore_permissions=True)

                # Update Sales Invoice custom fields
                _update_sales_invoice(log, invoice_id, settings)

                frappe.logger("mobile_payments").info(
                    f"Payment processed: {log.name} → PE: {payment_entry.name}"
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


def _create_payment_entry(log, invoice_id, settings):
    """
    Create a Payment Entry for the completed transaction.

    Args:
        log: Mobile Payment Transaction Log document
        invoice_id: Sales Invoice name
        settings: Mobile Payment Settings document

    Returns:
        Payment Entry document
    """
    try:
        invoice = frappe.get_doc("Sales Invoice", invoice_id)

        if invoice.docstatus != 1:
            frappe.logger("mobile_payments").warning(
                f"Sales Invoice {invoice_id} is not submitted (docstatus={invoice.docstatus})"
            )
            return None

        # Check if already fully paid
        outstanding = flt(invoice.outstanding_amount)
        if outstanding <= 0:
            frappe.logger("mobile_payments").info(
                f"Sales Invoice {invoice_id} already fully paid"
            )
            return None

        # Determine payment amount (minimum of transaction amount and outstanding)
        payment_amount = min(flt(log.amount), outstanding)

        # Determine mode of payment
        mode_of_payment = _get_mode_of_payment(log.payment_method or log.provider)

        # Determine payment account
        payment_account = _get_payment_account(
            mode_of_payment, invoice.company, settings
        )

        # Create Payment Entry
        pe = frappe.get_doc(
            {
                "doctype": "Payment Entry",
                "payment_type": "Receive",
                "posting_date": getdate(now_datetime()),
                "company": invoice.company,
                "mode_of_payment": mode_of_payment,
                "party_type": "Customer",
                "party": invoice.customer,
                "party_name": invoice.customer_name,
                "paid_from": invoice.debit_to,
                "paid_to": payment_account,
                "paid_amount": payment_amount,
                "received_amount": payment_amount,
                "source_exchange_rate": 1,
                "target_exchange_rate": 1,
                "reference_no": log.provider_transaction_id or log.transaction_id,
                "reference_date": getdate(now_datetime()),
                "mobile_payment_reference": log.transaction_id,
                "mobile_payment_transaction_id": log.name,
                "references": [
                    {
                        "reference_doctype": "Sales Invoice",
                        "reference_name": invoice_id,
                        "total_amount": invoice.grand_total,
                        "outstanding_amount": outstanding,
                        "allocated_amount": payment_amount,
                    }
                ],
                "remarks": (
                    f"Mobile Payment via {log.provider} ({log.payment_method})\n"
                    f"Transaction: {log.transaction_id}\n"
                    f"Provider Ref: {log.provider_transaction_id}\n"
                    f"Phone: {log.phone_number}"
                ),
            }
        )

        pe.insert(ignore_permissions=True)

        # Auto-submit the Payment Entry
        pe.submit()

        frappe.logger("mobile_payments").info(
            f"Created & submitted Payment Entry {pe.name} for {invoice_id} "
            f"| Amount: {payment_amount}"
        )

        return pe

    except Exception as e:
        frappe.log_error(
            message=(
                f"Failed to create Payment Entry for {invoice_id}: {str(e)}\n"
                f"{frappe.get_traceback()}"
            ),
            title="Payment Entry Error",
        )
        return None


def _update_sales_invoice(log, invoice_id, settings):
    """
    Update Sales Invoice with mobile payment details.

    Args:
        log: Mobile Payment Transaction Log document
        invoice_id: Sales Invoice name
        settings: Mobile Payment Settings document
    """
    try:
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
    """Map payment method to ERPNext Mode of Payment."""
    method_map = {
        "ZAAD": "ZAAD",
        "SAHAL": "SAHAL",
        "EVCPlus": "EVCPlus",
        "Edahab": "Edahab",
        "WaafiPay": "WaafiPay",
    }

    mode = method_map.get(method, "WaafiPay")

    # Verify the Mode of Payment exists
    if not frappe.db.exists("Mode of Payment", mode):
        # Fall back to Cash if specific mode doesn't exist
        frappe.logger("mobile_payments").warning(
            f"Mode of Payment '{mode}' not found, falling back to default"
        )
        return frappe.db.get_single_value("POSProfile", "payments") or "Cash"

    return mode


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
    Hook: Called when a Sales Invoice is submitted.
    Can be used to auto-trigger mobile payment if configured.
    """
    # This is a placeholder for optional auto-payment trigger
    pass


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

    # Map currency aliases
    currency = _normalize_currency(currency)

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
        frappe.db.set_value(
            "Mobile Payment Transaction Log", log_name,
            {
                "patient_appointment": appointment_name,
                "custom_source": "Patient Appointment",
            },
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
            frappe.db.set_value(
                "Mobile Payment Transaction Log", log_name,
                {
                    "patient_appointment": appointment_name,
                    "custom_source": "Patient Appointment",
                },
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

        # ── Create Sales Invoice ──
        si = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": customer,
            "company": company,
            "posting_date": getdate(now_datetime()),
            "due_date": getdate(now_datetime()),
            "items": [{
                "item_code": item_code,
                "qty": 1,
                "rate": flt(amount),
                "description": (
                    f"Consultation – {appointment.patient_name or appointment.patient}"
                ),
            }],
            # Mobile payment custom fields
            "mobile_payment_status": "Completed",
            "mobile_payment_provider": provider,
            "mobile_payment_method": method,
            "mobile_payment_phone": phone,
            "mobile_payment_transaction_id": transaction_log_name or "",
        })

        # Set patient field if it exists on Sales Invoice (Healthcare module)
        if "patient" in [f.fieldname for f in frappe.get_meta("Sales Invoice").fields]:
            si.patient = appointment.patient

        si.insert(ignore_permissions=True)
        si.submit()

        frappe.logger("mobile_payments").info(
            f"Created Sales Invoice {si.name} for appointment {appointment_name}"
        )

        # ── Create Payment Entry against the new invoice ──
        settings = frappe.get_single("Mobile Payment Settings")
        if settings.auto_create_payment_entry:
            log = None
            if transaction_log_name:
                log = frappe.get_doc(
                    "Mobile Payment Transaction Log", transaction_log_name
                )
                # Store the invoice reference on the log now
                log.db_set("sales_invoice", si.name, update_modified=False)

            pe = _create_payment_entry_for_appointment(
                si, mode_of_payment, amount, company, settings,
                transaction_log_name=transaction_log_name,
                phone=phone,
                provider=provider,
                method=method,
            )
            if pe and log:
                log.db_set("payment_entry", pe.name, update_modified=False)

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
def get_payment_status(transaction_log):
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
