/**
 * Mobile Payments — POS Awesome Integration
 *
 * Rules enforced:
 *   A. Single selection — only ONE payment mode can have amount > 0 at a time.
 *      Selecting any mode auto-clears all others.
 *   B. No mixing — mobile payment blocks all other modes while active.
 *      Other modes block mobile while they have a value.
 *   C. Verified-before-submit — if a mobile MOP has amount > 0 the SUBMIT
 *      and SUBMIT & PRINT buttons are blocked until the payment dialog
 *      completes successfully (_payment_verified = true).
 */

frappe.provide("mobile_payments.pos");

mobile_payments.pos = {

  // ── State ─────────────────────────────────────────────────────
  _methods:           null,
  _enabled:           false,
  _initialized:       false,
  _intercepting:      false,
  _watching:          false,
  _last_transaction:  null,
  _mobile_mop_names:  [],
  _payment_verified:  false,   // true only after successful payment dialog
  _submit_guard_on:   false,   // true while guard listener is attached

  // ── Helpers ───────────────────────────────────────────────────

  /** Strip thousands-separators / currency symbols before parsing. */
  _parse_amount: function (val) {
    if (val === null || val === undefined) return 0;
    return parseFloat(String(val).replace(/[^0-9.]/g, "")) || 0;
  },

  _is_mobile_mop: function (label) {
    if (!label) return false;
    var up = label.toUpperCase();
    return mobile_payments.pos._mobile_mop_names.some(function (n) {
      return up === n || up.indexOf(n) !== -1;
    });
  },

  _method_from_label: function (label) {
    var up = label.toUpperCase();
    return (mobile_payments.pos._methods || []).find(function (m) {
      return up.indexOf(m.label.toUpperCase()) !== -1;
    }) || null;
  },

  _payment_panel_open: function () {
    var btns = document.querySelectorAll("button, .v-btn");
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].textContent || "").trim().toUpperCase();
      if (t === "REC CASH" || t === "CASH") {
        var r = btns[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return true;
      }
    }
    return false;
  },

  /**
   * Return all visible payment rows as an array of {btn, inp, label}.
   * Only rows where the button has a label and the input is visible.
   */
  _get_payment_rows: function () {
    var rows = [];
    document.querySelectorAll(".v-row").forEach(function (row) {
      var btn = row.querySelector("button, .v-btn");
      var inp = row.querySelector("input");
      if (!btn || !inp) return;
      var rect = btn.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      var label = (btn.textContent || "").trim().toUpperCase();
      if (!label) return;
      rows.push({ btn: btn, inp: inp, label: label, row: row });
    });
    return rows;
  },

  _get_outstanding: function () {
    // Most reliable: read the REC CASH input (POS pre-fills it with grand total)
    var rows = mobile_payments.pos._get_payment_rows();
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r.label === "REC CASH" || r.label === "CASH") {
        var v = mobile_payments.pos._parse_amount(r.inp.value);
        if (v > 0) return v;
      }
    }
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc) {
        var fv = flt(cur_frm.doc.rounded_total || cur_frm.doc.grand_total || 0);
        if (fv > 0) return fv;
      }
    } catch (e) {}
    return 0;
  },

  /** Write a value to a payment input and fire Vue's change events. */
  _set_input: function (inp, value) {
    inp.value = value;
    inp.dispatchEvent(new Event("input",  { bubbles: true }));
    inp.dispatchEvent(new Event("change", { bubbles: true }));
  },

  // ── Init ──────────────────────────────────────────────────────
  init: function () {
    if (mobile_payments.pos._initialized) return;
    mobile_payments.pos._initialized = true;

    frappe.call({
      method: "mobile_payments.api.pos.get_mobile_payment_methods",
      async: false,
      callback: function (r) {
        if (r.message && r.message.enabled) {
          mobile_payments.pos._enabled  = true;
          mobile_payments.pos._methods  = r.message.methods || [];
          mobile_payments.pos._mobile_mop_names = mobile_payments.pos._methods
            .map(function (m) { return m.label.toUpperCase(); });
          ["ZAAD","SAHAL","EVCPLUS","EDAHAB","WAAFIPAY"].forEach(function (n) {
            if (mobile_payments.pos._mobile_mop_names.indexOf(n) === -1)
              mobile_payments.pos._mobile_mop_names.push(n);
          });
        }
      },
    });

    if (!mobile_payments.pos._enabled) return;

    mobile_payments.pos._attach_interceptor();
    mobile_payments.pos._attach_input_watcher();
    mobile_payments.pos._attach_submit_guard();
  },

  // ── A. Click interceptor for mobile MOP buttons ───────────────
  _attach_interceptor: function () {
    if (mobile_payments.pos._intercepting) return;
    mobile_payments.pos._intercepting = true;

    document.addEventListener("click", function (e) {
      var btn = e.target.closest("button, .v-btn");
      if (!btn) return;
      var label = (btn.textContent || "").trim().toUpperCase();
      if (!mobile_payments.pos._is_mobile_mop(label)) return;
      if (!mobile_payments.pos._payment_panel_open()) return;

      e.stopImmediatePropagation();
      e.preventDefault();

      var outstanding = mobile_payments.pos._get_outstanding();
      if (outstanding <= 0) {
        frappe.show_alert({ message: __("Cart is empty — add items before payment"), indicator: "orange" });
        return;
      }

      // Rule A+B: set this MOP to full amount, zero everything else
      mobile_payments.pos._apply_single_selection(btn, outstanding);

      // Reset verified flag — new payment attempt needed
      mobile_payments.pos._payment_verified = false;
      mobile_payments.pos._update_submit_buttons();

      var methodObj = mobile_payments.pos._method_from_label(label);
      mobile_payments.pos.show_dialog(methodObj, outstanding);

    }, true); // capture phase
  },

  // ── A+B. Input watcher — mutual exclusion on all payment rows ─
  /**
   * Watches all payment inputs. When any input gets a non-zero value:
   *   - if it is a mobile MOP → clear all other MOPs and all cash/card rows
   *   - if it is cash/card    → clear all mobile MOPs
   * This enforces single-selection regardless of how the value was set.
   */
  _attach_input_watcher: function () {
    if (mobile_payments.pos._watching) return;
    mobile_payments.pos._watching = true;

    // Use a debounce so we don't fight Vue's own reactive updates
    var _debounce_timer = null;

    document.addEventListener("input", function (e) {
      if (!mobile_payments.pos._payment_panel_open()) return;
      // Only care about inputs inside a payment row (has a sibling button)
      var row = e.target.closest(".v-row");
      if (!row) return;
      var btn = row.querySelector("button, .v-btn");
      if (!btn) return;

      clearTimeout(_debounce_timer);
      _debounce_timer = setTimeout(function () {
        mobile_payments.pos._enforce_single_selection(e.target, row, btn);
      }, 150);
    }, true);
  },

  _enforce_single_selection: function (changedInput, changedRow, changedBtn) {
    var changedVal   = mobile_payments.pos._parse_amount(changedInput.value);
    var changedLabel = (changedBtn.textContent || "").trim().toUpperCase();
    var isMobile     = mobile_payments.pos._is_mobile_mop(changedLabel);

    if (changedVal <= 0) {
      // Nothing entered — nothing to enforce, but update submit state
      mobile_payments.pos._update_submit_buttons();
      return;
    }

    // Something was entered — clear every other row
    // Guard flag so our own _set_input calls don't retrigger this
    if (mobile_payments.pos._enforcing) return;
    mobile_payments.pos._enforcing = true;

    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      if (r.inp === changedInput) return; // skip the row that was just changed
      mobile_payments.pos._set_input(r.inp, "0");
    });

    // If a non-mobile row was entered, also reset verified flag
    if (!isMobile) {
      mobile_payments.pos._payment_verified = false;
    }

    mobile_payments.pos._update_submit_buttons();
    mobile_payments.pos._enforcing = false;
  },

  /** Apply single-selection when a mobile MOP button is clicked. */
  _apply_single_selection: function (clickedBtn, amount) {
    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      var isMine = r.btn === clickedBtn || r.btn.contains(clickedBtn) || clickedBtn.contains(r.btn);
      mobile_payments.pos._set_input(r.inp, isMine ? amount.toFixed(2) : "0");
    });
  },

  /** After verified success: re-confirm the amount in the mobile MOP row. */
  _confirm_mop_amount: function (amount) {
    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      if (mobile_payments.pos._is_mobile_mop(r.label)) {
        mobile_payments.pos._set_input(r.inp, amount.toFixed(2));
      }
    });
  },

  /** On error/cancel: zero all mobile MOPs, restore cash to full outstanding. */
  _reset_to_cash: function () {
    var total = mobile_payments.pos._get_outstanding();
    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      if (mobile_payments.pos._is_mobile_mop(r.label)) {
        mobile_payments.pos._set_input(r.inp, "0");
      } else if (r.label === "REC CASH" || r.label === "CASH") {
        mobile_payments.pos._set_input(r.inp, total ? total.toFixed(2) : "0");
      }
    });
    mobile_payments.pos._payment_verified = false;
    mobile_payments.pos._update_submit_buttons();
  },

  // ── C. Submit guard ───────────────────────────────────────────
  /**
   * Blocks SUBMIT / SUBMIT & PRINT if a mobile MOP has amount > 0
   * but _payment_verified is false.
   */
  _attach_submit_guard: function () {
    if (mobile_payments.pos._submit_guard_on) return;
    mobile_payments.pos._submit_guard_on = true;

    document.addEventListener("click", function (e) {
      if (!mobile_payments.pos._payment_panel_open()) return;

      var btn = e.target.closest("button, .v-btn");
      if (!btn) return;
      var label = (btn.textContent || "").trim().toUpperCase();

      // Match SUBMIT and SUBMIT & PRINT buttons
      var isSubmit = label === "SUBMIT" || label === "SUBMIT & PRINT"
        || label.indexOf("SUBMIT") !== -1;
      if (!isSubmit) return;

      // Check if any mobile MOP has amount > 0
      var mobileAmount = 0;
      mobile_payments.pos._get_payment_rows().forEach(function (r) {
        if (mobile_payments.pos._is_mobile_mop(r.label)) {
          mobileAmount += mobile_payments.pos._parse_amount(r.inp.value);
        }
      });

      if (mobileAmount > 0 && !mobile_payments.pos._payment_verified) {
        e.stopImmediatePropagation();
        e.preventDefault();
        frappe.show_alert({
          message: __("Mobile payment not yet verified. Please complete the payment first."),
          indicator: "red",
        }, 6);
      }
    }, true); // capture phase — before Vue
  },

  /** Visually indicate submit buttons based on verification state. */
  _update_submit_buttons: function () {
    var mobileAmount = 0;
    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      if (mobile_payments.pos._is_mobile_mop(r.label)) {
        mobileAmount += mobile_payments.pos._parse_amount(r.inp.value);
      }
    });

    var needsVerification = mobileAmount > 0 && !mobile_payments.pos._payment_verified;

    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").trim().toUpperCase();
      if (label.indexOf("SUBMIT") === -1) return;
      if (btn.getBoundingClientRect().width === 0) return;

      if (needsVerification) {
        btn.style.opacity = "0.45";
        btn.title = __("Complete mobile payment verification first");
      } else {
        btn.style.opacity = "";
        btn.title = "";
      }
    });
  },

  // ── Dialog ────────────────────────────────────────────────────
  show_dialog: function (methodObj, amount) {
    var methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) {
      frappe.show_alert({ message: __("No mobile payment methods configured"), indicator: "orange" });
      return;
    }

    var options = methods.map(function (m) {
      return { label: m.label + " (" + m.provider + ")", value: m.provider + "|" + m.method };
    });

    var default_val = options[0] ? options[0].value : "";
    if (methodObj) {
      var match = options.find(function (o) {
        return o.value.toUpperCase().indexOf(methodObj.label.toUpperCase()) !== -1;
      });
      if (match) default_val = match.value;
    }

    var customer = mobile_payments.pos._get_customer();
    var currency  = mobile_payments.pos._get_currency();
    var invoice   = mobile_payments.pos._get_invoice_name();

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
            + frappe.format(amount, { fieldtype: "Currency" }) + " " + currency
            + "</div>"
            + (customer ? '<div class="text-muted">' + customer + "</div>" : "")
            + "</div>",
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
          options: "Subscriber (Mobile Wallet)
