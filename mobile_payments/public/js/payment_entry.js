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
      // Use the standard invoice-based payment dialog (beautiful green UI)
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
      // No linked SI — use the same beautiful green dialog via _show_provider_selection
      show_standalone_via_provider_selection(frm, auto_submit);
    }
  }

  // ── Standalone: use the beautiful _show_provider_selection dialog ──
  function show_standalone_via_provider_selection(frm, auto_submit) {
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

        var amount = flt(frm.doc.paid_amount || frm.doc.received_amount || 0);
        var currency = frm.doc.paid_to_account_currency
          || frm.doc.paid_from_account_currency || "USD";
        var party = frm.doc.party;
        var default_provider = (methods[0] || {}).provider || "";

        // Auto-fetch phone from party
        frappe.call({
          method: "mobile_payments.api.pos.get_customer_phone_for_provider",
          args: { customer: party || "", provider: default_provider },
          callback: function (phone_r) {
            var phone_data = phone_r.message || {};

            // Build a fake "invoice frm" so _show_provider_selection and
            // _initiate_purchase_payment work. On success we intercept
            // reload_doc to auto-submit the real PE.
            var fake_frm = {
              doc: {
                name: frm.doc.name,
                customer: party || "",
                currency: currency,
                grand_total: amount,
                base_grand_total: amount,
                outstanding_amount: amount,
                rounded_total: 0,
                conversion_rate: 1,
                docstatus: 1,
              },
              reload_doc: function () {
                if (auto_submit) {
                  submit_payment_entry(frm);
                } else {
                  frm.reload_doc();
                }
              },
            };

            window.mobile_payments._show_provider_selection(fake_frm, methods, {
              amount: amount,
              currency: currency,
              doctype: "Sales Invoice",   // routes to _initiate_purchase_payment
              docname: frm.doc.name,
              prefill_phone: phone_data.phone || "",
              phone_source: phone_data.source || "",
              phone_data: phone_data,
            });
          },
        });
      },
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
