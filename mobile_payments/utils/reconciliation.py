"""
Reconciliation & Reporting
Daily reconciliation, settlement matching, and exportable reports.
"""
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import (
    now_datetime,
    add_days,
    getdate,
    get_datetime,
    flt,
    today,
)


# ──────────────────────────────────────────────
# Daily Reconciliation
# ──────────────────────────────────────────────

def run_daily_reconciliation():
    """
    Scheduled task: Run daily reconciliation at 1 AM.
    Compares transaction logs with Payment Entries to find discrepancies.
    """
    settings = frappe.get_single("Mobile Payment Settings")
    if not settings.enabled:
        return

    yesterday = add_days(today(), -1)
    reconcile_date(yesterday)


def reconcile_date(date_str):
    """
    Reconcile all transactions for a specific date.

    Args:
        date_str: Date string (YYYY-MM-DD)

    Returns:
        dict with reconciliation summary
    """
    date = getdate(date_str)
    start = get_datetime(f"{date} 00:00:00")
    end = get_datetime(f"{date} 23:59:59")

    # Get all completed transactions for the date
    transactions = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters={
            "status": "Completed",
            "completed_at": ["between", [start, end]],
        },
        fields=[
            "name", "transaction_id", "provider", "payment_method",
            "amount", "currency", "phone_number", "sales_invoice",
            "payment_entry", "provider_transaction_id", "is_reconciled",
        ],
    )

    summary = {
        "date": str(date),
        "total_transactions": len(transactions),
        "total_amount": 0,
        "reconciled": 0,
        "discrepancies": [],
        "missing_payment_entries": [],
        "amount_mismatches": [],
    }

    for txn in transactions:
        summary["total_amount"] += flt(txn["amount"])

        if txn["is_reconciled"]:
            summary["reconciled"] += 1
            continue

        # Check if Payment Entry exists and matches
        if not txn["payment_entry"]:
            summary["missing_payment_entries"].append({
                "transaction": txn["name"],
                "transaction_id": txn["transaction_id"],
                "amount": txn["amount"],
                "invoice": txn["sales_invoice"],
            })
            continue

        # Verify Payment Entry amount matches
        pe_amount = frappe.db.get_value(
            "Payment Entry", txn["payment_entry"], "paid_amount"
        )

        if pe_amount is None:
            summary["missing_payment_entries"].append({
                "transaction": txn["name"],
                "transaction_id": txn["transaction_id"],
                "amount": txn["amount"],
                "invoice": txn["sales_invoice"],
                "note": "Payment Entry not found in database",
            })
            continue

        if flt(pe_amount) != flt(txn["amount"]):
            summary["amount_mismatches"].append({
                "transaction": txn["name"],
                "transaction_id": txn["transaction_id"],
                "expected_amount": txn["amount"],
                "actual_amount": pe_amount,
                "payment_entry": txn["payment_entry"],
            })
            continue

        # Mark as reconciled
        frappe.db.set_value(
            "Mobile Payment Transaction Log",
            txn["name"],
            {
                "is_reconciled": 1,
                "reconciled_at": now_datetime(),
            },
            update_modified=False,
        )
        summary["reconciled"] += 1

    # Log discrepancies
    summary["discrepancies"] = (
        summary["missing_payment_entries"] + summary["amount_mismatches"]
    )

    if summary["discrepancies"]:
        frappe.log_error(
            message=(
                f"Mobile Payment Reconciliation ({date}):\n"
                f"Total: {summary['total_transactions']}\n"
                f"Reconciled: {summary['reconciled']}\n"
                f"Missing PEs: {len(summary['missing_payment_entries'])}\n"
                f"Amount Mismatches: {len(summary['amount_mismatches'])}"
            ),
            title="Reconciliation Report",
        )

    frappe.db.commit()

    frappe.logger("mobile_payments").info(
        f"Reconciliation for {date}: {summary['reconciled']}/{summary['total_transactions']} "
        f"reconciled, {len(summary['discrepancies'])} discrepancies"
    )

    return summary


# ──────────────────────────────────────────────
# Report Data APIs
# ──────────────────────────────────────────────