Merchant Account",
          default: "Subscriber (Mobile Wallet)",
          description: __("Subscriber = regular customer wallet. Merchant = business till number."),
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
          fieldtype: "Currency",
          label: __("Amount"),
          reqd: 1,
          default: amount,
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
        var parts      = values.payment_method.split("|");
        var provider   = parts[0];
        var method     = parts[1];
        mobile_payments.pos._process(provider, method, phone_clean, values.amount, currency, invoice, customer);
      },
    });

    d.show();

    // Async phone fetch — fills field after dialog opens
    if (customer) {
      frappe.call({
        method: "mobile_payments.api.pos.get_customer_phone",
        args: { customer: customer },
        callback: function (r) {
          if (r.message && r.message.phone) {
            var current = d.get_value("phone_number");
            if (!current) d.set_value("phone_number", r.message.phone);
          }
        },
      });
    }
  },

  // ── Process ───────────────────────────────────────────────────
  _process: function (provider, method, phone, amount, currency, invoice, customer) {
    var spin = new frappe.ui.Dialog({
      title: __("Processing..."),
      fields: [{
        fieldtype: "HTML", fieldname: "html",
        options: '<div style="text-align:center;padding:30px;">'
          + '<i class="fa fa-spinner fa-pulse fa-3x" style="color:#00d4ff;"></i>'
          + '<p style="margin-top:14px;font-weight:500;">' + provider + " — " + method + "</p>"
          + '<p class="text-muted" id="mp-spin-status">' + __("Sending request to") + " " + phone + "...</p>"
          + "</div>",
      }],
      static: true,
    });
    spin.show();
    spin.$wrapper.find(".modal-footer").hide();

    frappe.call({
      method: "mobile_payments.api.pos.initiate_pos_payment",
      args: {
        provider: provider,
        method: method,
        phone: phone,
        amount: amount,
        currency: currency,
        pos_profile: mobile_payments.pos._get_pos_profile(),
        customer: customer || "",
        invoice_name: invoice || "",
      },
      callback: function (r) {
        var res = (r && r.message) ? r.message : {};
        if (res.success) {
          spin.hide();
          mobile_payments.pos._on_success(res, amount, method);
        } else if (res.pending) {
          mobile_payments.pos._poll(res.transaction_log, spin, amount, method);
        } else {
          spin.hide();
          mobile_payments.pos._on_error(res.message || __("Payment failed"));
        }
      },
      error: function () {
        spin.hide();
        mobile_payments.pos._on_error(__("Connection error — please try again"));
      },
    });
  },

  // ── Poll ──────────────────────────────────────────────────────
  _poll: function (txlog, dialog, amount, method) {
    var count = 0;
    var max   = 60;

    dialog.$wrapper.find(".modal-footer").show();
    dialog.set_primary_action(__("Cancel"), function () {
      clearInterval(timer);
      dialog.hide();
      mobile_payments.pos._on_error(__("Cancelled by user"));
    });

    var timer = setInterval(function () {
      count++;
      var el = dialog.$wrapper.find("#mp-spin-status");
      if (el.length) el.text(__("Waiting for confirmation… ({0}s)", [count * 2]));

      if (count > max) {
        clearInterval(timer);
        dialog.hide();
        mobile_payments.pos._on_error(__("Timed out — customer did not respond"));
        return;
      }

      frappe.call({
        method: "mobile_payments.api.pos.check_pos_payment_status",
        args: { transaction_log: txlog },
        callback: function (r) {
          if (!r || !r.message) return;
          var s = r.message.status;
          if (s === "Completed") {
            clearInterval(timer);
            dialog.hide();
            mobile_payments.pos._on_success(r.message, amount, method);
          } else if (s === "Failed" || s === "Cancelled") {
            clearInterval(timer);
            dialog.hide();
            mobile_payments.pos._on_error(r.message.error_message || __("Payment {0}", [s.toLowerCase()]));
          }
        },
      });
    }, 2000);
  },

  // ── Outcome ───────────────────────────────────────────────────
  _on_success: function (result, amount, method) {
    mobile_payments.pos._last_transaction  = result;
    mobile_payments.pos._payment_verified  = true;

    mobile_payments.pos._confirm_mop_amount(amount);
    mobile_payments.pos._update_submit_buttons();

    frappe.show_alert({
      message: __("{0}: {1} paid ✓ — ready to submit", [method, frappe.format(amount, { fieldtype: "Currency" })]),
      indicator: "green",
    }, 8);
  },

  _on_error: function (message) {
    mobile_payments.pos._reset_to_cash();
    frappe.msgprint({
      title: __("Payment Failed"),
      indicator: "red",
      message: "<p>" + message + "</p><p class='text-muted'>" + __("Please try again.") + "</p>",
    });
  },

  // ── Context helpers ───────────────────────────────────────────
  _get_currency: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && cur_frm.doc.currency)
        return cur_frm.doc.currency;
      var root = document.querySelector("#pos-awesome-root, .pos-awesome-app");
      if (root && root.__vue__) {
        var vue = root.__vue__;
        if (vue.currency) return vue.currency;
        if (vue.$store && vue.$store.state.currency) return vue.$store.state.currency;
      }
    } catch (e) {}
    return frappe.defaults.get_global_default("currency") || "USD";
  },

  _get_customer: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc)
        if (cur_frm.doc.customer || cur_frm.doc.customer_name)
          return cur_frm.doc.customer || cur_frm.doc.customer_name;

      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app']"
      );
      for (var i = 0; i < roots.length; i++) {
        var vue = roots[i].__vue__;
        if (!vue) continue;
        if (vue.customer) return vue.customer;
        if (vue.$store && vue.$store.state.customer) return vue.$store.state.customer;
        if (vue.$children) {
          for (var j = 0; j < vue.$children.length; j++) {
            var child = vue.$children[j];
            if (child.customer) return child.customer;
            if (child.$store && child.$store.state.customer) return child.$store.state.customer;
          }
        }
      }

      var selectors = [".customer-name", ".pos-customer-name", "[class*='customer'] strong", ".posa-customer"];
      for (var k = 0; k < selectors.length; k++) {
        var el = document.querySelector(selectors[k]);
        if (el) {
          var text = (el.textContent || "").trim();
          if (text && text.length > 1 && !/^(customer|select|search)/i.test(text)) return text;
        }
      }
    } catch (e) {}
    return "";
  },

  _get_pos_profile: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && cur_frm.doc.pos_profile)
        return cur_frm.doc.pos_profile;
      var root = document.querySelector("#pos-awesome-root, .pos-awesome-app");
      if (root && root.__vue__ && root.__vue__.pos_profile) return root.__vue__.pos_profile;
    } catch (e) {}
    return "";
  },

  _get_invoice_name: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && cur_frm.doc.name)
        return cur_frm.doc.name;
    } catch (e) {}
    return "";
  },

  _is_pos_page: function () {
    try {
      var path = (window.location.pathname + window.location.hash).toLowerCase();
      if (/pos/.test(path)) return true;
      if (/pos/.test((frappe.get_route_str() || "").toLowerCase())) return true;
    } catch (e) {}
    return false;
  },
};

