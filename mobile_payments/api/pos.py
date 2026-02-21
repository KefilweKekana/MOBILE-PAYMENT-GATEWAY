"""
POS Awesome Integration - Backend API
Handles mobile money payments initiated from POS Awesome POS interface.

POS flow differs from Sales Invoice:
1. POS creates a POS Invoice (child of Sales Invoice) on submit
2. Payment must be handled within the POS session context
3. We create a "pending" transaction, process payment, then confirm POS submission
"""
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, getdate


@frappe.whitelist()
def get_mobile_payment_methods():
    """
    Return available mobile payment methods for POS Awesome.
    Called when the POS loads to populate the payment method selector.

    Returns:
        dict: {methods: [...], enabled: bool}
    """
    settings = frappe.get_single("Mobile Payment Settings")

    if not settings.enabled:
        return {"enabled": False, "methods": []}

    methods = []

    # WaafiPay methods
    if settings.waafipay_enabled:
        supported = (settings.waafipay_supported_methods or "ZAAD,SAHAL,EVCPlus").split(",")
        for method in supported:
            method = method.strip()
            if method:
                methods.append({
                    "provider": "WaafiPay",
                    "method": method,
                    "label": method,
                    "icon": _get_method_icon(method),
                })

    # Edahab
    if settings.edahab_enabled:
        methods.append({
            "provider": "Edahab",
            "method": "Edahab",
            "label": "Edahab",
            "icon": _get_method_icon("Edahab"),
        })

    return {"enabled": True, "methods": methods}


