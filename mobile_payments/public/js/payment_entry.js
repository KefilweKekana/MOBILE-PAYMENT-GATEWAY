// Payment Entry — Mobile Payment Integration
// Loaded via doctype_js hook so it is guaranteed to run
// on every Payment Entry form.
//
// Workflow:
//   1. When a mobile MOP (ZAAD, SAHAL, EVCPlus, EDAHAB, etc.) is selected
//      and the record is SAVED → payment popup appears automatically.
//   2. If payment succeeds → auto-submit the Payment Entry.
//   3. If payment fails → keep the Payment Entry as draft.
//   4. A manual "Pay with Mobile" button is also shown for convenience.

(function () {
  // ── Mobile-money mode name matching ─────────────────────────
  var MOBILE_MODES = [
    "mobilemoney", "zaad", "evcplus", "evc", "edahab",
    "sahal", "waafipay", "waafi", "mobilepayment",
  ];

  function is_mobile_mode(mode_of_payment) {
    if (!mode_of_payment) return false;
    var raw = mode_of_payment.toLowerCase().replace(/[\s_\-]/g, "");
    return MOBILE_MODES.some(function (m) { return raw.indexOf(m) !== -1; });
  }

  // Track whether we already triggered the auto-popup for this save
  // to avoid duplicate popups on frequent refresh events.
  var _auto_popup_shown_for = "";

  // ── Frappe form events ──────────────────────────────────────
  frappe.ui.form.on("Payment Entry", {
    refresh: function (frm) {
      add_pay_button(frm);
    },

    mode_of_payment: function (frm) {
      add_pay_button(frm);
    },

    after_save: function (frm) {
      // Auto-trigger the payment popup when the PE is saved
      // with a mobile Mode of Payment (draft only).
      if (frm.doc.docstatus !== 0) return;
      if (!is_mobile_mode(frm.doc.mode_of_payment)) return;
      if (!frm.doc.paid_amount && !frm.doc.received_amount) return;

      // Don't re-show if we already showed for this exact doc name + modified
      var sig = frm.doc.name + "|" + frm.doc.modified;
      if (_auto_popup_shown_for === sig) return;
      _auto_popup_shown_for = sig;

      open_payment_popup(frm, true);  // true = auto_submit on success
    },
  });

  // ── "Pay with Mobile" manual button ─────────────────────────
  function add_pay_button(frm) {
    if (!frm || frm.doc.docstatus !== 0) return;
    if (!is_mobile_mode(frm.doc.mode_of_payment)) return;

    // Avoid duplicating the button
    if (frm.custom_buttons && frm.custom_buttons[__("Pay with Mobile")]) return;

    frm.add_custom_button(__("Pay with Mobile"), function () {
      open_payment_popup(frm, true);
    });
    frm.change_custom_button_type(__("Pay with Mobile"), null, "primary");
  }

  // ── Open payment popup ──────────────────────────────────────
  function open_payment_popup(frm, auto_submit) {
    if (!window.mobile_payments) {
      frappe.msgprint(
        __("Mobile Payments module is not loaded. Please refresh the page.")
      );
      return;
    }

    // Find the first linked Sales Invoice, if any
    var si_ref = (frm.doc.references || []).find(function (r) {
      return r.reference_doctype === "Sales Invoice" && r.reference_name;
    });

    if (si_ref) {
      // Use the standard invoice-based payment dialog
      frappe.model.with_doc("Sales Invoice", si_ref.reference_name, function () {
        var si_frm = {
          doc: frappe.get_doc("Sales Invoice", si_ref.reference_name),
        };
        // Override reload_doc so success reloads (and optionally submits) the PE
        si_frm.reload_doc = function () {
          if (auto_submit) {
            submit_payment_entry(frm);
          } else {
            frm.reload_doc();
          }
        };
        window.mobile_payments.show_payment_dialog(si_frm);
      });
    } else {
      // No linked SI — standalone dialog
      show_standalone_dialog(frm, auto_submit);
    }
  }

  // ── Standalone payment dialog (no linked SI) ────────────────
  function show_standalone_dialog(frm, auto_submit) {
    frappe.call({
      method: "mobile_payments.utils.payment_handler.get_available_methods",
      callback: function (r) {
        if (!r.message || !r.message.enabled) {
          frappe.msgprint(
            __("Mobile payments are not enabled. Please configure in Mobile Payment Settings.")
          );
          return;
        }

        var methods = r.message.methods;
        if (!methods.length) {
          frappe.msgprint(__("No payment methods are configured."));
          return;
        }

        var method_options = methods.map(function (m) {
          return {
            label: m.label + " (" + m.provider + ")",
            value: m.provider + "|" + m.method,
          };
        });

        var amount = flt(frm.doc.paid_amount || frm.doc.received_amount || 0);
        var currency = frm.doc.paid_to_account_currency
          || frm.doc.paid_from_account_currency || "USD";
        var party = frm.doc.party;
        var default_provider = (methods[0] || {}).provider || "";

        // Try to auto-fetch phone from party
        var fetch_phone;
        if (party) {
          fetch_phone = new Promise(function (resolve) {
            frappe.call({
              method: "mobile_payments.api.pos.get_customer_phone_for_provider",
              args: { customer: party, provider: default_provider },
              async: false,
              callback: function (pr) { resolve((pr.message || {}).phone || ""); },
              error: function () { resolve(""); },
            });
          });
        } else {
          fetch_phone = Promise.resolve("");
        }

        fetch_phone.then(function (prefill_phone) {
          var d = new frappe.ui.Dialog({
            title: __("Pay with Mobile"),
            fields: [
              {
                fieldtype: "HTML",
                fieldname: "pe_info",
                options: '<div style="text-align:center; margin-bottom:16px; padding:16px; background:#f0fdf4; border-radius:10px; border:1px solid #bbf7d0;">'
                  + '<p style="margin:0 0 4px; font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:1px;">' + __("Amount") + '</p>'
                  + '<p style="margin:0; font-size:24px; font-weight:700; color:#059669;">' + format_currency(amount, currency) + '</p>'
                  + '<p style="margin:6px 0 0; font-size:12px; color:#6b7280;">' + frm.doc.name + (party ? ' &mdash; ' + party : '') + '</p>'
                  + '</div>',
              },
              {
                fieldname: "payment_method",
                fieldtype: "Select",
                label: __("Payment Method"),
                options: method_options.map(function (o) { return o.value; }).join("\n"),
                reqd: 1,
                default: method_options[0] ? method_options[0].value : "",
              },
              {
                fieldname: "phone_number",
                fieldtype: "Data",
                label: __("Phone Number"),
                reqd: 1,
                default: prefill_phone,
                description: __("Customer's mobile wallet number"),
              },
            ],
            size: "small",
            primary_action_label: __("Send Payment Request"),
            primary_action: function (values) {
              var parts = values.payment_method.split("|");
              var provider = parts[0];
              var method = parts[1];
              var phone = (values.phone_number || "").trim().replace(/[\s\-\+]/g, "");

              if (!phone || !/^\d{9,15}$/.test(phone)) {
                frappe.msgprint({
                  title: __("Invalid Phone Number"),
                  message: __("Phone number must be 9\u201315 digits."),
                  indicator: "red",
                });
                return;
              }

              d.hide();

              var api_method = provider === "Edahab"
                ? "mobile_payments.api.edahab.initiate_edahab_payment"
                : "mobile_payments.api.waafipay.initiate_waafipay_payment";

              var args = {
                phone: phone,
                amount: amount,
                invoice_id: "",
                description: "Payment via " + frm.doc.name,
                currency: currency,
              };
              if (provider === "WaafiPay") {
                args.method = method;
              }

              var processing_dialog = window.mobile_payments._show_processing_dialog(
                provider, method, amount, currency
              );

              frappe.call({
                method: api_method,
                args: args,
                callback: function (cr) {
                  if (cr.message) {
                    var result = cr.message;
                    if (result.success) {
                      processing_dialog.hide();
                      on_payment_success(frm, result, auto_submit);
                    } else if (result.pending) {
                      // Poll for pending — wrap success callback
                      poll_until_done(result.transaction_log, processing_dialog, frm, auto_submit);
                    } else {
                      processing_dialog.hide();
                      on_payment_failure(result.message || __("Payment failed"));
                    }
                  }
                },
                error: function () {
                  processing_dialog.hide();
                  on_payment_failure(__("An error occurred while processing the payment."));
                },
              });
            },
          });

          d.show();
        });
      },
    });
  }

  // ── Poll pending transaction until Completed / Failed ───────
  function poll_until_done(transaction_log, processing_dialog, frm, auto_submit) {
    // Reuse the existing polling logic but intercept success
    if (window.mobile_payments && window.mobile_payments._start_status_polling) {
      // Monkey-patch frm.reload_doc temporarily to handle success
      var orig_reload = frm.reload_doc;
      frm.reload_doc = function () {
        if (auto_submit) {
          submit_payment_entry(frm);
        } else {
          orig_reload.call(frm);
        }
      };
      window.mobile_payments._start_status_polling(
        transaction_log, processing_dialog, frm
      );
    }
  }

  // ── On payment success ──────────────────────────────────────
  function on_payment_success(frm, result, auto_submit) {
    frappe.show_alert({
      message: __("Payment Successful!"),
      indicator: "green",
    }, 5);

    if (auto_submit) {
      submit_payment_entry(frm);
    } else {
      frm.reload_doc();
    }
  }

  // ── On payment failure ──────────────────────────────────────
  function on_payment_failure(message) {
    frappe.msgprint({
      title: __("Payment Failed"),
      indicator: "red",
      message: '<div style="text-align:center; padding:10px;">'
        + '<i class="fa fa-times-circle fa-3x" style="color:#e74c3c; margin-bottom:10px;"></i>'
        + '<p>' + message + '</p>'
        + '<p class="text-muted">' + __("The Payment Entry remains as a draft. You can retry.") + '</p>'
        + '</div>',
    });
  }

  // ── Auto-submit the Payment Entry after successful payment ──
  function submit_payment_entry(frm) {
    frappe.show_alert({
      message: __("Payment confirmed — submitting Payment Entry\u2026"),
      indicator: "blue",
    }, 4);

    // Reload first to make sure we have the latest doc before submit
    frm.reload_doc(function () {
      // Small delay to let frm reconcile
      setTimeout(function () {
        frm.savesubmit();
      }, 1000);
    });
  }

})();