// ── Boot ─────────────────────────────────────────────────────────
$(document).ready(function () {
  if (mobile_payments.pos._is_pos_page()) mobile_payments.pos.init();
  [2000, 5000].forEach(function (ms) {
    setTimeout(function () {
      if (mobile_payments.pos._is_pos_page() && !mobile_payments.pos._initialized)
        mobile_payments.pos.init();
    }, ms);
  });
});

var _mp_nav_handler = function () {
  setTimeout(function () {
    if (mobile_payments.pos._is_pos_page()) {
      mobile_payments.pos._initialized      = false;
      mobile_payments.pos._intercepting     = false;
      mobile_payments.pos._watching         = false;
      mobile_payments.pos._submit_guard_on  = false;
      mobile_payments.pos._payment_verified = false;
      mobile_payments.pos.init();
    }
  }, 600);
};

if (frappe.router && frappe.router.on) {
  frappe.router.on("change", _mp_nav_handler);
} else {
  $(window).on("hashchange", _mp_nav_handler);
}

// Link transaction on invoice submit
frappe.ui.form.on("POS Invoice", {
  on_submit: function (frm) {
    var tx = mobile_payments.pos._last_transaction;
    if (!tx || !tx.transaction_log) return;
    frappe.call({
      method: "mobile_payments.api.pos.link_pos_invoice",
      args: { transaction_log: tx.transaction_log, invoice_name: frm.doc.name },
      callback: function (r) {
        if (r.message && r.message.success)
          frappe.show_alert({ message: __("Mobile payment linked to {0}", [frm.doc.name]), indicator: "green" });
      },
    });
    mobile_payments.pos._last_transaction  = null;
    mobile_payments.pos._payment_verified  = false;
  },
});

frappe.ui.form.on("Sales Invoice", {
  on_submit: function (frm) {
    if (!frm.doc.is_pos) return;
    var tx = mobile_payments.pos._last_transaction;
    if (!tx || !tx.transaction_log) return;
    frappe.call({
      method: "mobile_payments.api.pos.link_pos_invoice",
      args: { transaction_log: tx.transaction_log, invoice_name: frm.doc.name },
    });
    mobile_payments.pos._last_transaction  = null;
    mobile_payments.pos._payment_verified  = false;
  },
});