@frappe.whitelist()
def initiate_pos_payment(provider, method, phone, amount, currency=None,
                          pos_profile=None, customer=None, invoice_name=None,
                          account_type=None):
    """
    Initiate a mobile payment from POS Awesome.

    In POS flow, the invoice may not exist yet when payment is initiated.
    We create a transaction log and process the payment. The POS will
    link the invoice after submission.

    Args:
        provider: WaafiPay or Edahab
        method: ZAAD, SAHAL, EVCPlus, or Edahab
        phone: Customer phone number
        amount: Payment amount
        currency: USD or SLSH (default USD)
        pos_profile: POS Profile name (optional)
        customer: Customer name (optional)
        invoice_name: POS Invoice/Sales Invoice name if already created

    Returns:
        dict with success, transaction_log, message
    """
    settings = frappe.get_single("Mobile Payment Settings")

    if not settings.enabled:
        return {"success": False, "message": _("Mobile payments are disabled")}

    amount = flt(amount)
    if amount <= 0:
        return {"success": False, "message": _("Invalid payment amount")}

    # Validate phone / merchant till number.
    # Merchant accounts use a short WaafiPay till number (e.g. "7853") that must
    # be sent as-is — no country code prefix, no minimum length like mobile numbers.
    # Subscriber wallets still require a full international mobile number (9-15 digits).
    is_merchant = account_type and "merchant" in str(account_type or "").lower()

    phone = (phone or "").strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]

    if not phone:
        label = _("Merchant till number") if is_merchant else _("Customer mobile number")
        return {
            "success": False,
            "message": _("{0} is required for mobile payment.").format(label),
        }
    if not phone.isdigit():
        return {
            "success": False,
            "message": _(
                "Invalid number — must contain only digits "
                "(e.g. 7853 for merchant till, 252612345678 for subscriber)."
            ),
        }
    if not is_merchant and (len(phone) < 9 or len(phone) > 15):
        return {
            "success": False,
            "message": _("Subscriber phone number must be between 9 and 15 digits."),
        }

    # Auto-detect currency from invoice if not provided
    if not currency and invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
        currency = frappe.db.get_value("Sales Invoice", invoice_name, "currency")
    if not currency and invoice_name and frappe.db.exists("POS Invoice", invoice_name):
        currency = frappe.db.get_value("POS Invoice", invoice_name, "currency")
    currency = currency or frappe.defaults.get_global_default("currency") or "USD"

    # Validate credentials before proceeding
    try:
        if provider == "Edahab":
            creds = settings.get_edahab_credentials()
            if not creds.get("api_key"):
                return {"success": False, "message": _("Edahab API Key is not configured")}
            if not creds.get("api_secret"):
                return {"success": False, "message": _("Edahab API Secret is not configured")}
        else:
            creds = settings.get_waafipay_credentials()
            missing = []
            if not creds.get("merchant_uid"):
                missing.append("Merchant UID")
            if not creds.get("api_key"):
                missing.append("API Key")
            if not creds.get("store_id"):
                missing.append("Store ID")
            if missing:
                return {
                    "success": False,
                    "message": _("WaafiPay configuration missing: {0}. Please update Mobile Payment Settings.").format(", ".join(missing)),
                }
    except Exception as e:
        return {"success": False, "message": str(e)}

    # Build payment description matching Sales Invoice format.
    # In POS flow the invoice often doesn't exist yet (created on SUBMIT),
    # so we fall back to customer name for a meaningful Edahab/WaafiPay reference.
    if invoice_name:
        payment_description = f"Payment for {invoice_name}"
    elif customer and customer.lower() not in ("walk-in", "walk in", ""):
        payment_description = f"Payment for {customer}"
    else:
        payment_description = f"POS Payment - {pos_profile or 'Walk-in'}"

    # Create Transaction Log
    log = frappe.get_doc({
        "doctype": "Mobile Payment Transaction Log",
        "provider": provider,
        "payment_method": method,
        "phone_number": phone,
        "amount": amount,
        "currency": currency,
        "status": "Initiated",
        "sales_invoice": invoice_name or "",
        "initiated_at": now_datetime(),
        "custom_pos_profile": pos_profile or "",
        "custom_pos_customer": customer or "",
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    # Initiate payment with provider
    try:
        if provider == "Edahab":
            from mobile_payments.api.edahab import EdahabClient
            client = EdahabClient()
            result = client.purchase_request(
                phone=phone,
                amount=amount,
                currency=log.currency,
                description=payment_description,
                transaction_log=log.name,
            )
        else:
            from mobile_payments.api.waafipay import WaafiPayClient
            client = WaafiPayClient()
            result = client.purchase_request(
                phone=phone,
                amount=amount,
                method=method,
                currency=log.currency,
                description=payment_description,
                transaction_log=log.name,
                account_type=account_type or "Subscriber (Mobile Wallet)",
            )

        if result.get("success"):
            # Reload to avoid timestamp conflict — the client already saved the log
            frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                "status": "Completed",
                "provider_transaction_id": result.get("transaction_id", ""),
                "completed_at": now_datetime(),
            }, update_modified=False)
            frappe.db.commit()

            return {
                "success": True,
                "transaction_log": log.name,
                "transaction_id": result.get("transaction_id", ""),
                "provider_transaction_id": result.get("transaction_id", ""),
                "message": _("Payment successful"),
            }

        elif result.get("pending"):
            frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                "status": "Pending",
            }, update_modified=False)
            frappe.db.commit()

            return {
                "success": False,
                "pending": True,
                "transaction_log": log.name,
                "message": _("Payment pending confirmation"),
            }

        else:
            frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                "status": "Failed",
                "error_message": result.get("message", "Payment failed"),
                "completed_at": now_datetime(),
            }, update_modified=False)
            frappe.db.commit()

            return {
                "success": False,
                "transaction_log": log.name,
                "message": result.get("message", "Payment failed"),
            }

    except Exception as e:
        frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
            "status": "Failed",
            "error_message": str(e),
            "completed_at": now_datetime(),
        }, update_modified=False)
        frappe.db.commit()

        frappe.log_error(
            message=f"POS Mobile Payment Error: {str(e)}\n{frappe.get_traceback()}",
            title="POS Payment Error",
        )

        return {
            "success": False,
            "transaction_log": log.name,
            "message": _("Payment processing error: {0}").format(str(e)),
        }


@frappe.whitelist()
def link_pos_invoice(transaction_log, invoice_name):
    """
    Link a POS Invoice / Sales Invoice to an existing transaction log.
    Called after POS Awesome submits the invoice.

    Args:
        transaction_log: Transaction Log name
        invoice_name: POS Invoice or Sales Invoice name
    """
    if not transaction_log or not invoice_name:
        return {"success": False, "message": _("Missing parameters")}

    log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

    if log.sales_invoice and log.sales_invoice != invoice_name:
        frappe.logger("mobile_payments").warning(
            f"Transaction {log.name} already linked to {log.sales_invoice}, "
            f"updating to {invoice_name}"
        )

    frappe.db.set_value("Mobile Payment Transaction Log", transaction_log, {
        "sales_invoice": invoice_name,
    }, update_modified=False)

    # If payment already completed, create Payment Entry
    if log.status == "Completed" and not log.payment_entry:
        settings = frappe.get_single("Mobile Payment Settings")
        if settings.auto_create_payment_entry:
            frappe.enqueue(
                "mobile_payments.utils.payment_handler.process_successful_payment",
                transaction_log=log.name,
                invoice_id=invoice_name,
                queue="short",
            )

    frappe.db.commit()

    return {
        "success": True,
        "message": _("Invoice linked successfully"),
        "status": log.status,
        "payment_entry": log.payment_entry or "",
    }


