/**
 * Mobile Payments - Frontend Integration
 * Adds "Pay with Mobile" button to Sales Invoice and POS.
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
// Mobile Payments Namespace
// ──────────────────────────────────────────────

var mobile_payments = {
  /**
   * Show the main payment selection dialog.
   * User selects: Provider → Method → Phone → Confirm
   */
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

        mobile_payments._show_provider_selection(frm, methods);
      },
    });
  },

  /**
   * Step 1: Provider & Method Selection
   */
  _show_provider_selection: function (frm, methods) {
    let method_options = methods.map((m) => ({
      label: `${m.label} (${m.provider})`,
      value: `${m.provider}|${m.method}`,
    }));

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
              <p class="text-muted">${__("Amount")}: <strong>${format_currency(frm.doc.outstanding_amount, frm.doc.currency)}</strong></p>
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
          description: __("Customer's mobile wallet number (e.g., 252612345678)"),
          depends_on: 'eval:doc.flow_type=="Purchase API (USSD Push)"',
        },
      ],
      primary_action_label: __("Proceed to Pay"),
      primary_action: function (values) {
        d.hide();

        let [provider, method] = values.payment_method.split("|");
        let is_hpp = values.flow_type.includes("HPP");

        if (is_hpp) {
          mobile_payments._initiate_hpp_payment(frm, provider, method);
        } else {
          if (!values.phone_number) {
            frappe.msgprint(__("Phone number is required for USSD Push payment"));
            return;
          }
          mobile_payments._initiate_purchase_payment(
            frm,
            provider,
            method,
            values.phone_number
          );
        }
      },
    });

    d.show();
  },

  /**
   * Initiate Purchase API (USSD Push) payment
   */
  _initiate_purchase_payment: function (frm, provider, method, phone) {
    let api_method =
      provider === "Edahab"
        ? "mobile_payments.api.edahab.initiate_edahab_payment"
        : "mobile_payments.api.waafipay.initiate_waafipay_payment";

    let args = {
      phone: phone,
      amount: frm.doc.outstanding_amount,
      invoice_id: frm.doc.name,
      description: `Payment for ${frm.doc.name}`,
    };

    if (provider === "WaafiPay") {
      args.method = method;
    }

    // Show processing dialog
    let processing_dialog = mobile_payments._show_processing_dialog(
      provider,
      method,
      frm.doc.outstanding_amount,
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
  _initiate_hpp_payment: function (frm, provider, method) {
    // Create a persistent payment link (auto-refreshes provider HPP sessions)
    frappe.call({
      method: "mobile_payments.api.payment_link.create_payment_link",
      args: {
        invoice_id: frm.doc.name,
        provider: provider,
        method: method,
        expiry_hours: 24,
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
              amount: frm.doc.outstanding_amount,
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
                  frm.doc.outstanding_amount,
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
                  amount: frm.doc.outstanding_amount,
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
              ${result.provider_transaction_id
                ? `<p class="text-muted">${__("Transaction ID")}: <strong>${result.provider_transaction_id}</strong></p>`
                : ""}
              ${result.payment_entry
                ? `<p><a href="/app/payment-entry/${result.payment_entry}">${__("View Payment Entry")}</a></p>`
                : ""}
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

// Export for POS integration
if (typeof window !== "undefined") {
  window.mobile_payments = mobile_payments;
}
