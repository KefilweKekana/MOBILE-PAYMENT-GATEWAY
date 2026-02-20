/**
 * Mobile Payments — POS Awesome Integration
 *
 * How it works:
 *   1. ZAAD / SAHAL / EVCPlus / Edahab are standard ERPNext "Phone" type
 *      Modes of Payment added to the POS Profile by the admin.
 *   2. POS Awesome renders them natively as payment buttons — no DOM injection.
 *   3. This script intercepts clicks on those buttons, sets the mobile
 *      payment amount to the full outstanding total, zeroes cash, and shows
 *      the phone-number dialog.
 *   4. On success the payment is confirmed and POS can be submitted normally.
 */

frappe.provide("mobile_payments.pos");

mobile_payments.pos = {

  // ── State ──────────────────────────────────────────────────────
  _methods: null,

  /** Parse a number from a DOM input value that may contain commas e.g. "3,500.00" */
  _parse_amount: function (val) {
    if (val === null || val === undefined) return 0;
    // Remove every character that isn't a digit or a decimal point
    var cleaned = String(val).replace(/[^0-9.]/g, "");
    return parseFloat(cleaned) || 0;
  },
  _enabled: false,
  _initialized: false,
  _last_transaction: null,
  _intercepting: false,
  _mobile_mop_names: [],

  // ── Init ───────────────────────────────────────────────────────
  init: function () {
    if (mobile_payments.pos._initialized) return;
    mobile_payments.pos._initialized = true;

    frappe.call({
      method: "mobile_payments.api.pos.get_mobile_payment_methods",
      async: false,
      callback: function (r) {
        if (r.message && r.message.enabled) {
          mobile_payments.pos._enabled = true;
          mobile_payments.pos._methods = r.message.methods || [];
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
  },

  // ── Click interceptor ──────────────────────────────────────────
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

      var methodObj = mobile_payments.pos._method_from_label(label);
      var outstanding = mobile_payments.pos._get_outstanding();

      if (outstanding <= 0) {
        frappe.show_alert({ message: __("Cart is empty"), indicator: "orange" });
        return;
      }

      // Set this MOP to full amount, zero cash
      mobile_payments.pos._set_mop_amounts(btn, outstanding);

      // Show dialog
      mobile_payments.pos.show_dialog(methodObj, outstanding);

    }, true); // capture phase — runs before Vue
  },

  // ── Dialog ─────────────────────────────────────────────────────
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
      var match = options.find(function (o) { return o.value.toUpperCase().includes(methodObj.label.toUpperCase()); });
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
          fieldname: "phone_number",
          fieldtype: "Data",
          label: __("Customer Phone"),
          description: __("e.g. 252612345678"),
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
        var parts    = values.payment_method.split("|");
        var provider = parts[0];
        var method   = parts[1];
        mobile_payments.pos._process(provider, method, phone_clean, values.amount, currency, invoice, customer);
      },
    });

    d.show();

    // Fetch phone asynchronously and fill field once available
    if (customer) {
      frappe.call({
        method: "mobile_payments.api.pos.get_customer_phone",
        args: { customer: customer },
        callback: function (r) {
          if (r.message && r.message.phone) {
            // Only fill if cashier hasn't already typed something
            var current = d.get_value("phone_number");
            if (!current) d.set_value("phone_number", r.message.phone);
          }
        },
      });
    }
  },

  // ── Process ────────────────────────────────────────────────────
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

  // ── Poll ───────────────────────────────────────────────────────
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

  // ── Outcome ────────────────────────────────────────────────────
  _on_success: function (result, amount, method) {
    mobile_payments.pos._last_transaction = result;

    frappe.show_alert({
      message: __("{0}: {1} paid ✓", [method, frappe.format(amount, { fieldtype: "Currency" })]),
      indicator: "green",
    }, 8);

    // Fire native input events on the MOP field so POS totals recalculate
    mobile_payments.pos._confirm_mop_amount(amount, method);
  },

  _on_error: function (message) {
    // Restore amounts: zero mobile MOP, put total back into cash
    mobile_payments.pos._reset_to_cash();
    frappe.msgprint({
      title: __("Payment Failed"),
      indicator: "red",
      message: "<p>" + message + "</p><p class='text-muted'>" + __("Please try again.") + "</p>",
    });
  },

  // ── MOP amount helpers ─────────────────────────────────────────

  /** When mobile MOP clicked: fill it with full amount, zero cash. */
  _set_mop_amounts: function (clickedBtn, amount) {
    document.querySelectorAll(".v-row").forEach(function (row) {
      var btn = row.querySelector("button, .v-btn");
      var inp = row.querySelector("input");
      if (!btn || !inp) return;
      if (btn.getBoundingClientRect().width === 0) return;

      var label = (btn.textContent || "").trim().toUpperCase();
      var isMine = btn === clickedBtn || btn.contains(clickedBtn) || clickedBtn.contains(btn);

      if (isMine) {
        inp.value = amount.toFixed(2);
      } else if (label === "REC CASH" || label === "CASH") {
        inp.value = "0";
      }
      inp.dispatchEvent(new Event("input",  { bubbles: true }));
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    });
  },

  /** After confirmed success: re-fire events so POS totals update. */
  _confirm_mop_amount: function (amount, method) {
    document.querySelectorAll(".v-row").forEach(function (row) {
      var btn = row.querySelector("button, .v-btn");
      var inp = row.querySelector("input");
      if (!btn || !inp) return;
      var label = (btn.textContent || "").trim().toUpperCase();
      if (mobile_payments.pos._is_mobile_mop(label)) {
        inp.value = amount.toFixed(2);
        inp.dispatchEvent(new Event("input",  { bubbles: true }));
        inp.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  },

  /** On failure: restore cash to the outstanding total. */
  _reset_to_cash: function () {
    var total = mobile_payments.pos._get_outstanding();
    document.querySelectorAll(".v-row").forEach(function (row) {
      var btn = row.querySelector("button, .v-btn");
      var inp = row.querySelector("input");
      if (!btn || !inp) return;
      if (btn.getBoundingClientRect().width === 0) return;
      var label = (btn.textContent || "").trim().toUpperCase();
      if (mobile_payments.pos._is_mobile_mop(label)) {
        inp.value = "0";
      } else if (label === "REC CASH" || label === "CASH") {
        inp.value = total ? total.toFixed(2) : "0";
      }
      inp.dispatchEvent(new Event("input",  { bubbles: true }));
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    });
  },

  // ── Detection helpers ──────────────────────────────────────────

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

  _get_outstanding: function () {
    // Read from the REC CASH input — POS fills it with the grand total
    var btns = document.querySelectorAll("button, .v-btn");
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].textContent || "").trim().toUpperCase();
      if (t === "REC CASH" || t === "CASH") {
        var row = btns[i].closest(".v-row");
        if (row) {
          var inp = row.querySelector("input");
          if (inp) { var v = mobile_payments.pos._parse_amount(inp.value); if (v > 0) return v; }
        }
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
      // cur_frm works in classic POS / Sales Invoice
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc)
        if (cur_frm.doc.customer || cur_frm.doc.customer_name)
          return cur_frm.doc.customer || cur_frm.doc.customer_name;

      // POS Awesome Vue root (several possible selectors across versions)
      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app']"
      );
      for (var i = 0; i < roots.length; i++) {
        var vue = roots[i].__vue__;
        if (!vue) continue;
        if (vue.customer) return vue.customer;
        if (vue.$store && vue.$store.state.customer) return vue.$store.state.customer;
        // Drill into child components
        if (vue.$children) {
          for (var j = 0; j < vue.$children.length; j++) {
            var child = vue.$children[j];
            if (child.customer) return child.customer;
            if (child.$store && child.$store.state.customer) return child.$store.state.customer;
          }
        }
      }

      // POS Awesome DOM fallback — customer name shown in the UI
      var selectors = [
        ".customer-name", ".pos-customer-name", "[class*='customer'] strong",
        ".v-card-title", ".posa-customer"
      ];
      for (var k = 0; k < selectors.length; k++) {
        var el = document.querySelector(selectors[k]);
        if (el) {
          var text = (el.textContent || "").trim();
          if (text && text.length > 1 && !/^(customer|select|search)/i.test(text))
            return text;
        }
      }
    } catch (e) {}
    return "";
  },

  // _get_customer_phone_sync removed — phone is now fetched async after dialog opens

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

// ── Boot ──────────────────────────────────────────────────────────
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
      mobile_payments.pos._initialized = false;
      mobile_payments.pos._intercepting = false;
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
    mobile_payments.pos._last_transaction = null;
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
    mobile_payments.pos._last_transaction = null;
  },
});
