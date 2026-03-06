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
  var MOBILE_MODES = [
    "mobilemoney", "zaad", "evcplus", "evc", "edahab",
    "sahal", "waafipay", "waafi", "mobilepayment",
  ];

  function is_mobile_mode(mode_of_payment) {
    if (!mode_of_payment) return false;
    var raw = mode_of_payment.toLowerCase().replace(/[\s_\-]/g, "");
    return MOBILE_MODES.some(function (m) { return raw.indexOf(m) !== -1; });
  }

  var _auto_popup_shown_for = "";
  // Track whether mobile payment was verified for this PE
  var _payment_verified = false;
  var _verified_for = "";  // PE name that was verified

  frappe.ui.form.on("Payment Entry", {
    refresh: function (frm) {
      add_pay_button(frm);
      // Reset verified state if this is a different PE
      if (_verified_for && _verified_for !== frm.doc.name) {
        _payment_verified = false;
        _verified_for = "";
      }
    },

    mode_of_payment: function (frm) {
      add_pay_button(frm);
      // Reset verification when MoP changes
      _payment_verified = false;
      _verified_for = "";
    },

    before_submit: function (frm) {
      if (!is_mobile_mode(frm.doc.mode_of_payment)) return;
      if (_payment_verified && _verified_for === frm.doc.name) return;

      frappe.validated = false;
      frappe.msgprint({
        title: __("Mobile Payment Required"),
        message: __("Please complete the mobile payment before submitting. Click the <b>Pay with Mobile</b> button to proceed."),
        indicator: "orange",
      });
      open_payment_popup(frm, true);
    },

    after_save: function (frm) {
      if (frm.doc.docstatus !== 0) return;
      if (!is_mobile_mode(frm.doc.mode_of_payment)) return;
      if (!frm.doc.paid_amount && !frm.doc.received_amount) return;

      var sig = frm.doc.name + "|" + frm.doc.modified;
      if (_auto_popup_shown_for === sig) return;
      _auto_popup_shown_for = sig;

      open_payment_popup(frm, true);
    },
  });

  function add_pay_button(frm) {
    if (!frm || frm.doc.docstatus !== 0) return;
    if (!is_mobile_mode(frm.doc.mode_of_payment)) return;
    if (frm.custom_buttons && frm.custom_buttons[__("Pay with Mobile")]) return;

    frm.add_custom_button(__("Pay with Mobile"), function () {
      open_payment_popup(frm, true);
    });
    frm.change_custom_button_type(__("Pay with Mobile"), null, "primary");
  }

  function open_payment_popup(frm, auto_submit) {
    frappe.call({
      method: "mobile_payments.utils.payment_handler.get_available_methods",
      callback: function (r) {
        if (!r.message || !r.message.enabled) {
          frappe.msgprint(__("Mobile payments are not enabled. Please configure in Mobile Payment Settings."));
          return;
        }
        var methods = r.message.methods;
        if (!methods.length) {
          frappe.msgprint(__("No payment methods are configured."));
          return;
        }
        show_payment_dialog(frm, methods, auto_submit);
      },
    });
  }

  function show_payment_dialog(frm, methods, auto_submit) {
    var amount = flt(frm.doc.paid_amount || frm.doc.received_amount || 0);
    var currency = frm.doc.paid_to_account_currency
      || frm.doc.paid_from_account_currency || "USD";
    var customer = frm.doc.party_name || frm.doc.party || "";

    // Build combined "Provider|Method" options (same as POS Awesome)
    var options = methods.map(function (m) {
      return { label: m.label + " (" + m.provider + ")", value: m.provider + "|" + m.method };
    });
    var default_val = options[0] ? options[0].value : "";

    var d = new frappe.ui.Dialog({
      title: __("Mobile Payment"),
      size: "small",
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "header",
          options: '<div style="text-align:center;padding:8px 0 14px;border-bottom:1px solid #eee;margin-bottom:8px;">'
            + '<i class="fa fa-mobile fa-2x" style="color:#00d4ff;"></i>'
            + '<div style="margin-top:6px;font-size:18px;font-weight:600;">'
            + parseFloat(amount).toFixed(2) + " " + currency
            + '</div>'
            + (customer ? '<div class="text-muted">' + customer + '</div>' : '')
            + '</div>',
        },
        {
          fieldname: "payment_method",
          fieldtype: "Select",
          label: __("Payment Method"),
          options: options.map(function (o) { return o.value; }).join("\n"),
          reqd: 1,
          default: default_val,
        },
        {
          fieldname: "account_type",
          fieldtype: "Select",
          label: __("Payer Account Type"),
          options: "Subscriber (Mobile Wallet)\nMerchant Account",
          default: "Subscriber (Mobile Wallet)",
          description: __("Subscriber = regular customer wallet. Merchant = business till number."),
          onchange: function () {
            var is_merchant = (d.get_value("account_type") || "").toLowerCase().indexOf("merchant") !== -1;
            var phone_field = d.get_field("phone_number");
            if (phone_field) {
              phone_field.df.label = is_merchant ? __("Merchant Till Number") : __("Customer Phone");
              phone_field.df.description = is_merchant
                ? __("WaafiPay merchant till number (e.g. 7853)")
                : __("e.g. 252612345678 — editable if another person is paying");
              phone_field.refresh();
              if (is_merchant) d.set_value("phone_number", "");
            }
          },
        },
        {
          fieldname: "phone_number",
          fieldtype: "Data",
          label: __("Customer Phone"),
          description: __("e.g. 252612345678 — editable if another person is paying"),
          reqd: 1,
          default: "",
        },
        {
          fieldname: "amount",
          fieldtype: "Data",
          label: __("Amount") + " (" + currency + ")",
          reqd: 1,
          default: parseFloat(amount).toFixed(2),
          read_only: 1,
        },
      ],
      primary_action_label: __("Process Payment"),
      primary_action: function (values) {
        var phone_clean = (values.phone_number || "").replace(/[\s\-()+]/g, "");
        if (!phone_clean || !/^\d{7,15}$/.test(phone_clean)) {
          frappe.show_alert({ message: __("Enter a valid phone number (7–15 digits)"), indicator: "red" });
          return;
        }
        d.hide();

        var parts = values.payment_method.split("|");
        var provider = parts[0];
        var method = parts[1];

        initiate_payment(frm, provider, method, phone_clean, amount, currency, auto_submit);
      },
    });

    // Smart phone auto-fill from customer
    var _cached_phones = null;

    function _fill_phone_for_provider() {
      var sel = d.get_value("payment_method") || "";
      var provider = sel.split("|")[0] || "";

      if (_cached_phones) {
        var best = "";
        if (provider.toLowerCase() === "edahab" && _cached_phones.edahab_phone) {
          best = _cached_phones.edahab_phone;
        } else if (_cached_phones.waafi_phone) {
          best = _cached_phones.waafi_phone;
        } else if (_cached_phones.phone) {
          best = _cached_phones.phone;
        }
        if (best) d.set_value("phone_number", best);
        return;
      }

      if (!customer) return;
      frappe.call({
        method: "mobile_payments.api.pos.get_customer_phone_for_provider",
        args: { customer: frm.doc.party || "", provider: provider },
        callback: function (r) {
          if (r.message) {
            _cached_phones = r.message;
            var best = r.message.phone || "";
            if (best) {
              var current = d.get_value("phone_number");
              if (!current) d.set_value("phone_number", best);
            }
          }
        },
      });
    }

    // Re-route phone when payment method changes
    d.fields_dict.payment_method.$input.on("change", function () {
      if (_cached_phones) {
        var sel = d.get_value("payment_method") || "";
        var prov = sel.split("|")[0] || "";
        var best = "";
        if (prov.toLowerCase() === "edahab" && _cached_phones.edahab_phone) {
          best = _cached_phones.edahab_phone;
        } else if (_cached_phones.waafi_phone) {
          best = _cached_phones.waafi_phone;
        } else if (_cached_phones.phone) {
          best = _cached_phones.phone;
        }
        if (best) d.set_value("phone_number", best);
      }
    });

    _fill_phone_for_provider();
    d.show();
  }

  function initiate_payment(frm, provider, method, phone, amount, currency, auto_submit) {
    var api_method = provider === "Edahab"
      ? "mobile_payments.api.edahab.initiate_edahab_payment"
      : "mobile_payments.api.waafipay.initiate_waafipay_payment";

    var args = {
      phone: phone,
      amount: amount,
      invoice_id: "",
      description: "Payment Entry " + frm.doc.name,
      currency: currency,
    };
    if (provider === "WaafiPay") {
      args.method = method;
    }

    // Processing spinner dialog (same as POS Awesome)
    var spin = new frappe.ui.Dialog({
      title: __("Processing..."),
      fields: [{
        fieldtype: "HTML", fieldname: "html",
        options: '<div style="text-align:center;padding:30px;">'
          + '<i class="fa fa-spinner fa-pulse fa-3x" style="color:#00d4ff;"></i>'
          + '<p style="margin-top:14px;font-weight:500;">' + provider + " — " + method + '</p>'
          + '<p class="text-muted">' + __("Sending request to") + " " + phone + '...</p>'
          + '</div>',
      }],
      static: true,
    });
    spin.show();
    spin.$wrapper.find(".modal-footer").hide();

    frappe.call({
      method: api_method,
      args: args,
      callback: function (r) {
        if (!r.message) { spin.hide(); return; }
        var result = r.message;

        if (result.success) {
          spin.hide();
          _payment_verified = true;
          _verified_for = frm.doc.name;

          // Save payment details to the Payment Entry custom fields
          try {
            frappe.call({
              method: "frappe.client.set_value",
              args: {
                doctype: "Payment Entry",
                name: frm.doc.name,
                fieldname: {
                  mobile_payment_status: "Completed",
                  mobile_payment_provider: provider,
                  mobile_payment_method: method,
                  mobile_payment_phone: phone,
                  mobile_payment_reference: result.transaction_id || result.provider_reference || "",
                  mobile_payment_transaction_id: result.transaction_log || "",
                },
              },
              async: false,
              error: function () { /* fields may not exist yet — ignore */ },
            });
          } catch (e) {
            console.warn("Could not save mobile payment fields:", e);
          }

          frappe.show_alert({
            message: __("Payment successful!"),
            indicator: "green",
          }, 5);

          if (auto_submit) {
            submit_payment_entry(frm);
          } else {
            frm.reload_doc();
          }
        } else {
          spin.hide();
          frappe.msgprint({
            title: __("Payment Failed"),
            message: result.message || __("Payment was not completed. Please try again."),
            indicator: "red",
          });
        }
      },
      error: function () {
        spin.hide();
        frappe.msgprint({
          title: __("Error"),
          message: __("Could not connect to payment provider. Please try again."),
          indicator: "red",
        });
      },
    });
  }

  function submit_payment_entry(frm) {
    frappe.show_alert({
      message: __("Payment confirmed — submitting Payment Entry…"),
      indicator: "blue",
    }, 4);

    // Fetch fresh doc from server to avoid timestamp mismatch,
    // then submit it directly via API (no confirmation dialog).
    setTimeout(function () {
      frappe.call({
        method: "frappe.client.get",
        args: { doctype: "Payment Entry", name: frm.doc.name },
        callback: function (r) {
          var fresh_doc = r && r.message;
          if (!fresh_doc) {
            frappe.show_alert({
              message: __("Auto-submit failed — please click Submit manually."),
              indicator: "orange",
            }, 8);
            frm.reload_doc();
            return;
          }

          // Already submitted?
          if (fresh_doc.docstatus === 1) {
            frappe.show_alert({
              message: __("Payment Entry already submitted ✓"),
              indicator: "green",
            }, 5);
            frm.reload_doc();
            return;
          }

          frappe.call({
            method: "frappe.client.submit",
            args: { doc: fresh_doc },
            callback: function (r2) {
              if (r2 && r2.message) {
                frappe.show_alert({
                  message: __("Payment Entry submitted ✓"),
                  indicator: "green",
                }, 5);
              }
              frm.reload_doc();
            },
            error: function () {
              frappe.show_alert({
                message: __("Auto-submit failed — please click Submit manually."),
                indicator: "orange",
              }, 8);
              frm.reload_doc();
            },
          });
        },
        error: function () {
          frappe.show_alert({
            message: __("Auto-submit failed — please click Submit manually."),
            indicator: "orange",
          }, 8);
          frm.reload_doc();
        },
      });
    }, 1000);
  }

})();
