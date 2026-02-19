"""
Mobile Payment Settlement Report
Provides settlement reconciliation details with filtering.
"""
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "fieldname": "transaction_id",
            "label": _("Transaction ID"),
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "fieldname": "name",
            "label": _("Log"),
            "fieldtype": "Link",
            "options": "Mobile Payment Transaction Log",
            "width": 140,
        },
        {
            "fieldname": "provider",
            "label": _("Provider"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "payment_method",
            "label": _("Method"),
            "fieldtype": "Data",
            "width": 90,
        },
        {
            "fieldname": "status",
            "label": _("Status"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "amount",
            "label": _("Amount"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "phone_number",
            "label": _("Phone"),
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "fieldname": "sales_invoice",
            "label": _("Sales Invoice"),
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "width": 140,
        },
        {
            "fieldname": "payment_entry",
            "label": _("Payment Entry"),
            "fieldtype": "Link",
            "options": "Payment Entry",
            "width": 140,
        },
        {
            "fieldname": "provider_transaction_id",
            "label": _("Provider TxnID"),
            "fieldtype": "Data",
            "width": 150,
        },
        {
            "fieldname": "is_reconciled",
            "label": _("Reconciled"),
            "fieldtype": "Check",
            "width": 90,
        },
        {
            "fieldname": "initiated_at",
            "label": _("Initiated"),
            "fieldtype": "Datetime",
            "width": 160,
        },
        {
            "fieldname": "completed_at",
            "label": _("Completed"),
            "fieldtype": "Datetime",
            "width": 160,
        },
    ]


def get_data(filters):
    conditions = []
    values = {}

    if filters:
        if filters.get("from_date"):
            conditions.append("DATE(t.initiated_at) >= %(from_date)s")
            values["from_date"] = filters["from_date"]

        if filters.get("to_date"):
            conditions.append("DATE(t.initiated_at) <= %(to_date)s")
            values["to_date"] = filters["to_date"]

        if filters.get("provider"):
            conditions.append("t.provider = %(provider)s")
            values["provider"] = filters["provider"]

        if filters.get("status"):
            conditions.append("t.status = %(status)s")
            values["status"] = filters["status"]

        if filters.get("is_reconciled") is not None:
            conditions.append("t.is_reconciled = %(is_reconciled)s")
            values["is_reconciled"] = filters["is_reconciled"]

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    return frappe.db.sql(
        f"""
        SELECT
            t.transaction_id,
            t.name,
            t.provider,
            t.payment_method,
            t.status,
            t.amount,
            t.phone_number,
            t.sales_invoice,
            t.payment_entry,
            t.provider_transaction_id,
            t.is_reconciled,
            t.initiated_at,
            t.completed_at
        FROM `tabMobile Payment Transaction Log` t
        WHERE {where_clause}
        ORDER BY t.initiated_at DESC
        """,
        values,
        as_dict=True,
    )
