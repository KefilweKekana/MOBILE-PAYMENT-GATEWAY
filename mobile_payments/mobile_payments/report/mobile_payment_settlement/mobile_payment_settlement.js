frappe.query_reports["Mobile Payment Settlement"] = {
  filters: [
    {
      fieldname: "from_date",
      label: __("From Date"),
      fieldtype: "Date",
      default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
      reqd: 1,
    },
    {
      fieldname: "to_date",
      label: __("To Date"),
      fieldtype: "Date",
      default: frappe.datetime.get_today(),
      reqd: 1,
    },
    {
      fieldname: "provider",
      label: __("Provider"),
      fieldtype: "Select",
      options: "\nWaafiPay\nEdahab",
    },
    {
      fieldname: "status",
      label: __("Status"),
      fieldtype: "Select",
      options:
        "\nInitiated\nPending\nProcessing\nCompleted\nFailed\nCancelled\nTimeout\nRetrying",
    },
    {
      fieldname: "is_reconciled",
      label: __("Reconciled"),
      fieldtype: "Select",
      options: "\n0\n1",
    },
  ],
};
