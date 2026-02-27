/**
 * Mobile Payments - Frontend Integration
 * Adds "Pay with Mobile" button to Sales Invoice, Patient Appointment, and POS.
 * Handles payment flow UI for WaafiPay and Edahab.
 */

// ──────────────────────────────────────────────
// Sales Invoice Integration
// ──────────────────────────────────────────────

frappe.ui.form.on("Sales Invoice", {
  refresh: function (frm) {
    if (frm.doc.docstatus === 1 && flt(frm.doc.outstanding_amount) > 0) {
      // Add "Pay with Mobile" button
      frm.add_custom_button(
        __("Pay with Mobile"),
        function () {
          mobile_payments.show_payment_dialog(frm);
        },
        __("Payment")
      );

      // Add "Generate Payment Links" button if links don't exist yet
      if (!frm.doc.waafi_payment_link || !frm.doc.edahab_payment_link) {
        frm.add_custom_button(
          __("Generate Payment Links"),
          function () {
            frappe.call({
              method: "mobile_payments.utils.payment_links.generate_links_for_invoice",
              args: { invoice_name: frm.doc.name },
              freeze: true,
              freeze_message: __("Generating payment links..."),
              callback: function (r) {
                if (r.message) {
                  let msg_parts = [];
                  if (r.message.waafi_payment_link) msg_parts.push("WaafiPay");
                  if (r.message.edahab_payment_link) msg_parts.push("Edahab");
                  if (msg_parts.length) {
                    frappe.show_alert({
                      message: __("Payment links generated: {0}", [msg_parts.join(", ")]),
                      indicator: "green",
                    }, 5);
                  } else {
                    frappe.show_alert({
                      message: __("No payment links could be generated. Check Error Log."),
                      indicator: "orange",
                    }, 5);
                  }
                  frm.reload_doc();
                }
              },
            });
          },
          __("Payment")
        );
      }
    }

    // Show mobile payment status indicator
    if (frm.doc.mobile_payment_status) {
      let color = {
        Completed: "green",
        Pending: "orange",
        Processing: "blue",
        Failed: "red",
        Cancelled: "grey",
      }[frm.doc.mobile_payment_status] || "grey";

      frm.dashboard.set_headline(
        `<span class="indicator whitespace-nowrap ${color}">
          Mobile Payment: ${frm.doc.mobile_payment_status}
          ${frm.doc.mobile_payment_provider ? " via " + frm.doc.mobile_payment_provider : ""}
        </span>`
      );
    }
  },
});

// ──────────────────────────────────────────────
// Patient Appointment Integration (only if Healthcare is installed)
// ──────────────────────────────────────────────

try {
  frappe.ui.form.on("Patient Appointment", {
  refresh: function (frm) {
    // Show "Pay with Mobile" button for open / unlinked appointments
    // Works for both saved and submitted appointments with a payable amount
    let payable = flt(frm.doc.paid_amount || frm.doc.billing_amount || frm.doc.consultation_fee || 0);
    let already_paid = frm.doc.mobile_payment_status === "Completed";

    if (!frm.is_new() && payable > 0 && !already_paid) {
      frm.add_custom_button(
        __("Pay with Mobile"),
        function () {
          mobile_payments.show_appointment_payment_dialog(frm);
        },
        __("Payment")
      );
    }

    // Show mobile payment status indicator
    if (frm.doc.mobile_payment_status) {
      let color = {
        Completed: "green",
        Pending: "orange",
        Processing: "blue",
        Failed: "red",
        Cancelled: "grey",
      }[frm.doc.mobile_payment_status] || "grey";

      let headline = `<span class="indicator whitespace-nowrap ${color}">
          Mobile Payment: ${frm.doc.mobile_payment_status}
          ${frm.doc.mobile_payment_provider ? " via " + frm.doc.mobile_payment_provider : ""}
        </span>`;

      // Show linked Sales Invoice if available
      if (frm.doc.mobile_payment_sales_invoice) {
        headline += ` &mdash; <a href="/app/sales-invoice/${frm.doc.mobile_payment_sales_invoice}">
          ${__("Sales Invoice")}: ${frm.doc.mobile_payment_sales_invoice}
        </a>`;
      }

      frm.dashboard.set_headline(headline);
    }
  },
});
  } catch(e) {
    // Patient Appointment doctype not available (Healthcare not installed)
    console.log("Mobile Payments: Patient Appointment integration skipped (Healthcare not installed)");
  }

// ──────────────────────────────────────────────
// Mobile Payments Namespace
// ──────────────────────────────────────────────