@frappe.whitelist()
def get_dashboard_data(from_date=None, to_date=None):
    """
    Get dashboard summary data for mobile payments.

    Args:
        from_date: Start date (default: 30 days ago)
        to_date: End date (default: today)

    Returns:
        dict with dashboard metrics
    """
    frappe.has_permission("Mobile Payment Transaction Log", "read", throw=True)

    if not from_date:
        from_date = add_days(today(), -30)
    if not to_date:
        to_date = today()

    # Total transaction counts by status and currency
    status_counts = frappe.db.sql(
        """
        SELECT status, IFNULL(currency, 'USD') as currency,
               COUNT(*) as count, COALESCE(SUM(amount), 0) as total_amount
        FROM `tabMobile Payment Transaction Log`
        WHERE DATE(initiated_at) BETWEEN %s AND %s
        GROUP BY status, IFNULL(currency, 'USD')
        """,
        (from_date, to_date),
        as_dict=True,
    )

    # Provider breakdown (with currency)
    provider_stats = frappe.db.sql(
        """
        SELECT provider, payment_method, IFNULL(currency, 'USD') as currency,
               COUNT(*) as count,
               COALESCE(SUM(amount), 0) as total_amount
        FROM `tabMobile Payment Transaction Log`
        WHERE status = 'Completed'
          AND DATE(completed_at) BETWEEN %s AND %s
        GROUP BY provider, payment_method, IFNULL(currency, 'USD')
        """,
        (from_date, to_date),
        as_dict=True,
    )

    # Daily transaction volume
    daily_volume = frappe.db.sql(
        """
        SELECT DATE(initiated_at) as date, COUNT(*) as count,
               COALESCE(SUM(CASE WHEN status = 'Completed' THEN amount ELSE 0 END), 0) as success_amount,
               SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) as success_count,
               SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) as failed_count
        FROM `tabMobile Payment Transaction Log`
        WHERE DATE(initiated_at) BETWEEN %s AND %s
        GROUP BY DATE(initiated_at)
        ORDER BY DATE(initiated_at)
        """,
        (from_date, to_date),
        as_dict=True,
    )

    # Retry queue
    retry_queue = frappe.db.count(
        "Mobile Payment Transaction Log",
        {"status": "Retrying"},
    )

    # Pending count
    pending_count = frappe.db.count(
        "Mobile Payment Transaction Log",
        {"status": ["in", ("Pending", "Processing", "Initiated")]},
    )

    # Unreconciled count
    unreconciled = frappe.db.count(
        "Mobile Payment Transaction Log",
        {"status": "Completed", "is_reconciled": 0},
    )

    # Calculate totals
    total_success = sum(s["count"] for s in status_counts if s["status"] == "Completed")
    total_failed = sum(s["count"] for s in status_counts if s["status"] == "Failed")
    total_amount = sum(s["total_amount"] for s in status_counts if s["status"] == "Completed")
    total_all = sum(s["count"] for s in status_counts)
    success_rate = (total_success / total_all * 100) if total_all > 0 else 0

    # Per-currency amounts for completed transactions
    amount_by_currency = {}
    for s in status_counts:
        if s["status"] == "Completed":
            curr = s.get("currency") or "USD"
            amount_by_currency[curr] = amount_by_currency.get(curr, 0) + flt(s["total_amount"])

    return {
        "summary": {
            "total_transactions": total_all,
            "successful": total_success,
            "failed": total_failed,
            "pending": pending_count,
            "retry_queue": retry_queue,
            "unreconciled": unreconciled,
            "total_amount": total_amount,
            "amount_by_currency": amount_by_currency,
            "success_rate": round(success_rate, 1),
        },
        "status_breakdown": status_counts,
        "provider_breakdown": provider_stats,
        "daily_volume": daily_volume,
        "period": {"from_date": str(from_date), "to_date": str(to_date)},
    }


@frappe.whitelist()
def get_settlement_report(from_date=None, to_date=None, provider=None):
    """
    Get settlement reconciliation report.

    Returns:
        dict with settlement data
    """
    frappe.has_permission("Mobile Payment Transaction Log", "read", throw=True)

    if not from_date:
        from_date = add_days(today(), -7)
    if not to_date:
        to_date = today()

    filters = {
        "status": "Completed",
        "completed_at": ["between", [from_date, to_date]],
    }
    if provider:
        filters["provider"] = provider

    transactions = frappe.get_all(
        "Mobile Payment Transaction Log",
        filters=filters,
        fields=[
            "name", "transaction_id", "provider", "payment_method",
            "amount", "currency", "phone_number", "sales_invoice",
            "payment_entry", "provider_transaction_id", "completed_at",
            "is_reconciled", "settlement_reference",
        ],
        order_by="completed_at desc",
    )

    total = sum(flt(t["amount"]) for t in transactions)
    reconciled = sum(1 for t in transactions if t["is_reconciled"])

    return {
        "transactions": transactions,
        "summary": {
            "total_count": len(transactions),
            "total_amount": total,
            "reconciled_count": reconciled,
            "unreconciled_count": len(transactions) - reconciled,
        },
        "period": {"from_date": str(from_date), "to_date": str(to_date)},
    }


@frappe.whitelist()
def export_transactions(from_date=None, to_date=None, status=None, provider=None):
    """
    Export transactions for CSV/Excel download.

    Returns:
        list of transaction dicts
    """
    frappe.has_permission("Mobile Payment Transaction Log", "export", throw=True)

    if not from_date:
        from_date = add_days(today(), -30)
    if not to_date:
        to_date = today()

    filters = {
        "initiated_at": ["between", [from_date, to_date]],
    }
    if status:
        filters["status"] = status
    if provider:
        filters["provider"] = provider

    return frappe.get_all(
        "Mobile Payment Transaction Log",
        filters=filters,
        fields=[
            "name", "transaction_id", "provider", "payment_method",
            "flow_type", "status", "amount", "currency", "phone_number",
            "customer_name", "sales_invoice", "payment_entry",
            "provider_transaction_id", "provider_reference",
            "error_message", "initiated_at", "completed_at",
            "retry_count", "is_reconciled", "settlement_reference",
        ],
        order_by="initiated_at desc",
        limit_page_length=0,
    )


@frappe.whitelist()
def manual_reconcile(transaction_log, notes=""):
    """
    Manually mark a transaction as reconciled.

    Args:
        transaction_log: Transaction Log name
        notes: Reconciliation notes

    Returns:
        dict with result
    """
    frappe.has_permission("Mobile Payment Transaction Log", "write", throw=True)

    frappe.db.set_value(
        "Mobile Payment Transaction Log",
        transaction_log,
        {
            "is_reconciled": 1,
            "reconciled_at": now_datetime(),
            "reconciliation_notes": notes,
        },
    )

    return {"success": True, "message": "Transaction marked as reconciled"}
