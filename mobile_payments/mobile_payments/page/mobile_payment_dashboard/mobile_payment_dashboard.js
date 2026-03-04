frappe.pages["mobile-payment-dashboard"].on_page_load = function (wrapper) {
  var page = frappe.ui.make_app_page({
    parent: wrapper,
    title: __("Mobile Payment Dashboard"),
    single_column: true,
  });

  page.main.html(`
    <div class="mobile-payment-dashboard">
      <div id="mpay-filters" style="margin-bottom:20px;"></div>
      <div id="mpay-stats" class="row"></div>
      <div class="row" style="margin-top:20px;">
        <div class="col-md-8">
          <div id="mpay-chart"></div>
        </div>
        <div class="col-md-4">
          <div id="mpay-provider-breakdown"></div>
        </div>
      </div>
      <div id="mpay-recent-transactions" style="margin-top:20px;"></div>
    </div>
  `);

  // Date filters
  let from_date = frappe.datetime.add_days(frappe.datetime.get_today(), -30);
  let to_date = frappe.datetime.get_today();

  page.add_field({
    fieldname: "from_date",
    label: __("From Date"),
    fieldtype: "Date",
    default: from_date,
    change: function () {
      from_date = page.fields_dict.from_date.get_value();
      load_dashboard(from_date, to_date);
    },
  });

  page.add_field({
    fieldname: "to_date",
    label: __("To Date"),
    fieldtype: "Date",
    default: to_date,
    change: function () {
      to_date = page.fields_dict.to_date.get_value();
      load_dashboard(from_date, to_date);
    },
  });

  // Action buttons
  page.set_secondary_action(__("Refresh"), function () {
    load_dashboard(from_date, to_date);
  });

  page.add_menu_item(__("Export Transactions"), function () {
    frappe.call({
      method: "mobile_payments.utils.reconciliation.export_transactions",
      args: { from_date: from_date, to_date: to_date },
      callback: function (r) {
        if (r.message) {
          frappe.tools.downloadify(
            r.message.map((t) => Object.values(t)),
            null,
            "Mobile_Payment_Transactions"
          );
        }
      },
    });
  });

  page.add_menu_item(__("Settlement Report"), function () {
    frappe.set_route("query-report", "Mobile Payment Settlement");
  });

  page.add_menu_item(__("Run Reconciliation"), function () {
    frappe.confirm(
      __("Run reconciliation for yesterday's transactions?"),
      function () {
        frappe.call({
          method: "mobile_payments.utils.reconciliation.run_daily_reconciliation",
          callback: function () {
            frappe.show_alert({
              message: __("Reconciliation completed"),
              indicator: "green",
            });
            load_dashboard(from_date, to_date);
          },
        });
      }
    );
  });

  // Initial load
  load_dashboard(from_date, to_date);

  function load_dashboard(from_date, to_date) {
    frappe.call({
      method: "mobile_payments.utils.reconciliation.get_dashboard_data",
      args: { from_date: from_date, to_date: to_date },
      callback: function (r) {
        if (r.message) {
          render_stats(r.message.summary);
          render_chart(r.message.daily_volume);
          render_provider_breakdown(r.message.provider_breakdown);
          render_recent_transactions();
        }
      },
    });
  }

  function render_stats(summary) {
    // Build currency breakdown string for successful amounts
    let currency_lines = "";
    if (summary.currency_totals && summary.currency_totals.length) {
      currency_lines = summary.currency_totals
        .map((ct) => `<div>${format_currency(ct.total_amount, ct.currency)}</div>`)
        .join("");
    } else {
      currency_lines = `<div>${format_currency(summary.total_amount)}</div>`;
    }

    let stats_html = `
      <div class="col-sm-6 col-md-3">
        <div class="stat-card success" style="cursor:pointer;" data-filter="Completed">
          <div class="stat-label">${__("Successful")}</div>
          <div class="stat-value">${summary.successful}</div>
          <small class="text-muted">${currency_lines}</small>
        </div>
      </div>
      <div class="col-sm-6 col-md-3">
        <div class="stat-card danger" style="cursor:pointer;" data-filter="Failed">
          <div class="stat-label">${__("Failed")}</div>
          <div class="stat-value">${summary.failed}</div>
        </div>
      </div>
      <div class="col-sm-6 col-md-3">
        <div class="stat-card warning" style="cursor:pointer;" data-filter="Pending">
          <div class="stat-label">${__("Pending / Retry")}</div>
          <div class="stat-value">${summary.pending + summary.retry_queue}</div>
        </div>
      </div>
      <div class="col-sm-6 col-md-3">
        <div class="stat-card info" style="cursor:pointer;" data-filter="all">
          <div class="stat-label">${__("Success Rate")}</div>
          <div class="stat-value">${summary.success_rate}%</div>
          <small class="text-muted">${summary.unreconciled} ${__("unreconciled")}</small>
        </div>
      </div>
    `;
    page.main.find("#mpay-stats").html(stats_html);

    // Make cards clickable — navigate to filtered transaction log
    page.main.find(".stat-card").on("click", function () {
      let filter = $(this).data("filter");
      if (filter === "all") {
        frappe.set_route("List", "Mobile Payment Transaction Log");
      } else {
        frappe.set_route("List", "Mobile Payment Transaction Log", { status: filter });
      }
    });
  }

  function render_chart(daily_volume) {
    if (!daily_volume || !daily_volume.length) {
      page.main.find("#mpay-chart").html(
        `<p class="text-muted text-center">${__("No transaction data for this period")}</p>`
      );
      return;
    }

    let labels = daily_volume.map((d) => d.date);
    let success_data = daily_volume.map((d) => d.success_count || 0);
    let failed_data = daily_volume.map((d) => d.failed_count || 0);

    // Use Frappe Chart
    page.main.find("#mpay-chart").html('<div id="mpay-chart-canvas"></div>');
    new frappe.Chart("#mpay-chart-canvas", {
      title: __("Daily Transaction Volume"),
      data: {
        labels: labels,
        datasets: [
          { name: __("Successful"), values: success_data, chartType: "bar" },
          { name: __("Failed"), values: failed_data, chartType: "bar" },
        ],
      },
      type: "axis-mixed",
      height: 250,
      colors: ["#2ecc71", "#e74c3c"],
      barOptions: { stacked: 1, spaceRatio: 0.4 },
    });
  }

  function render_provider_breakdown(providers) {
    if (!providers || !providers.length) {
      page.main.find("#mpay-provider-breakdown").html("");
      return;
    }

    let rows = providers
      .map(
        (p) => `
      <tr>
        <td>
          <span class="mpay-provider-icon">
            <span class="icon-dot ${p.provider.toLowerCase()}"></span>
            ${p.provider}
          </span>
        </td>
        <td>${p.payment_method || "-"}</td>
        <td>${p.currency || "USD"}</td>
        <td style="text-align:right;">${p.count}</td>
        <td style="text-align:right;">${format_currency(p.total_amount, p.currency || "USD")}</td>
      </tr>
    `
      )
      .join("");

    page.main.find("#mpay-provider-breakdown").html(`
      <h6>${__("By Provider")}</h6>
      <table class="mobile-payment-table">
        <thead>
          <tr>
            <th>${__("Provider")}</th>
            <th>${__("Method")}</th>
            <th>${__("Currency")}</th>
            <th style="text-align:right;">${__("Count")}</th>
            <th style="text-align:right;">${__("Amount")}</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `);
  }

  function render_recent_transactions() {
    frappe.call({
      method: "frappe.client.get_list",
      args: {
        doctype: "Mobile Payment Transaction Log",
        fields: [
          "name",
          "transaction_id",
          "provider",
          "payment_method",
          "status",
          "amount",
          "currency",
          "phone_number",
          "sales_invoice",
          "initiated_at",
        ],
        order_by: "initiated_at desc",
        limit_page_length: 20,
      },
      callback: function (r) {
        if (r.message && r.message.length) {
          let rows = r.message
            .map(
              (t) => `
            <tr>
              <td><a href="/app/mobile-payment-transaction-log/${t.name}">${t.transaction_id || t.name}</a></td>
              <td>
                <span class="mpay-provider-icon">
                  <span class="icon-dot ${t.provider.toLowerCase()}"></span>
                  ${t.provider}
                </span>
              </td>
              <td>${t.payment_method || "-"}</td>
              <td><span class="mpay-status ${t.status.toLowerCase()}">${t.status}</span></td>
              <td style="text-align:right;">${format_currency(t.amount, t.currency || "USD")}</td>
              <td>${t.phone_number || "-"}</td>
              <td>${t.sales_invoice ? '<a href="/app/sales-invoice/' + t.sales_invoice + '">' + t.sales_invoice + "</a>" : "-"}</td>
              <td>${frappe.datetime.prettyDate(t.initiated_at)}</td>
            </tr>
          `
            )
            .join("");

          page.main.find("#mpay-recent-transactions").html(`
            <h6>${__("Recent Transactions")}</h6>
            <table class="mobile-payment-table">
              <thead>
                <tr>
                  <th>${__("Transaction")}</th>
                  <th>${__("Provider")}</th>
                  <th>${__("Method")}</th>
                  <th>${__("Status")}</th>
                  <th style="text-align:right;">${__("Amount")}</th>
                  <th>${__("Phone")}</th>
                  <th>${__("Invoice")}</th>
                  <th>${__("Time")}</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
          `);
        }
      },
    });
  }
};