var mobile_payments = {
  /**
   * Show the main payment selection dialog for Sales Invoice.
   * Auto-fetches available methods and customer phone.
   */
  /**
   * Get the payable amount in the invoice's OWN currency.
   *
   * ERPNext stores outstanding_amount in COMPANY currency (always USD if
   * company is USD-based). For foreign-currency invoices (e.g. SOS/SLSH)
   * that gives the wrong number.
   *
   * grand_total / rounded_total are stored in the TRANSACTION currency,
   * so they reflect what the customer actually owes in their currency.
   *
   * We still check outstanding_amount > 0 as a gating condition (it tells
   * us whether the invoice is unpaid), but we send grand_total as the amount.
   */
  _get_invoice_amount: function (frm) {
    // ERPNext field reference:
    //   grand_total       — always in the TRANSACTION currency (what customer owes)
    //   base_grand_total  — always in the COMPANY currency
    //   rounded_total     — in transaction currency, but 0 when rounding is disabled;
    //                       on some ERPNext versions it can hold the company-currency
    //                       rounded value, making it unreliable for foreign invoices
    //   outstanding_amount — in COMPANY currency — NEVER use as the charge amount

    var company_currency = frappe.defaults.get_default("currency") || "USD";
    var invoice_currency = frm.doc.currency || company_currency;

    if (invoice_currency !== company_currency) {
      // Foreign-currency invoice (e.g. SOS/SLSH on a USD company).
      // grand_total is guaranteed to be in the invoice currency.
      // Do NOT touch rounded_total — it may contain the company-currency value.
      return flt(frm.doc.grand_total || 0);
    }

    // Same-currency invoice — prefer rounded_total when it is explicitly set,
    // but only if it is close to grand_total (i.e. it's a cosmetic rounding,
    // not a completely different value).
    var grand   = flt(frm.doc.grand_total   || 0);
    var rounded = flt(frm.doc.rounded_total || 0);
    if (rounded > 0 && Math.abs(rounded - grand) <= grand * 0.01) {
      return rounded;
    }
    return grand;
  },

  show_payment_dialog: function (frm) {
    // First, fetch available methods from server
    frappe.call({
      method: "mobile_payments.utils.payment_handler.get_available_methods",
      callback: function (r) {
        if (!r.message || !r.message.enabled) {
          frappe.msgprint(
            __("Mobile payments are not enabled. Please configure in Mobile Payment Settings.")
          );
          return;
        }

        let methods = r.message.methods;
        if (!methods.length) {
          frappe.msgprint(__("No payment methods are configured."));
          return;
        }

        // Auto-fetch customer phone with smart provider routing
        // (Edahab prefixes: 65, 66, 62, 76 — everything else → WaafiPay)
        let default_provider = (methods[0] || {}).provider || "";
        frappe.call({
          method: "mobile_payments.api.pos.get_customer_phone_for_provider",
          args: { customer: frm.doc.customer, provider: default_provider },
          callback: function (phone_r) {
            let phone_data = phone_r.message || {};
            mobile_payments._show_provider_selection(frm, methods, {
              amount: mobile_payments._get_invoice_amount(frm),
              currency: frm.doc.currency || "USD",
              doctype: "Sales Invoice",
              docname: frm.doc.name,
              prefill_phone: phone_data.phone || "",
              phone_source: phone_data.source || "",
              phone_data: phone_data,  // full phone data for provider switching
            });
          },
        });
      },
    });
  },

  /**
   * Show the payment dialog for Patient Appointment.
   * Auto-fetches patient phone and appointment amount/currency.
   */
  show_appointment_payment_dialog: function (frm) {
    frappe.call({
      method: "mobile_payments.utils.payment_handler.get_available_methods",
      callback: function (r) {
        if (!r.message || !r.message.enabled) {
          frappe.msgprint(
            __("Mobile payments are not enabled. Please configure in Mobile Payment Settings.")
          );
          return;
        }

        let methods = r.message.methods;
        if (!methods.length) {
          frappe.msgprint(__("No payment methods are configured."));
          return;
        }

        // Auto-fetch amount + currency + patient phone from backend
        frappe.call({
          method: "mobile_payments.utils.payment_handler.get_appointment_payment_details",
          args: { appointment_name: frm.doc.name },
          callback: function (details_r) {
            let details = details_r.message || {};
            let amount = details.amount || flt(frm.doc.paid_amount || frm.doc.billing_amount || frm.doc.consultation_fee || 0);
            let currency = details.currency || frm.doc.currency || frappe.defaults.get_default("currency") || "USD";
            let prefill_phone = details.phone || "";
            let phone_source = details.source || "";

            mobile_payments._show_provider_selection(frm, methods, {
              amount: amount,
              currency: currency,
              doctype: "Patient Appointment",
              docname: frm.doc.name,
              prefill_phone: prefill_phone,
              phone_source: phone_source,
            });
          },
        });
      },
    });
  },

  /**
   * Step 1: Provider & Method Selection (shared between Sales Invoice & Patient Appointment)
   */
  _show_provider_selection: function (frm, methods, opts) {
    let method_options = methods.map((m) => ({
      label: `${m.label} (${m.provider})`,
      value: `${m.provider}|${m.method}`,
    }));

    let prefill_phone = opts.prefill_phone || "";
    let invoice_label = opts.docname || "";

    // Phone verified badge (only when auto-fetched)
    let phone_verified_html = prefill_phone
      ? '<div class="mp-verified"><i class="fa fa-check-circle"></i> ' + __("Verified with customer record") + '</div>'
      : '';

    // Scoped CSS for the payment dialog
    let dialog_css = `
      <style>
        .mp-dialog .modal-dialog { max-width: 480px; height: 50em; }
        .mp-dialog .modal-content { border-radius: 20px !important; overflow: hidden !important; border: none !important; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25); }
        .mp-dialog .modal-header { display: none !important; height: 0 !important; min-height: 0 !important; padding: 0 !important; margin: 0 !important; border: none !important; }
        .mp-dialog .modal-body { padding: 0 !important; margin: 0 !important; }
        .mp-dialog .modal-body > .form-layout,
        .mp-dialog .form-layout { padding: 0 !important; margin: 0 !important; }
        .mp-dialog .form-page, .mp-dialog .form-layout .form-page { padding: 0 !important; margin: 0 !important; }
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .ql-editor,
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .html-content { padding: 0 !important; margin: 0 !important; }

        /* Header: zero margin/padding on all wrappers */
        .mp-dialog .modal-body > .frappe-control[data-fieldname="payment_header"],
        .mp-dialog .frappe-control[data-fieldname="payment_header"],
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .form-group,
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .frappe-control-value,
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .control-input-wrapper,
        .mp-dialog .frappe-control[data-fieldname="payment_header"] .control-value {
          margin: 0 !important; padding: 0 !important;
        }

        /* All other controls: side padding */
        .mp-dialog .modal-body > .frappe-control:not([data-fieldname="payment_header"]) {
          padding-left: 28px !important; padding-right: 28px !important;
        }
        .mp-dialog .form-section .frappe-control {
          padding-left: 0 !important; padding-right: 0 !important;
        }
        .mp-dialog .form-section {
          border: none !important; padding: 0 28px !important; margin: 0 !important;
        }
        .mp-dialog .section-head { display: none !important; }
        .mp-dialog .section-body { padding: 0 !important; }
        .mp-dialog .form-column { padding: 0 !important; }

        /* Form controls — high specificity to override frappe */
        .mp-dialog .form-group { margin-bottom: 18px !important; }
        .mp-dialog .frappe-control .control-label,
        .mp-dialog .control-label {
          font-size: 13px !important; font-weight: 600 !important; color: #374151 !important; margin-bottom: 8px !important;
        }
        .mp-dialog .frappe-control .control-input .form-control,
        .mp-dialog .frappe-control .control-input select,
        .mp-dialog .frappe-control .control-input input[type="text"],
        .mp-dialog .frappe-control input.input-with-feedback,
        .mp-dialog .form-control,
        .mp-dialog select.form-control,
        .mp-dialog input.form-control {
          border: 2px solid #e5e7eb !important; border-radius: 10px !important;
          padding: 12px 16px !important; font-size: 15px !important; height: auto !important;
          background: #fafafa !important; transition: border-color 0.2s, box-shadow 0.2s !important;
          color: #1f2937 !important; -webkit-appearance: none !important;
          line-height: 1.4 !important; box-shadow: none !important;
        }
        .mp-dialog .frappe-control .control-input .form-control:focus,
        .mp-dialog .frappe-control .control-input select:focus,
        .mp-dialog .frappe-control .control-input input:focus,
        .mp-dialog .form-control:focus,
        .mp-dialog select.form-control:focus,
        .mp-dialog input.form-control:focus {
          border-color: #10b981 !important; background: #fff !important;
          box-shadow: 0 0 0 3px rgba(16,185,129,0.1) !important; outline: none !important;
        }
        .mp-dialog select.form-control,
        .mp-dialog .frappe-control select {
          padding-right: 40px !important; cursor: pointer !important;
        }
        .mp-dialog .select-icon,
        .mp-dialog .control-input .select-icon {
          top: 50% !important; transform: translateY(-50%) !important;
          right: 12px !important; margin-top: 0 !important;
        }
        .mp-dialog .like-disabled-input { background: #fafafa !important; }

        /* Hide footer — button is inside form body */
        .mp-dialog .modal-footer { display: none !important; }

        /* Help text */
        .mp-dialog .help-box {
          font-size: 12px !important; color: #6b7280 !important; margin-top: 6px !important;
          border: none !important; padding: 0 !important; background: none !important;
        }

        /* Green header */
        .mp-amount-header {
          background: linear-gradient(135deg, #10b981 0%, #059669 100%);
          color: #fff; text-align: center; padding: 40px 24px 34px; position: relative; overflow: hidden;
          margin: -1px -1px 0 -1px !important;
          border-radius: 20px 20px 0 0 !important;
        }
        .mp-amount-header::before {
          content: ''; position: absolute; top: -50%; left: -50%; width: 300%; height: 200%;
          background: radial-gradient(circle, rgba(255,255,255,0.1) 1px, transparent 1px);
          background-size: 20px 20px; opacity: 0.3;
        }
        .mp-amount-header .mp-inner { position: relative; z-index: 2; }
        .mp-amount-header .mp-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; opacity: 0.85; margin: 0 0 6px; }
        .mp-amount-header .mp-amount { font-size: 36px; font-weight: 700; margin: 0 0 8px; line-height: 1; }
        .mp-amount-header .mp-invoice { font-size: 12px; opacity: 0.8; margin: 0; }
        .mp-amount-header .mp-close {
          position: absolute; top: 14px; right: 14px; z-index: 10;
          background: rgba(255,255,255,0.2); border: none; color: #fff;
          width: 32px; height: 32px; border-radius: 8px; cursor: pointer;
          display: flex; align-items: center; justify-content: center; transition: background 0.2s;
          font-size: 14px;
        }
        .mp-amount-header .mp-close:hover { background: rgba(255,255,255,0.35); }

        .mp-form-spacer { height: 20px; }

        /* Verified badge */
        .mp-verified { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #6b7280; margin-top: 4px; margin-bottom: 8px; }
        .mp-verified i { color: #10b981 !important; font-size: 13px; }

        /* Secure transaction card */
        .mp-info-card {
          background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px;
          padding: 14px 16px; display: flex; gap: 12px; align-items: flex-start; margin: 4px 0 16px;
        }
        .mp-info-card .mp-icon {
          flex-shrink: 0; width: 36px; height: 36px; background: #dcfce7; border-radius: 50%;
          display: flex; align-items: center; justify-content: center;
        }
        .mp-info-card .mp-icon i { color: #059669 !important; font-size: 15px; }
        .mp-info-card .mp-text strong { font-size: 13px; color: #1f2937; display: block; margin-bottom: 3px; }
        .mp-info-card .mp-text span { font-size: 11px; color: #6b7280; line-height: 1.5; }

        /* Pay button (in-form) */
        .mp-pay-btn {
          background: linear-gradient(135deg, #10b981 0%, #059669 100%);
          border: none; font-weight: 600; font-size: 16px; color: #fff;
          padding: 14px 32px; border-radius: 12px; width: 100%; cursor: pointer;
          box-shadow: 0 4px 6px -1px rgba(16,185,129,0.2); transition: all 0.2s;
          display: flex; align-items: center; justify-content: center; gap: 8px;
          margin-top: 8px;
        }
        .mp-pay-btn:hover { transform: translateY(-1px); box-shadow: 0 10px 15px -3px rgba(16,185,129,0.3); }
        .mp-pay-btn:active { transform: translateY(0); }

        /* Security badge */
        .mp-security-badge {
          display: flex; align-items: center; justify-content: center; gap: 8px;
          color: #9ca3af; font-size: 11px; padding: 12px 0 8px;
        }
        .mp-security-badge .mp-dot { width: 3px; height: 3px; background: #d1d5db; border-radius: 50%; display: inline-block; }
      </style>
    `;

    let d = new frappe.ui.Dialog({
      title: "",
      size: "small",
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "payment_header",
          options: dialog_css + `
            <div class="mp-amount-header">
              <button class="mp-close" onclick="cur_dialog.hide()"><i class="fa fa-times"></i></button>
              <div class="mp-inner">
                <p class="mp-label">${__("Total Amount")}</p>
                <div class="mp-amount">${format_currency(opts.amount, opts.currency)}</div>
                <p class="mp-invoice"><i class="fa fa-file-text-o" style="margin-right:4px;"></i>${__("Invoice")} #${invoice_label}</p>
              </div>
            </div>
            <div class="mp-form-spacer"></div>
          `,
        },
        {
          fieldname: "payment_method",
          fieldtype: "Select",
          label: __("Payment Method"),
          options: method_options.map((o) => o.value).join("\n"),
          reqd: 1,
          default: method_options[0]?.value,
        },
        {
          fieldname: "flow_type",
          fieldtype: "Select",
          label: __("Payment Flow"),
          options: "Purchase API (USSD Push)\nHosted Payment Page (HPP)",
          default: "Purchase API (USSD Push)",
          description: '<i class="fa fa-info-circle" style="color:#10b981;"></i> '
            + __("You'll receive a prompt on your phone to confirm payment"),
        },
        {
          fieldname: "phone_section",
          fieldtype: "Section Break",
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
        },
        {
          fieldname: "phone_number",
          fieldtype: "Data",
          label: __("Phone Number"),
          default: prefill_phone,
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
          reqd: 1,
        },
        {
          fieldtype: "HTML",
          fieldname: "phone_verified",
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
          options: phone_verified_html,
        },
        {
          fieldname: "action_section",
          fieldtype: "Section Break",
        },
        {
          fieldtype: "HTML",
          fieldname: "pay_button",
          options: `
            <button type="button" class="mp-pay-btn" id="mp-proceed-btn">
              ${__("Proceed to Pay")} <span>→</span>
            </button>
          `,
        },
      ],
      primary_action_label: __("Proceed to Pay") + " →",
      primary_action: function (values) {
        let [provider, method] = values.payment_method.split("|");
        let is_hpp = values.flow_type.includes("HPP");
        // Currency is auto-detected from invoice — no manual selection
        let currency = opts.currency || "USD";

        if (!is_hpp) {
          // Validate phone before proceeding
          let phone = (values.phone_number || "").trim().replace(/[\s\-\+]/g, "");
          if (!phone) {
            frappe.msgprint({
              title: __("Phone Number Required"),
              message: __("Please enter the customer's mobile wallet number to proceed with USSD Push payment."),
              indicator: "red",
            });
            return;
          }
          if (!/^\d{9,15}$/.test(phone)) {
            frappe.msgprint({
              title: __("Invalid Phone Number"),
              message: __("Phone number must be 9–15 digits only (e.g. 252612345678). Remove any spaces, dashes or country code prefix '+'."),
              indicator: "red",
            });
            return;
          }

          d.hide();
          if (opts.doctype === "Patient Appointment") {
            mobile_payments._initiate_appointment_payment(frm, provider, method, phone, currency);
          } else {
            mobile_payments._initiate_purchase_payment(frm, provider, method, phone, currency);
          }
        } else {
          d.hide();
          if (opts.doctype === "Patient Appointment") {
            mobile_payments._initiate_appointment_hpp_payment(frm, provider, method, currency);
          } else {
            mobile_payments._initiate_hpp_payment(frm, provider, method, currency);
          }
        }
      },
    });

    d.show();

    // Add the mp-dialog class to activate scoped styles
    d.$wrapper.addClass('mp-dialog');

    // Wire the in-form pay button to trigger primary action
    d.$wrapper.find('#mp-proceed-btn').on('click', function() {
      let values = d.get_values();
      if (values) d.primary_action(values);
    });

    // Ensure select options show proper labels
    if (d.fields_dict.payment_method && d.fields_dict.payment_method.$input) {
      let $sel = d.fields_dict.payment_method.$input;
      $sel.find('option').each(function() {
        let val = $(this).val();
        if (val) {
          let label_match = method_options.find(function(o) { return o.value === val; });
          if (label_match) $(this).text(label_match.label);
        }
      });
      $sel.trigger('change');
    }
    // Friendly flow-type labels
    if (d.fields_dict.flow_type && d.fields_dict.flow_type.$input) {
      let $flow = d.fields_dict.flow_type.$input;
      $flow.find('option').each(function() {
        let v = $(this).val();
        if (v.indexOf('USSD') !== -1) $(this).text(__("USSD Push (Instant)"));
        else if (v.indexOf('HPP') !== -1) $(this).text(__("Payment Link (HPP)"));
      });
    }

    // ── Smart phone routing: swap phone when payment method changes ──
    let _phone_data = opts.phone_data || null;
    if (_phone_data && d.fields_dict.payment_method) {
      let _swap_phone = function () {
        let sel = d.get_value("payment_method") || "";
        let prov = sel.split("|")[0] || "";
        let best = "";
        if (prov.toLowerCase() === "edahab" && _phone_data.edahab_phone) {
          best = _phone_data.edahab_phone;
        } else if (_phone_data.waafi_phone) {
          best = _phone_data.waafi_phone;
        } else if (_phone_data.phone) {
          best = _phone_data.phone;
        }
        if (best) {
          d.set_value("phone_number", best);
          console.log("mobile_payments: smart phone routing →", prov, best);
        }
      };
      // Swap on provider change
      d.fields_dict.payment_method.$input.on("change", _swap_phone);
    }
  },

  /**
   * Initiate Purchase API (USSD Push) payment for Sales Invoice
   */
  _initiate_purchase_payment: function (frm, provider, method, phone, currency) {
    let api_method =
      provider === "Edahab"
        ? "mobile_payments.api.edahab.initiate_edahab_payment"
        : "mobile_payments.api.waafipay.initiate_waafipay_payment";

    let args = {
      phone: phone,
      amount: mobile_payments._get_invoice_amount(frm),
      invoice_id: frm.doc.name,
      description: `Payment for ${frm.doc.name}`,
      currency: currency || frm.doc.currency || "USD",
    };

    if (provider === "WaafiPay") {
      args.method = method;
    }

    // Show processing dialog
    let processing_dialog = mobile_payments._show_processing_dialog(
      provider,
      method,
      mobile_payments._get_invoice_amount(frm),
      frm.doc.currency
    );

    frappe.call({
      method: api_method,
      args: args,
      callback: function (r) {
        if (r.message) {
          let result = r.message;

          if (result.success) {
            processing_dialog.hide();
            mobile_payments._show_success_dialog(result, frm);
          } else if (result.pending) {
            // Payment pending - start polling
            mobile_payments._start_status_polling(
              result.transaction_log,
              processing_dialog,
              frm
            );
          } else {
            processing_dialog.hide();
            mobile_payments._show_error_dialog(
              result.message || "Payment failed"
            );
          }
        }
      },
      error: function () {
        processing_dialog.hide();
        mobile_payments._show_error_dialog(
          "An error occurred while processing the payment"
        );
      },
    });
  },

  /**
   * Initiate HPP (Hosted Payment Page) payment
   * Creates a persistent payment link that auto-refreshes the HPP session.
   * The link stays valid for 24+ hours even though provider tokens expire sooner.
   */
  _initiate_hpp_payment: function (frm, provider, method, currency) {
    // Create a persistent payment link (auto-refreshes provider HPP sessions)
    frappe.call({
      method: "mobile_payments.api.payment_link.create_payment_link",
      args: {
        invoice_id: frm.doc.name,
        provider: provider,
        method: method,
        expiry_hours: 24,
        currency: currency || frm.doc.currency || "USD",
      },
      freeze: true,
      freeze_message: __("Creating payment link..."),
      callback: function (r) {
        if (r.message && r.message.success) {
          let payment_link = r.message.payment_link;
          let transaction_log = r.message.transaction_log || "";
          let expires_at = r.message.expires_at;

          // Auto-send notification silently
          frappe.call({
            method: "mobile_payments.utils.notifications.send_payment_link_notification",
            args: {
              payment_link: payment_link,
              invoice_id: frm.doc.name,
              amount: mobile_payments._get_invoice_amount(frm),
              currency: frm.doc.currency,
              provider: provider,
              transaction_log: transaction_log,
            },
            callback: function (notify_r) {
              let el = document.getElementById("plink-notify-status");
              if (!el) return;
              if (notify_r.message && notify_r.message.success) {
                let channels = (notify_r.message.sent_channels || []).map(function(ch) {
                  return '<span style="display:inline-block;padding:2px 8px;background:#e8f8f0;color:#27ae60;border-radius:10px;font-size:11px;margin:0 2px;">' + ch.toUpperCase() + ' ✓</span>';
                }).join(" ");
                el.innerHTML = channels || '<span style="color:#27ae60;">✓ ' + __("Sent") + '</span>';
              } else {
                el.innerHTML = '<span style="color:#999;">' + __("Notification not available") + '</span>';
              }
            },
            async: true,
          });

          // Show HPP dialog with payment link
          let d = new frappe.ui.Dialog({
            title: __("Payment Link Created"),
            fields: [
              {
                fieldtype: "HTML",
                fieldname: "plink_info",
                options: `
                  <div style="text-align:center; padding:24px 16px;">
                    <div style="width:56px; height:56px; border-radius:50%; background:#e8f8f0; display:inline-flex; align-items:center; justify-content:center; margin-bottom:16px;">
                      <i class="fa fa-check" style="color:#2ecc71; font-size:28px;"></i>
                    </div>
                    <h4 style="margin:0 0 8px;">${__("Payment Link Ready")}</h4>
                    <p class="text-muted" style="margin:0 0 16px; font-size:13px;">${__("Share this link with the customer to complete payment.")}</p>
                    <div style="margin:0 0 16px; padding:12px 14px; background:#f8f9fa; border-radius:6px; word-break:break-all; border:1px solid #e9ecef; text-align:left;">
                      <a href="${payment_link}" target="_blank" style="color:#3498db; font-size:13px;">${payment_link}</a>
                    </div>
                    <div style="display:flex; justify-content:center; gap:12px; margin:0 0 12px;">
                      <p class="text-muted" style="margin:0; font-size:12px;"><i class="fa fa-clock-o"></i> ${__("Expires")}: ${expires_at}</p>
                    </div>
                    <div id="plink-notify-status" style="font-size:12px;">
                      <span class="text-muted"><i class="fa fa-spinner fa-spin"></i> ${__("Sending notification...")}</span>
                    </div>
                  </div>
                `,
              },
            ],
            primary_action_label: __("Copy Link"),
            primary_action: function () {
              frappe.utils.copy_to_clipboard(payment_link);
              frappe.show_alert({ message: __("Payment link copied!"), indicator: "green" });
            },
          });

          // Share button — uses native share API or copies link
          d.add_custom_action(
            '<i class="fa fa-share-alt"></i> ' + __("Share"),
            function () {
              if (navigator.share) {
                navigator.share({
                  title: __("Payment Link"),
                  text: __("Please complete your payment of {0}", [format_currency(mobile_payments._get_invoice_amount(frm), frm.doc.currency)]),
                  url: payment_link,
                }).catch(function() {});
              } else {
                frappe.utils.copy_to_clipboard(payment_link);
                frappe.show_alert({ message: __("Link copied — paste in WhatsApp, SMS, or Email"), indicator: "blue" });
              }
            },
            "btn-default btn-sm"
          );

          // Resend Notification button
          d.add_custom_action(
            '<i class="fa fa-refresh"></i> ' + __("Resend"),
            function () {
              frappe.call({
                method: "mobile_payments.utils.notifications.send_payment_link_notification",
                args: {
                  payment_link: payment_link,
                  invoice_id: frm.doc.name,
                  amount: mobile_payments._get_invoice_amount(frm),
                  currency: frm.doc.currency,
                  provider: provider,
                  transaction_log: transaction_log,
                },
                freeze: true,
                freeze_message: __("Resending..."),
                callback: function (resend_r) {
                  if (resend_r.message && resend_r.message.success) {
                    frappe.show_alert({ message: __("Notification resent!"), indicator: "green" });
                  } else {
                    frappe.show_alert({ message: __("Could not send notification"), indicator: "orange" });
                  }
                },
              });
            },
            "btn-default btn-sm"
          );

          d.show();
        } else {
          mobile_payments._show_error_dialog(
            (r.message && r.message.message) || "Failed to create payment link"
          );
        }
      },
    });
  },

  /**
   * Initiate USSD Push payment for Patient Appointment
   */
  _initiate_appointment_payment: function (frm, provider, method, phone, currency) {
    let amount = flt(frm.doc.paid_amount || frm.doc.billing_amount || frm.doc.consultation_fee || 0);

    let args = {
      phone: phone,
      amount: amount,
      appointment_name: frm.doc.name,
      description: `Appointment payment for ${frm.doc.patient_name || frm.doc.name}`,
      currency: currency || "USD",
      provider: provider,
      method: method,
    };

    // Show processing dialog
    let processing_dialog = mobile_payments._show_processing_dialog(
      provider,
      method,
      amount,
      currency || "USD"
    );

    frappe.call({
      method: "mobile_payments.utils.payment_handler.initiate_appointment_payment",
      args: args,
      callback: function (r) {
        if (r.message) {
          let result = r.message;

          if (result.success) {
            processing_dialog.hide();
            // Include sales_invoice in result for the success dialog
            mobile_payments._show_success_dialog(result, frm);
          } else if (result.pending) {
            mobile_payments._start_status_polling(
              result.transaction_log,
              processing_dialog,
              frm
            );
          } else {
            processing_dialog.hide();
            mobile_payments._show_error_dialog(
              result.message || "Payment failed"
            );
          }
        }
      },
      error: function () {
        processing_dialog.hide();
        mobile_payments._show_error_dialog(
          "An error occurred while processing the payment"
        );
      },
    });
  },

  /**
   * Initiate HPP payment for Patient Appointment
   */
  _initiate_appointment_hpp_payment: function (frm, provider, method, currency) {
    let amount = flt(frm.doc.paid_amount || frm.doc.billing_amount || frm.doc.consultation_fee || 0);

    frappe.call({
      method: "mobile_payments.utils.payment_handler.initiate_appointment_hpp",
      args: {
        appointment_name: frm.doc.name,
        provider: provider,
        method: method,
        amount: amount,
        currency: currency || "USD",
      },
      freeze: true,
      freeze_message: __("Creating payment link..."),
      callback: function (r) {
        if (r.message && r.message.success) {
          let payment_link = r.message.hpp_url;
          let transaction_log = r.message.transaction_log || "";

          let d = new frappe.ui.Dialog({
            title: __("Payment Link Created"),
            fields: [
              {
                fieldtype: "HTML",
                fieldname: "plink_info",
                options: `
                  <div style="text-align:center; padding:24px 16px;">
                    <div style="width:56px; height:56px; border-radius:50%; background:#e8f8f0; display:inline-flex; align-items:center; justify-content:center; margin-bottom:16px;">
                      <i class="fa fa-check" style="color:#2ecc71; font-size:28px;"></i>
                    </div>
                    <h4 style="margin:0 0 8px;">${__("Payment Link Ready")}</h4>
                    <p class="text-muted" style="margin:0 0 16px; font-size:13px;">${__("Share this link with the patient to complete payment.")}</p>
                    <div style="margin:0 0 16px; padding:12px 14px; background:#f8f9fa; border-radius:6px; word-break:break-all; border:1px solid #e9ecef; text-align:left;">
                      <a href="${payment_link}" target="_blank" style="color:#3498db; font-size:13px;">${payment_link}</a>
                    </div>
                  </div>
                `,
              },
            ],
            primary_action_label: __("Copy Link"),
            primary_action: function () {
              frappe.utils.copy_to_clipboard(payment_link);
              frappe.show_alert({ message: __("Payment link copied!"), indicator: "green" });
            },
          });

          // Share button
          d.add_custom_action(
            '<i class="fa fa-share-alt"></i> ' + __("Share"),
            function () {
              if (navigator.share) {
                navigator.share({
                  title: __("Payment Link"),
                  text: __("Please complete your appointment payment"),
                  url: payment_link,
                }).catch(function() {});
              } else {
                frappe.utils.copy_to_clipboard(payment_link);
                frappe.show_alert({ message: __("Link copied — paste in WhatsApp, SMS, or Email"), indicator: "blue" });
              }
            },
            "btn-default btn-sm"
          );

          d.show();
        } else {
          mobile_payments._show_error_dialog(
            (r.message && r.message.message) || "Failed to create payment link"
          );
        }
      },
    });
  },

  /**
   * Show processing/waiting dialog
   */
  _show_processing_dialog: function (provider, method, amount, currency, is_hpp) {
    let message = is_hpp
      ? __("Waiting for customer to complete payment on the hosted page...")
      : __("A payment prompt has been sent to the customer's phone. Waiting for confirmation...");

    let d = new frappe.ui.Dialog({
      title: __("Processing Payment"),
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "processing_content",
          options: `
            <div class="mobile-payment-processing" style="text-align:center; padding:20px;">
              <div class="mobile-payment-spinner" style="margin-bottom:20px;">
                <i class="fa fa-spinner fa-pulse fa-3x" style="color:#3498db;"></i>
              </div>
              <h4>${provider} - ${method}</h4>
              <p class="text-muted">${__('Amount')}: <strong>${format_currency(amount, currency)}</strong></p>
              <p>${message}</p>
              <div id="payment-status-text" class="text-muted" style="margin-top:10px;">
                ${__('Please wait...')}
              </div>
            </div>
          `,
        },
      ],
      static: true,
    });

    d.show();
    d.$wrapper.find(".modal-footer").hide();
    return d;
  },

  /**
   * Poll for payment status updates
   */
  _start_status_polling: function (transaction_log, dialog, frm) {
    let poll_count = 0;
    let max_polls = 60; // Max 2 minutes at 2-second intervals
    let poll_interval = 2000;

    let poller = setInterval(function () {
      poll_count++;

      if (poll_count > max_polls) {
        clearInterval(poller);
        dialog.hide();
        mobile_payments._show_timeout_dialog(transaction_log, frm);
        return;
      }

      frappe.call({
        method: "mobile_payments.utils.payment_handler.get_payment_status",
        args: { transaction_log: transaction_log },
        async: true,
        callback: function (r) {
          if (r.message) {
            let status = r.message.status;

            // Update status text in dialog
            let status_el = dialog.$wrapper.find("#payment-status-text");
            if (status_el.length) {
              status_el.html(`${__("Status")}: ${status} (${poll_count}s)`);
            }

            if (status === "Completed") {
              clearInterval(poller);
              dialog.hide();
              mobile_payments._show_success_dialog(r.message, frm);
            } else if (
              status === "Failed" ||
              status === "Cancelled" ||
              status === "Timeout"
            ) {
              clearInterval(poller);
              dialog.hide();
              mobile_payments._show_error_dialog(
                r.message.error_message || `Payment ${status.toLowerCase()}`
              );
            }
          }
        },
      });
    }, poll_interval);

    // Add cancel button to dialog
    dialog.$wrapper.find(".modal-footer").show();
    dialog.set_primary_action(__("Cancel Payment"), function () {
      clearInterval(poller);
      dialog.hide();

      frappe.call({
        method: "mobile_payments.utils.payment_handler.cancel_pending_payment",
        args: { transaction_log: transaction_log },
      });

      frm.reload_doc();
    });
  },

  /**
   * Show success dialog
   */
  _show_success_dialog: function (result, frm) {
    frappe.show_alert(
      {
        message: __("Payment Successful!"),
        indicator: "green",
      },
      7
    );

    // Build extra info lines (Sales Invoice, Payment Entry)
    let extra_info = "";
    if (result.sales_invoice) {
      extra_info += `<p><a href="/app/sales-invoice/${result.sales_invoice}">
        <i class="fa fa-file-text-o"></i> ${__("View Sales Invoice")}: ${result.sales_invoice}
      </a></p>`;
    }
    if (result.payment_entry) {
      extra_info += `<p><a href="/app/payment-entry/${result.payment_entry}">
        <i class="fa fa-money"></i> ${__("View Payment Entry")}: ${result.payment_entry}
      </a></p>`;
    }
    if (result.provider_transaction_id) {
      extra_info += `<p class="text-muted">${__("Transaction ID")}: <strong>${result.provider_transaction_id}</strong></p>`;
    }

    let d = new frappe.ui.Dialog({
      title: __("Payment Successful"),
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "success_content",
          options: `
            <div style="text-align:center; padding:20px;">
              <div style="margin-bottom:15px;">
                <i class="fa fa-check-circle fa-4x" style="color:#2ecc71;"></i>
              </div>
              <h3 style="color:#2ecc71;">${__("Payment Confirmed!")}</h3>
              <p>${__("The payment has been successfully processed.")}</p>
              ${extra_info}
            </div>
          `,
        },
      ],
      primary_action_label: __("Done"),
      primary_action: function () {
        d.hide();
        frm.reload_doc();
      },
    });
    d.show();
  },

  /**
   * Show error dialog
   */
  _show_error_dialog: function (message) {
    frappe.msgprint({
      title: __("Payment Failed"),
      indicator: "red",
      message: `
        <div style="text-align:center; padding:10px;">
          <i class="fa fa-times-circle fa-3x" style="color:#e74c3c; margin-bottom:10px;"></i>
          <p>${message}</p>
          <p class="text-muted">${__("Please try again or contact support.")}</p>
        </div>
      `,
    });
  },

  /**
   * Show timeout dialog
   */
  _show_timeout_dialog: function (transaction_log, frm) {
    let d = new frappe.ui.Dialog({
      title: __("Payment Timeout"),
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "timeout_content",
          options: `
            <div style="text-align:center; padding:20px;">
              <i class="fa fa-clock-o fa-3x" style="color:#f39c12; margin-bottom:15px;"></i>
              <h4>${__("Payment Timed Out")}</h4>
              <p>${__("The payment confirmation was not received in time. The payment may still be processing.")}</p>
              <p class="text-muted">${__("The system will continue to check the status automatically.")}</p>
            </div>
          `,
        },
      ],
      primary_action_label: __("OK"),
      primary_action: function () {
        d.hide();
        frm.reload_doc();
      },
    });
    d.show();
  },
};

// Export for POS integration — MERGE into existing global so we don't
// overwrite the .pos namespace added by pos_awesome_integration.js
if (typeof window !== "undefined") {
  if (window.mobile_payments) {
    // Preserve any existing properties (e.g. .pos) while adding ours
    Object.keys(mobile_payments).forEach(function (k) {
      window.mobile_payments[k] = mobile_payments[k];
    });
  } else {
    window.mobile_payments = mobile_payments;
  }
}
