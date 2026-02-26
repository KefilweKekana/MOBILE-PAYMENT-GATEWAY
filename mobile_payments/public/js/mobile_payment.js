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
    let phone_hint = prefill_phone
      ? __("Auto-fetched from customer record ({0})", [opts.phone_source || "Contact"])
      : __("Customer's mobile wallet number (e.g., 252612345678)");

    let d = new frappe.ui.Dialog({
      title: __("Pay with Mobile Money"),
      size: "small",
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "payment_header",
          options: `
            <div class="mobile-payment-header" style="text-align:center; margin-bottom:15px;">
              <i class="fa fa-mobile fa-3x" style="color:#2ecc71;"></i>
              <h4>${__("Select Payment Method")}</h4>
              <p class="text-muted">${__("Amount")}: <strong>${format_currency(opts.amount, opts.currency)}</strong></p>
              <p class="text-muted">${__("Currency")}: <strong>${opts.currency}</strong>
                <small class="text-success"> (${__("auto-detected from invoice")})</small>
              </p>
            </div>
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
          description: __(
            "USSD Push sends a prompt to the phone. HPP opens a payment page."
          ),
        },

        {
          fieldname: "phone_section",
          fieldtype: "Section Break",
          label: __("Customer Details"),
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
        },
        {
          fieldname: "phone_number",
          fieldtype: "Data",
          label: __("Phone Number"),
          description: phone_hint,
          default: prefill_phone,
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
          reqd: 1,
        },
      ],
      primary_action_label: __("Proceed to Pay"),
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

          // Auto-send notifications to customer via all configured channels
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
              if (notify_r.message) {
                let nr = notify_r.message;
                let status_el = document.getElementById("plink-notify-status");
                if (status_el) {
                  let badges = "";
                  if (nr.sent_channels && nr.sent_channels.length > 0) {
                    badges += nr.sent_channels.map(function(ch) {
                      return '<span class="badge badge-success" style="margin:2px;">' + ch.toUpperCase() + ' ✓</span>';
                    }).join(" ");
                  }
                  if (nr.failed_channels && nr.failed_channels.length > 0) {
                    badges += nr.failed_channels.map(function(ch) {
                      return '<span class="badge badge-danger" style="margin:2px;">' + ch.toUpperCase() + ' ✗</span>';
                    }).join(" ");
                  }
                  status_el.innerHTML = '<small>' + (badges || __("Notifications sent")) + '</small>';
                }
              }
            },
            async: true,
          });

          // Show HPP dialog with persistent payment link
          let d = new frappe.ui.Dialog({
            title: __("Payment Link Created"),
            fields: [
              {
                fieldtype: "HTML",
                fieldname: "plink_info",
                options: `
                  <div style="text-align:center; padding:20px;">
                    <i class="fa fa-link fa-3x" style="color:#2ecc71; margin-bottom:15px;"></i>
                    <h4>${__("Persistent Payment Link Ready")}</h4>
                    <p>${__("This link stays valid for 24 hours and auto-refreshes the payment session each time the customer visits it.")}</p>
                    <div style="margin:15px 0; padding:12px; background:#f5f5f5; border-radius:5px; word-break:break-all; border:1px solid #ddd;">
                      <a href="${payment_link}" target="_blank" style="color:#3498db;">${payment_link}</a>
                    </div>
                    <div style="display:flex; justify-content:center; gap:15px; margin:10px 0;">
                      <span class="text-muted"><i class="fa fa-clock-o"></i> ${__("Expires")}: ${expires_at}</span>
                      <span class="text-muted"><i class="fa fa-shield"></i> ${__("Auto-refreshing")}</span>
                    </div>
                    <div id="plink-notify-status" style="margin-top:10px;">
                      <small class="text-muted">
                        <i class="fa fa-spinner fa-spin"></i> ${__("Sending to customer via all notification channels...")}
                      </small>
                    </div>
                  </div>
                `,
              },
            ],
            primary_action_label: __("Open Payment Page"),
            primary_action: function () {
              window.open(payment_link, "_blank");
              d.hide();

              // Start polling for completion
              if (transaction_log) {
                let poll_dialog = mobile_payments._show_processing_dialog(
                  provider,
                  method,
                  mobile_payments._get_invoice_amount(frm),
                  frm.doc.currency,
                  true
                );
                mobile_payments._start_status_polling(
                  transaction_log,
                  poll_dialog,
                  frm
                );
              }
            },
            secondary_action_label: __("Copy Link"),
            secondary_action: function () {
              frappe.utils.copy_to_clipboard(payment_link);
              frappe.show_alert({ message: __("Payment link copied!"), indicator: "green" });
            },
          });

          // Add "Re-send" button
          d.add_custom_action(
            __("Re-send to Customer"),
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
                freeze_message: __("Resending notifications..."),
                callback: function (resend_r) {
                  if (resend_r.message && resend_r.message.success) {
                    frappe.msgprint({
                      title: __("Notifications Sent"),
                      message: resend_r.message.message,
                      indicator: "green",
                    });
                  } else {
                    frappe.msgprint({
                      title: __("Notification Failed"),
                      message: (resend_r.message && resend_r.message.message) || __("Failed to send notifications"),
                      indicator: "red",
                    });
                  }
                },
              });
            },
            "btn-default btn-sm"
          );

          // Add "Extend 24h" button
          d.add_custom_action(
            __("Extend 24h"),
            function () {
              frappe.call({
                method: "mobile_payments.api.payment_link.extend_payment_link",
                args: {
                  token: r.message.token,
                  additional_hours: 24,
                },
                callback: function (ext_r) {
                  if (ext_r.message && ext_r.message.success) {
                    frappe.show_alert({
                      message: __("Link extended! New expiry: {0}", [ext_r.message.new_expiry]),
                      indicator: "green",
                    });
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
                  <div style="text-align:center; padding:20px;">
                    <i class="fa fa-link fa-3x" style="color:#2ecc71; margin-bottom:15px;"></i>
                    <h4>${__("Payment Page Ready")}</h4>
                    <p>${__("Share this link with the patient to complete payment.")}</p>
                    <div style="margin:15px 0; padding:12px; background:#f5f5f5; border-radius:5px; word-break:break-all; border:1px solid #ddd;">
                      <a href="${payment_link}" target="_blank" style="color:#3498db;">${payment_link}</a>
                    </div>
                  </div>
                `,
              },
            ],
            primary_action_label: __("Open Payment Page"),
            primary_action: function () {
              window.open(payment_link, "_blank");
              d.hide();

              if (transaction_log) {
                let poll_dialog = mobile_payments._show_processing_dialog(
                  provider, method, amount, currency || "USD", true
                );
                mobile_payments._start_status_polling(
                  transaction_log, poll_dialog, frm
                );
              }
            },
            secondary_action_label: __("Copy Link"),
            secondary_action: function () {
              frappe.utils.copy_to_clipboard(payment_link);
              frappe.show_alert({ message: __("Payment link copied!"), indicator: "green" });
            },
          });

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