@frappe.whitelist()
def check_pos_payment_status(transaction_log):
    """
    Check payment status for a POS transaction.

    Args:
        transaction_log: Transaction Log name

    Returns:
        dict with status details
    """
    log = frappe.get_doc("Mobile Payment Transaction Log", transaction_log)

    result = {
        "status": log.status,
        "transaction_id": log.transaction_id,
        "provider_transaction_id": log.provider_transaction_id or "",
        "payment_entry": log.payment_entry or "",
        "error_message": log.error_message or "",
    }

    # If still pending, try to check with provider
    if log.status in ("Pending", "Initiated"):
        try:
            if log.provider == "Edahab":
                from mobile_payments.api.edahab import EdahabClient
                client = EdahabClient()
                status_result = client.check_transaction_status(log.transaction_id)
            else:
                from mobile_payments.api.waafipay import WaafiPayClient
                client = WaafiPayClient()
                status_result = client.check_transaction_status(log.transaction_id)

            if status_result.get("success"):
                txn_id = status_result.get("transaction_id", "")
                frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                    "status": "Completed",
                    "provider_transaction_id": txn_id,
                    "completed_at": now_datetime(),
                }, update_modified=False)
                frappe.db.commit()

                result["status"] = "Completed"
                result["provider_transaction_id"] = txn_id

                # Process payment if invoice is linked
                if log.sales_invoice and not log.payment_entry:
                    frappe.enqueue(
                        "mobile_payments.utils.payment_handler.process_successful_payment",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                        queue="short",
                    )

            elif status_result.get("failed"):
                err_msg = status_result.get("message", "")
                frappe.db.set_value("Mobile Payment Transaction Log", log.name, {
                    "status": "Failed",
                    "error_message": err_msg,
                    "completed_at": now_datetime(),
                }, update_modified=False)
                frappe.db.commit()

                result["status"] = "Failed"
                result["error_message"] = err_msg

        except Exception as e:
            frappe.log_error(
                message=f"POS status check error: {str(e)}",
                title="POS Status Check Error",
            )

    return result


def _get_method_icon(method):
    """Return icon identifier for a payment method."""
    icons = {
        "ZAAD": "zaad",
        "SAHAL": "sahal",
        "EVCPlus": "evcplus",
        "Edahab": "edahab",
    }
    return icons.get(method, "mobile")


@frappe.whitelist()
def get_invoice_payment_details(invoice_name):
    """
    Fetch invoice amount, currency and customer phone for POS payment auto-population.

    Returns the total due and the best-available customer phone so the POS
    payment dialog can pre-fill both fields without the cashier having to
    type anything.

    Args:
        invoice_name: Sales Invoice or POS Invoice name

    Returns:
        dict: {amount, currency, phone, customer, source}
    """
    if not invoice_name:
        return {}

    result = {"amount": 0, "currency": "USD", "phone": "", "customer": "", "source": ""}

    # Try Sales Invoice first, then POS Invoice
    for doctype in ("Sales Invoice", "POS Invoice"):
        if frappe.db.exists(doctype, invoice_name):
            doc = frappe.get_doc(doctype, invoice_name)
            result["amount"] = flt(doc.grand_total)
            result["currency"] = doc.currency or frappe.defaults.get_global_default("currency") or "USD"
            result["customer"] = doc.customer or ""
            break

    # Auto-fetch customer phone
    if result["customer"]:
        phone_data = get_customer_phone(result["customer"])
        result["phone"] = phone_data.get("phone", "")
        result["source"] = phone_data.get("source", "")

    return result


