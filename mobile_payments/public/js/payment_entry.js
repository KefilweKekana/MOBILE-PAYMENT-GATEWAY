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

    // Build provider options from available methods
    var provider_options = methods.map(function (m) { return m.provider; });
    // Build method options for the first provider
    var first_methods = methods.filter(function (m) {
      return m.provider === provider_options[0];
    }).map(function (m) { return m.method; });

    var d = new frappe.ui.Dialog({
      title: __("Pay with Mobile"),
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "payment_info",
          options: '<div style="margin-bottom:10px;">'
            + '<strong>' + __("Amount") + ':</strong> '
            + format_currency(amount, currency)
            + '</div>',
        },
        {
          fieldtype: "Select",
          fieldname: "provider",
          label: __("Provider"),
          options: provider_options.join("\n"),
          default: provider_options[0],
          reqd: 1,
          change: function () {
            var prov = d.get_value("provider");
            var prov_methods = methods.filter(function (m) {
              return m.provider === prov;
            }).map(function (m) { return m.method; });
            d.set_df_property("payment_method", "options", prov_methods.join("\n"));
            d.set_value("payment_method", prov_methods[0] || "");
          },
        },
        {
          fieldtype: "Select",
          fieldname: "payment_method",
          label: __("Payment Method"),
          options: first_methods.join("\n"),
          default: first_methods[0] || "",
          reqd: 1,
        },
        {
          fieldtype: "Data",
          fieldname: "phone",
          label: __("Phone Number"),
          reqd: 1,
          description: __("Customer mobile wallet number"),
        },
      ],
      primary_action_label: __("Pay"),
      primary_action: function (values) {
        d.hide();
        initiate_payment(frm, values, amount, currency, auto_submit);
      },
    });

    // Try to prefill phone from party
    if (frm.doc.party) {
      var default_prov = provider_options[0] || "";
      frappe.call({
        method: "mobile_payments.api.pos.get_customer_phone_for_provider",
        args: { customer: frm.doc.party, provider: default_prov },
        async: false,
        callback: function (r) {
          if (r.message && r.message.phone) {
            d.set_value("phone", r.message.phone);
          }
        },
      });
    }

    d.show();
  }

  function initiate_payment(frm, values, amount, currency, auto_submit) {
    var provider = values.provider;
    var method = values.payment_method;
    var phone = values.phone;

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

    frappe.show_alert({
      message: __("Processing {0} payment via {1}…", [method, provider]),
      indicator: "blue",
    }, 8);

    frappe.call({
      method: api_method,
      args: args,
      freeze: true,
      freeze_message: __("Waiting for payment confirmation…"),
      callback: function (r) {
        if (!r.message) return;
        var result = r.message;

        if (result.success) {
          _payment_verified = true;
          _verified_for = frm.doc.name;

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
          frappe.msgprint({
            title: __("Payment Failed"),
            message: result.message || __("Payment was not completed. Please try again."),
            indicator: "red",
          });
        }
      },
      error: function () {
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

    frm.reload_doc(function () {
      setTimeout(function () {
        frm.savesubmit();
      }, 1000);
    });
  }

})();
