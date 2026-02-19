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
                          pos_profile=None, customer=None, invoice_name=None):
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

    # Validate phone number format
    phone = (phone or "").strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if not phone or len(phone) < 9 or not phone.isdigit():
        return {
            "success": False,
            "message": _("Invalid phone number. Must be at least 9 digits (e.g., 252612345678)"),
        }

    # Validate credentials before proceeding
    try:
        if provider == "Edahab":
            creds = settings.get_edahab_credentials()
            if not creds.get("api_key"):
                return {"success": False, "message": _("Edahab API Key is not configured")}
        else:
            creds = settings.get_waafipay_credentials()
            if not creds.get("merchant_uid"):
                return {"success": False, "message": _("WaafiPay Merchant UID is not configured")}
            if not creds.get("api_key"):
                return {"success": False, "message": _("WaafiPay API Key is not configured")}
    except Exception as e:
        return {"success": False, "message": str(e)}

    # Build payment description with invoice reference
    desc_parts = ["POS Payment"]
    if invoice_name:
        desc_parts.append(f"Invoice: {invoice_name}")
    if customer:
        desc_parts.append(customer)
    else:
        desc_parts.append("Walk-in")
    payment_description = " - ".join(desc_parts)

    # Create Transaction Log
    log = frappe.get_doc({
        "doctype": "Mobile Payment Transaction Log",
        "provider": provider,
        "payment_method": method,
        "phone_number": phone,
        "amount": amount,
        "currency": currency or frappe.defaults.get_global_default("currency") or "USD",
        "status": "Initiated",
        "sales_invoice": invoice_name or "",
        "request_timestamp": now_datetime(),
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
            )

        if result.get("success"):
            log.status = "Completed"
            log.provider_transaction_id = result.get("transaction_id", "")
            log.provider_response = str(result.get("raw_response", ""))
            log.response_timestamp = now_datetime()
            log.save(ignore_permissions=True)
            frappe.db.commit()

            return {
                "success": True,
                "transaction_log": log.name,
                "transaction_id": log.transaction_id,
                "provider_transaction_id": result.get("transaction_id", ""),
                "message": _("Payment successful"),
            }

        elif result.get("pending"):
            log.status = "Pending"
            log.provider_response = str(result.get("raw_response", ""))
            log.save(ignore_permissions=True)
            frappe.db.commit()

            return {
                "success": False,
                "pending": True,
                "transaction_log": log.name,
                "transaction_id": log.transaction_id,
                "message": _("Payment pending confirmation"),
            }

        else:
            log.status = "Failed"
            log.error_message = result.get("message", "Payment failed")
            log.provider_response = str(result.get("raw_response", ""))
            log.response_timestamp = now_datetime()
            log.save(ignore_permissions=True)
            frappe.db.commit()

            return {
                "success": False,
                "transaction_log": log.name,
                "message": result.get("message", "Payment failed"),
            }

    except Exception as e:
        log.status = "Failed"
        log.error_message = str(e)
        log.response_timestamp = now_datetime()
        log.save(ignore_permissions=True)
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

    log.sales_invoice = invoice_name
    log.save(ignore_permissions=True)

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
                log.status = "Completed"
                log.provider_transaction_id = status_result.get("transaction_id", "")
                log.response_timestamp = now_datetime()
                log.save(ignore_permissions=True)
                frappe.db.commit()

                result["status"] = "Completed"
                result["provider_transaction_id"] = log.provider_transaction_id

                # Process payment if invoice is linked
                if log.sales_invoice and not log.payment_entry:
                    frappe.enqueue(
                        "mobile_payments.utils.payment_handler.process_successful_payment",
                        transaction_log=log.name,
                        invoice_id=log.sales_invoice,
                        queue="short",
                    )

            elif status_result.get("failed"):
                log.status = "Failed"
                log.error_message = status_result.get("message", "")
                log.response_timestamp = now_datetime()
                log.save(ignore_permissions=True)
                frappe.db.commit()

                result["status"] = "Failed"
                result["error_message"] = log.error_message

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