@frappe.whitelist()
def get_customer_phone(customer):
    """
    Robustly fetch a customer's phone number using multiple fallback strategies.
    This is called from POS to reliably get the phone for any customer.

    Lookup order:
    1. Contact linked to Customer via Dynamic Link (primary contact preferred)
    2. Contact Phone child table entries
    3. Customer.mobile_no field
    4. Patient linked to Customer (Healthcare module)

    Args:
        customer: Customer name/ID

    Returns:
        dict: {phone: "252...", source: "Contact|Customer|Patient"}
    """
    if not customer:
        return {"phone": "", "source": ""}

    phone = ""
    source = ""

    try:
        # Strategy 1: Contact via Dynamic Link
        contacts = frappe.get_all(
            "Contact",
            filters=[
                ["Dynamic Link", "link_doctype", "=", "Customer"],
                ["Dynamic Link", "link_name", "=", customer],
            ],
            fields=["name", "mobile_no", "phone", "is_primary_contact"],
            order_by="is_primary_contact desc",
            limit_page_length=5,
        )
        for contact in contacts:
            if contact.mobile_no:
                phone = contact.mobile_no
                source = "Contact"
                break
            if contact.phone and not phone:
                phone = contact.phone
                source = "Contact"

        # Strategy 2: Contact Phone child table
        if not phone and contacts:
            for contact in contacts:
                phone_entries = frappe.get_all(
                    "Contact Phone",
                    filters={"parent": contact.name},
                    fields=["phone", "is_primary_phone", "is_primary_mobile_no"],
                    order_by="is_primary_mobile_no desc, is_primary_phone desc",
                    limit_page_length=3,
                )
                for entry in phone_entries:
                    if entry.phone:
                        phone = entry.phone
                        source = "Contact Phone"
                        break
                if phone:
                    break

        # Strategy 3: Customer.mobile_no
        if not phone:
            cust = frappe.db.get_value("Customer", customer, ["mobile_no"], as_dict=True)
            if cust and cust.mobile_no:
                phone = cust.mobile_no
                source = "Customer"

        # Strategy 4: Patient linked to Customer (Healthcare)
        if not phone:
            try:
                patient = frappe.db.get_value(
                    "Patient", {"customer": customer}, ["mobile", "phone"], as_dict=True
                )
                if patient:
                    phone = patient.mobile or patient.phone or ""
                    if phone:
                        source = "Patient"
            except Exception:
                pass  # Patient doctype may not exist

    except Exception as e:
        frappe.log_error(
            message=f"Phone fetch error for {customer}: {str(e)}",
            title="POS Phone Fetch Error",
        )

    return {"phone": phone, "source": source}


@frappe.whitelist()
def create_payment_request_override(doc):
    """
    Override for posawesome.posawesome.api.posapp.create_payment_request.

    POS Awesome calls this when a Phone-type Mode of Payment is used.
    We check if it is one of our mobile methods. If yes, process via our
    API. If not (e.g. a third-party Phone MOP), fall through to POSAwesome.

    The actual phone dialog is shown on the frontend (JS intercepts the
    button click before this is called). By the time we get here the
    phone number is in doc.contact_mobile.
    """
    import json
    if isinstance(doc, str):
        doc = frappe._dict(json.loads(doc))

    mobile_methods = {"ZAAD", "SAHAL", "EVCPLUS", "EVCPlus", "EDAHAB", "Edahab", "WaafiPay", "WAAFIPAY"}

    # Find the Phone payment in the doc
    for pay in (doc.get("payments") or []):
        mop = (pay.get("mode_of_payment") or "").strip()
        if mop.upper() not in {m.upper() for m in mobile_methods}:
            continue

        amount = flt(pay.get("amount") or 0)
        if amount <= 0:
            frappe.throw(_("Payment amount must be greater than zero"))

        phone = (doc.get("contact_mobile") or "").strip()
        if not phone:
            frappe.throw(_("Customer phone number is required for mobile payment. "
                           "Please enter the phone number in the POS contact field."))

        currency = doc.get("currency") or frappe.defaults.get_global_default("currency") or "USD"

        # Determine provider/method
        provider = "Edahab" if mop.upper() in ("EDAHAB",) else "WaafiPay"
        method   = mop if mop.upper() != "WAAFIPAY" else "ZAAD"

        return initiate_pos_payment(
            provider=provider,
            method=method,
            phone=phone,
            amount=amount,
            currency=currency,
            pos_profile=doc.get("pos_profile") or "",
            customer=doc.get("customer") or "",
            invoice_name=doc.get("name") or "",
        )

    # Not one of ours — delegate to POSAwesome's original implementation
    try:
        from posawesome.posawesome.api.posapp import (
            get_existing_payment_request,
            get_new_payment_request,
        )
        import json as _json
        doc_dict = doc if isinstance(doc, dict) else doc
        for pay in (doc_dict.get("payments") or []):
            if pay.get("type") == "Phone":
                pay_req = get_existing_payment_request(doc_dict, pay)
                if not pay_req:
                    pay_req = get_new_payment_request(doc_dict, pay)
                    pay_req.submit()
                else:
                    pay_req.request_phone_payment()
                return pay_req
    except Exception as e:
        frappe.log_error(str(e), "create_payment_request_override fallback failed")
