/**
 * Mobile Payments — POS Awesome Integration  (v38 — poll-based)
 *
 * Strategy:
 *   POS Awesome uses Vue @click directives that CANNOT be intercepted by DOM
 *   stopImmediatePropagation(). So we don't fight it.
 *
 *   Instead we:
 *     1. Let POS Awesome handle button clicks normally (it sets the amount)
 *     2. Poll every 400ms — when a mobile MOP goes from $0 → $X, auto-show
 *        our payment dialog
 *     3. Guard SUBMIT — if mobile MOP has amount but not verified, block and
 *        show the dialog
 *     4. On success → _payment_verified = true → SUBMIT goes through
 */

// Ensure global namespace exists BEFORE frappe.provide
window.mobile_payments = window.mobile_payments || {};
window.mobile_payments.pos = window.mobile_payments.pos || {};

frappe.provide("mobile_payments.pos");

mobile_payments.pos = {

  // ── State ─────────────────────────────────────────────────────
  _methods:           null,
  _enabled:           false,
  _initialized:       false,
  _watching:          false,
  _last_transaction:  null,
  _mobile_mop_names:  [],
  _payment_verified:  false,
  _verified_mop_label: "",   // which MOP was actually paid
  _submit_guard_on:   false,
  _dialog_open:       false,   // true while our dialog is showing
  _processing:        false,   // true while payment is in-flight
  _cooldown_until:    0,       // timestamp — skip detection until this time
  _just_submitted:    false,   // true for a few seconds after our submit — suppresses stale-doc errors
  _prev_mobile_amount: 0,      // last detected mobile MOP amount (moved from closure for reset)
  _prev_mobile_label:  "",     // last detected mobile MOP label  (moved from closure for reset)

  // ── Helpers ───────────────────────────────────────────────────

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
    var rows = mobile_payments.pos._get_payment_rows();
    if (rows.length > 0) return true;
    var cashRe = /^(rec\s*cash|cash)$/i;
    var btns = document.querySelectorAll("button, .v-btn");
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].textContent || "").trim();
      if (cashRe.test(t)) {
        var r = btns[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return true;
      }
    }
    return false;
  },

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
    var rows = mobile_payments.pos._get_payment_rows();
    var cashRe = /cash/i;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (cashRe.test(r.label)) {
        var v = mobile_payments.pos._parse_amount(r.inp.value);
        if (v > 0) return v;
      }
    }
    for (var j = 0; j < rows.length; j++) {
      var v2 = mobile_payments.pos._parse_amount(rows[j].inp.value);
      if (v2 > 0) return v2;
    }
    try {
      var roots = document.querySelectorAll("#pos-awesome-root, .pos-awesome-app, [id^='posa']");
      for (var k = 0; k < roots.length; k++) {
        var vue = roots[k].__vue__;
        if (!vue) continue;
        if (vue.$store && vue.$store.state) {
          var st = vue.$store.state;
          var total = parseFloat(
            st.invoice_doc && (st.invoice_doc.rounded_total || st.invoice_doc.grand_total)
            || st.rounded_total || st.grand_total || st.total || 0
          );
          if (total > 0) return total;
        }
        var direct = parseFloat(vue.rounded_total || vue.grand_total || vue.total || 0);
        if (direct > 0) return direct;
      }
    } catch (e) {}
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc) {
        var fv = flt(cur_frm.doc.rounded_total || cur_frm.doc.grand_total || 0);
        if (fv > 0) return fv;
      }
    } catch (e) {}
    try {
      var totalSelectors = [
        ".grand-total .value", ".pos-grand-total", "[class*='grand-total'] .value",
        ".posa-grand-total", ".total-amount", ".net-total"
      ];
      for (var t = 0; t < totalSelectors.length; t++) {
        var el = document.querySelector(totalSelectors[t]);
        if (el) {
          var parsed = mobile_payments.pos._parse_amount(el.textContent || el.innerText);
          if (parsed > 0) return parsed;
        }
      }
    } catch (e) {}
    return 0;
  },

  _set_input: function (inp, value) {
    inp.value = value;
    // Fire only 'input' — firing both 'input' AND 'change' causes Vue to
    // trigger two auto-saves within ms of each other, causing a timestamp
    // mismatch when POS Awesome tries to submit the draft invoice.
    inp.dispatchEvent(new Event("input", { bubbles: true }));
  },

  // ── Init ──────────────────────────────────────────────────────
  init: function () {
    if (mobile_payments.pos._initialized) return;
    mobile_payments.pos._initialized = true;

    console.log("mobile_payments.pos.init() — v38 poll-based");

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
          console.log("mobile_payments.pos: enabled, methods:", mobile_payments.pos._mobile_mop_names);
        }
      },
    });

    mobile_payments.pos._attach_mop_watcher();
    mobile_payments.pos._attach_submit_guard();
  },

  // ── A. Poll-based MOP watcher ─────────────────────────────────
  //
  // Polls every 400ms. When a mobile MOP amount transitions from 0 to >0,
  // auto-opens the payment dialog. This works regardless of how POS Awesome
  // sets the value (Vue reactivity, direct DOM manipulation, etc.)
  //
  _attach_mop_watcher: function () {
    if (mobile_payments.pos._watching) return;
    mobile_payments.pos._watching = true;

    setInterval(function () {
      if (!mobile_payments.pos._enabled) return;
      if (!mobile_payments.pos._payment_panel_open()) {
        mobile_payments.pos._prev_mobile_amount = 0;
        mobile_payments.pos._prev_mobile_label  = "";
        return;
      }
      if (mobile_payments.pos._dialog_open) return;
      if (mobile_payments.pos._processing) return;
      if (mobile_payments.pos._payment_verified) return;
      if (Date.now() < mobile_payments.pos._cooldown_until) return;

      // Find first mobile MOP with amount > 0
      var mobileAmount = 0;
      var mobileLabel  = "";
      var mobileBtn    = null;
      var rows = mobile_payments.pos._get_payment_rows();
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        if (!mobile_payments.pos._is_mobile_mop(r.label)) continue;
        var val = mobile_payments.pos._parse_amount(r.inp.value);
        if (val > 0) {
          mobileAmount = val;
          mobileLabel  = r.label;
          mobileBtn    = r.btn;
          break;
        }
      }

      // Trigger when mobile amount goes from 0 → non-zero, OR label changes
      var shouldTrigger = (
        mobileAmount > 0
        && (mobile_payments.pos._prev_mobile_amount === 0 || mobileLabel !== mobile_payments.pos._prev_mobile_label)
      );

      mobile_payments.pos._prev_mobile_amount = mobileAmount;
      mobile_payments.pos._prev_mobile_label  = mobileLabel;

      if (shouldTrigger) {
        console.log("mobile_payments.pos: detected mobile MOP:", mobileLabel, "amount:", mobileAmount);

        // Immediately zero all other payment rows — enforce single selection
        rows.forEach(function (r) {
          if (r.btn !== mobileBtn) {
            mobile_payments.pos._set_input(r.inp, "0");
          }
        });

        mobile_payments.pos._payment_verified = false;
        mobile_payments.pos._update_submit_buttons();

        var methodObj = mobile_payments.pos._method_from_label(mobileLabel);
        mobile_payments.pos.show_dialog(methodObj, mobileAmount, mobileBtn);
      }
    }, 400);
  },

  // ── B. Submit guard ───────────────────────────────────────────
  //
  // If a mobile MOP has amount > 0 but payment is not verified:
  //   - Block the submit
  //   - Auto-open the payment dialog (not just an alert)
  //
  _attach_submit_guard: function () {
    if (mobile_payments.pos._submit_guard_on) return;
    mobile_payments.pos._submit_guard_on = true;

    document.addEventListener("click", function (e) {
      if (!mobile_payments.pos._enabled) return;
      if (!mobile_payments.pos._payment_panel_open()) return;

      var btn = e.target.closest("button, .v-btn");
      if (!btn) return;
      var label = (btn.textContent || "").trim().toUpperCase();

      var isSubmit = label === "SUBMIT" || label === "SUBMIT & PRINT"
        || label.indexOf("SUBMIT") !== -1;
      if (!isSubmit) return;

      // Find active mobile MOP
      var mobileAmount = 0;
      var mobileLabel  = "";
      var mobileBtn    = null;
      mobile_payments.pos._get_payment_rows().forEach(function (r) {
        if (mobile_payments.pos._is_mobile_mop(r.label)) {
          var val = mobile_payments.pos._parse_amount(r.inp.value);
          if (val > 0) {
            mobileAmount = val;
            mobileLabel  = r.label;
            mobileBtn    = r.btn;
          }
        }
      });

      if (mobileAmount > 0 && !mobile_payments.pos._payment_verified) {
        e.stopImmediatePropagation();
        e.preventDefault();

        if (!mobile_payments.pos._dialog_open) {
          console.log("mobile_payments.pos: submit blocked, opening payment dialog");
          var methodObj = mobile_payments.pos._method_from_label(mobileLabel);
          mobile_payments.pos.show_dialog(methodObj, mobileAmount, mobileBtn);
        } else {
          frappe.show_alert({
            message: __("Please complete the mobile payment dialog first."),
            indicator: "orange",
          }, 4);
        }
      }
    }, true); // capture phase
  },

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

  /** After verified success: set amount ONLY on the verified mobile MOP, zero everything else. */
  _confirm_mop_amount: function (amount) {
    var verifiedLabel = mobile_payments.pos._verified_mop_label || "";
    var foundVerified = false;

    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      // Match the exact MOP that was paid
      var isVerifiedMop = verifiedLabel && r.label.toUpperCase().indexOf(verifiedLabel) !== -1;
      if (!foundVerified && isVerifiedMop) {
        mobile_payments.pos._set_input(r.inp, amount.toFixed(2));
        foundVerified = true;
        console.log("mobile_payments.pos: setting", r.label, "=", amount.toFixed(2));
      } else {
        mobile_payments.pos._set_input(r.inp, "0");
      }
    });

    // Fallback: if we didn't find the verified MOP, set first mobile MOP
    if (!foundVerified) {
      mobile_payments.pos._get_payment_rows().forEach(function (r) {
        if (!foundVerified && mobile_payments.pos._is_mobile_mop(r.label)) {
          mobile_payments.pos._set_input(r.inp, amount.toFixed(2));
          foundVerified = true;
        }
      });
    }
  },

  /**
   * Full state reset — call after a completed transaction so the next
   * transaction can trigger the popup cleanly without a page refresh.
   */
  _full_reset: function () {
    mobile_payments.pos._payment_verified   = false;
    mobile_payments.pos._verified_mop_label = "";
    mobile_payments.pos._last_transaction   = null;
    mobile_payments.pos._dialog_open        = false;
    mobile_payments.pos._processing         = false;
    mobile_payments.pos._submitting         = false;
    mobile_payments.pos._submitted_name     = null;
    mobile_payments.pos._prev_mobile_amount = 0;
    mobile_payments.pos._prev_mobile_label  = "";
    mobile_payments.pos._cooldown_until     = Date.now() + 2500; // brief cooldown for Vue to settle
    mobile_payments.pos._just_submitted     = false;
    mobile_payments.pos._update_submit_buttons();
    console.log("mobile_payments.pos: _full_reset — ready for next transaction");
  },

  /** On error/cancel: zero all mobile MOPs, restore cash to full outstanding. */
  _reset_to_cash: function () {
    var total = mobile_payments.pos._get_outstanding();
    var cashRe = /cash/i;
    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      if (mobile_payments.pos._is_mobile_mop(r.label)) {
        mobile_payments.pos._set_input(r.inp, "0");
      } else if (cashRe.test(r.label)) {
        mobile_payments.pos._set_input(r.inp, total ? total.toFixed(2) : "0");
      }
    });
    mobile_payments.pos._payment_verified = false;
    mobile_payments.pos._verified_mop_label = "";
    mobile_payments.pos._update_submit_buttons();
  },

  // ── Dialog ────────────────────────────────────────────────────
  show_dialog: function (methodObj, amount, clickedBtn) {
    if (mobile_payments.pos._dialog_open) return; // prevent double-open
    mobile_payments.pos._dialog_open = true;

    var methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) {
      mobile_payments.pos._dialog_open = false;
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
    var currency = mobile_payments.pos._get_currency();
    var invoice  = mobile_payments.pos._get_invoice_name();

    // POS payment row amount is already in the transaction currency (USD or SLSH)
    // No conversion needed — use the amount as-is
    var display_amount = amount;

    console.log("mobile_payments.pos: dialog context — customer:", customer, "invoice:", invoice, "currency:", currency, "amount:", display_amount);

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
            + parseFloat(display_amount).toFixed(2) + " " + currency
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
          options: "Subscriber (Mobile Wallet)\nMerchant Account",
          default: "Subscriber (Mobile Wallet)",
          description: __("Subscriber = regular customer wallet. Merchant = business till number."),
          onchange: function () {
            var is_merchant = (d.get_value("account_type") || "").toLowerCase().indexOf("merchant") !== -1;
            var phone_field = d.get_field("phone_number");
            if (phone_field) {
              phone_field.df.label       = is_merchant ? __("Merchant Till Number") : __("Customer Phone");
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
          default: parseFloat(display_amount).toFixed(2),
          read_only: 1,
        },
      ],
      primary_action_label: __("Process Payment"),
      primary_action: function (values) {
        var phone_clean = (values.phone_number || "").replace(/[\s\-()+]/g, "");
        var is_merchant = (values.account_type || "").toLowerCase().indexOf("merchant") !== -1;
        var phone_valid = is_merchant ? /^\d+$/.test(phone_clean) : /^\d{7,15}$/.test(phone_clean);
        if (!phone_clean || !phone_valid) {
          var msg = is_merchant
            ? __("Enter a valid merchant till number (digits only, e.g. 7853)")
            : __("Enter a valid phone number (7–15 digits)");
          frappe.show_alert({ message: msg, indicator: "red" });
          return;
        }

        // Mark as processing BEFORE hiding — so close handler doesn't reset
        mobile_payments.pos._processing = true;
        d.hide();

        mobile_payments.pos._update_submit_buttons();

        var parts      = values.payment_method.split("|");
        var provider   = parts[0];
        var method     = parts[1];
        var acct_type  = values.account_type || "Subscriber (Mobile Wallet)";
        mobile_payments.pos._process(provider, method, phone_clean, values.amount, currency, invoice, customer, acct_type);
      },
    });

    // On dialog close (X button or cancel) — reset only if NOT processing
    d.on_page_show = function () {};
    d.$wrapper.on("hidden.bs.modal", function () {
      mobile_payments.pos._dialog_open = false;
      // 2-second cooldown so Vue reactivity settling doesn't re-trigger
      mobile_payments.pos._cooldown_until = Date.now() + 2000;
      // Only reset if user manually closed (X or Escape), NOT during processing
      if (!mobile_payments.pos._payment_verified && !mobile_payments.pos._processing) {
        mobile_payments.pos._reset_to_cash();
      }
    });

    d.show();

    // Smart phone auto-fill: picks the right number based on provider
    // (Edahab prefixes: 65, 66, 62, 76 — everything else → WaafiPay)
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
        args: { customer: customer, provider: provider },
        callback: function (r) {
          if (r.message) {
            _cached_phones = r.message;
            console.log("mobile_payments.pos: smart phone routing:", r.message);
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

    // Try Vue store first (instant), then smart API
    var vue_phone = mobile_payments.pos._get_customer_phone_from_vue();
    console.log("mobile_payments.pos: phone auto-fill — vue_phone:", vue_phone, "customer:", customer);
    if (vue_phone) {
      d.set_value("phone_number", vue_phone);
      // Still fetch all phones in background for provider switching
      if (customer) {
        frappe.call({
          method: "mobile_payments.api.pos.get_customer_phone_for_provider",
          args: { customer: customer },
          callback: function (r) { if (r.message) _cached_phones = r.message; },
        });
      }
    } else {
      _fill_phone_for_provider();
    }
  },

  // ── Process ───────────────────────────────────────────────────
  _process: function (provider, method, phone, amount, currency, invoice, customer, account_type) {
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
        account_type: account_type || "Subscriber (Mobile Wallet)",
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
    mobile_payments.pos._last_transaction   = result;
    mobile_payments.pos._payment_verified   = true;
    mobile_payments.pos._dialog_open        = false;
    mobile_payments.pos._processing         = false;
    mobile_payments.pos._verified_mop_label = (method || "").toUpperCase();

    mobile_payments.pos._update_submit_buttons();

    // Show a success screen with a manual "Submit & Print" button.
    // The button delegates to the real POS button — avoiding any preauth race condition.
    // NOTE: We do NOT call _confirm_mop_amount here — the MOP amount was already
    // set by the user clicking the button. Calling _set_input again would trigger
    // a Vue auto-save on the draft invoice, causing a TimestampMismatchError when
    // POS Awesome tries to submit 60ms later.
    mobile_payments.pos._show_success_dialog(amount, method);
  },

  _show_success_dialog: function (amount, method) {
    var currency = mobile_payments.pos._get_currency();

    var d = new frappe.ui.Dialog({
      title: __("Payment Successful"),
      static: true,
      fields: [{
        fieldtype: "HTML",
        fieldname: "success_html",
        options:
          '<div style="text-align:center; padding: 24px 16px 8px;">'

          // ── Green tick ──
          + '<div style="'
          +   'width:72px; height:72px; border-radius:50%;'
          +   'background:#d4edda; margin:0 auto 16px;'
          +   'display:flex; align-items:center; justify-content:center;">'
          +   '<i class="fa fa-check" style="font-size:36px; color:#28a745;"></i>'
          + '</div>'

          // ── Amount ──
          + '<div style="font-size:26px; font-weight:700; color:#28a745; margin-bottom:4px;">'
          +   parseFloat(amount).toFixed(2) + " " + currency
          + '</div>'

          // ── Method label ──
          + '<div style="font-size:14px; color:#6c757d; margin-bottom:16px;">'
          +   (method || __("Mobile Payment")) + " " + __("payment received")
          + '</div>'

          // ── Status text ──
          + '<div style="font-size:12px; color:#6c757d;">'
          +   __("Submitting…")
          + '</div>'

          + '</div>',
      }],
    });

    // Hide the X close button and the entire footer (no visible buttons)
    d.$wrapper.find(".btn-modal-close, .modal-header .close").hide();
    d.$wrapper.find(".modal-footer").hide();

    d.show();

    // Submit instantly (small delay for Vue auto-save to settle)
    setTimeout(function () {
      d.hide();
      mobile_payments.pos._delegate_submit();
    }, 500);
  },

  /**
   * Submit the POS invoice directly via API.
   * This completely bypasses POS Awesome's button click — avoiding any
   * TimestampMismatchError caused by Vue auto-saves on the draft invoice.
   */
  _delegate_submit: function () {
    if (!mobile_payments.pos._payment_verified) return;

    // MUTEX: if already submitting, wait 2s and check again once
    if (mobile_payments.pos._submitting) {
      setTimeout(function () {
        // By now the first submit should be done — if already submitted, just return.
        if (mobile_payments.pos._submitted_name) {
          console.log("mobile_payments: already submitted:", mobile_payments.pos._submitted_name);
        }
      }, 2000);
      return;
    }
    mobile_payments.pos._submitting = true;
    mobile_payments.pos._submitted_name = null;

    frappe.show_alert({ message: __("Submitting invoice…"), indicator: "blue" }, 3);

    // Disable POS Awesome submit buttons so they can't fire concurrently
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").replace(/\s+/g, " ").trim().toUpperCase();
      if (label.indexOf("SUBMIT") !== -1) btn.disabled = true;
    });

    var pos_doc = mobile_payments.pos._get_pos_doc();
    if (!pos_doc || !pos_doc.name) {
      // Fallback A: ask the server for the current draft POS invoice
      console.log("mobile_payments: Vue store lookup failed, trying server fallback…");
      frappe.call({
        method: "mobile_payments.api.pos.get_current_draft_pos_invoice",
        async: false,   // must block — we need the name before proceeding
        callback: function (r) {
          if (r && r.message && r.message.name) {
            pos_doc = r.message;
            console.log("mobile_payments: server fallback found:", pos_doc.doctype, pos_doc.name);
          }
        },
      });
    }

    if (!pos_doc || !pos_doc.name) {
      // Fallback B: physically click POS Awesome's own submit button
      console.log("mobile_payments: no invoice found at all — clicking POS submit button");
      mobile_payments.pos._re_enable_buttons();
      mobile_payments.pos._just_submitted = true;
      setTimeout(function () { mobile_payments.pos._just_submitted = false; }, 8000);

      // Save transaction info BEFORE reset clears it
      var _txn_log = (mobile_payments.pos._last_transaction || {}).transaction_log;

      mobile_payments.pos._click_submit_button();

      // After POS submits, find the new Sales Invoice and link the transaction
      if (_txn_log) {
        var _link_attempts = 0;
        var _link_timer = setInterval(function () {
          _link_attempts++;
          if (_link_attempts > 10) { clearInterval(_link_timer); return; }

          // Look for a just-submitted invoice in the DOM or via server
          frappe.call({
            method: "mobile_payments.api.pos.link_latest_pos_invoice",
            args: { transaction_log: _txn_log },
            callback: function (r) {
              if (r && r.message && r.message.success) {
                clearInterval(_link_timer);
                console.log("mobile_payments: linked transaction to", r.message.invoice_name);
              }
            },
          });
        }, 2000);
      }

      // Reset state for next transaction after a delay (let POS Awesome finish its submit)
      setTimeout(function () { mobile_payments.pos._full_reset(); }, 3000);
      return;
    }

    var invoice_name = pos_doc.name;

    // Fetch fresh doc from server to get current timestamp
    frappe.call({
      method: "frappe.client.get",
      args: { doctype: pos_doc.doctype || "POS Invoice", name: invoice_name },
      callback: function (r) {
        var fresh = r && r.message;
        if (!fresh) {
          mobile_payments.pos._submitting = false;
          mobile_payments.pos._re_enable_buttons();
          frappe.show_alert({
            message: __("Payment verified ✓ — please click SUBMIT manually."),
            indicator: "orange",
          }, 8);
          return;
        }

        // If already submitted, finish up
        if (fresh.docstatus === 1) {
          mobile_payments.pos._finish_submit(fresh.name, invoice_name);
          return;
        }

        frappe.call({
          method: "frappe.client.submit",
          args: { doc: fresh },
          callback: function (r2) {
            if (r2 && r2.exc) {
              // Already submitted? Finish up
              mobile_payments.pos._finish_submit(invoice_name, invoice_name);
              return;
            }
            var submitted_name = (r2 && r2.message && r2.message.name)
              ? r2.message.name : invoice_name;
            mobile_payments.pos._finish_submit(submitted_name, invoice_name);
          },
          error: function () {
            mobile_payments.pos._submitting = false;
            mobile_payments.pos._re_enable_buttons();
            frappe.show_alert({
              message: __("Submit failed — please click SUBMIT manually."),
              indicator: "orange",
            }, 8);
          },
        });
      },
    });
  },

  _finish_submit: function (submitted_name, pos_invoice_name) {
    mobile_payments.pos._submitted_name = submitted_name;

    // Flag: suppress any TimestampMismatchError for the next few seconds.
    // POS Awesome's Vue reactivity may try to save/submit the same (now stale)
    // doc right after us, causing a "Document has been modified" error dialog.
    mobile_payments.pos._just_submitted = true;
    setTimeout(function () { mobile_payments.pos._just_submitted = false; }, 8000);

    // Update transaction log with final Sales Invoice name (for fields)
    var txn = mobile_payments.pos._last_transaction;
    if (txn && txn.transaction_log) {
      frappe.call({
        method: "mobile_payments.api.pos.link_invoice_to_transaction",
        args: {
          transaction_log: txn.transaction_log,
          invoice_name: submitted_name,
        },
      });
    }

    frappe.show_alert({
      message: __("Invoice submitted \u2713"),
      indicator: "green",
    }, 4);

    mobile_payments.pos._re_enable_buttons();

    // Tell POS Awesome to start a fresh invoice so it stops touching the old doc
    mobile_payments.pos._make_new_pos_invoice();

    // Full state reset so next transaction's popup can trigger
    mobile_payments.pos._full_reset();
  },

  /**
   * Tell POS Awesome to start a new invoice, so it stops touching the
   * just-submitted doc. Finds the Vue store or component method and calls it.
   */
  _make_new_pos_invoice: function () {
    try {
      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app'], [data-app], .v-application, #app"
      );

      // ── 1. Vue 2 path ──
      for (var i = 0; i < roots.length; i++) {
        var vue = roots[i].__vue__;
        if (!vue) continue;

        // Vuex action / mutation
        if (vue.$store) {
          if (typeof vue.$store._actions.new_invoice !== "undefined") {
            vue.$store.dispatch("new_invoice");
            console.log("mobile_payments: dispatched new_invoice action (Vue2)");
            return;
          }
          if (typeof vue.$store._mutations.SET_INVOICE_DOC !== "undefined") {
            vue.$store.commit("SET_INVOICE_DOC", null);
          }
        }

        // Direct method
        if (typeof vue.new_invoice === "function") { vue.new_invoice(); return; }
        if (typeof vue.create_new_invoice === "function") { vue.create_new_invoice(); return; }

        // Check children
        if (vue.$children) {
          for (var j = 0; j < vue.$children.length; j++) {
            var child = vue.$children[j];
            if (typeof child.new_invoice === "function") { child.new_invoice(); return; }
            if (typeof child.create_new_invoice === "function") { child.create_new_invoice(); return; }
          }
        }
      }

      // ── 2. Vue 3 path (__vue_app__ / Pinia / Vuex4) ──
      for (var k = 0; k < roots.length; k++) {
        var el = roots[k];
        var app = el.__vue_app__;
        if (!app) continue;

        // Try Vuex 4 (Vue 3 compatible Vuex)
        try {
          var vuexStore = app.config.globalProperties.$store;
          if (vuexStore) {
            if (vuexStore._actions && vuexStore._actions.new_invoice) {
              vuexStore.dispatch("new_invoice");
              console.log("mobile_payments: dispatched new_invoice via Vue3 Vuex");
              return;
            }
            if (vuexStore._mutations && vuexStore._mutations.SET_INVOICE_DOC) {
              vuexStore.commit("SET_INVOICE_DOC", null);
              console.log("mobile_payments: committed SET_INVOICE_DOC=null via Vue3 Vuex");
              return;
            }
          }
        } catch (e) {}

        // Try Pinia stores
        try {
          var pinia = app.config.globalProperties.$pinia;
          if (pinia && pinia._s) {
            pinia._s.forEach(function (store) {
              if (typeof store.new_invoice === "function") {
                store.new_invoice();
                console.log("mobile_payments: called new_invoice on Pinia store");
                return;
              }
              if (typeof store.create_new_invoice === "function") {
                store.create_new_invoice();
                console.log("mobile_payments: called create_new_invoice on Pinia store");
                return;
              }
            });
          }
        } catch (e) {}

        // Try Vue 3 root component instance
        try {
          var inst = app._instance;
          if (inst && inst.proxy) {
            var proxy = inst.proxy;
            if (typeof proxy.new_invoice === "function") { proxy.new_invoice(); return; }
            if (typeof proxy.create_new_invoice === "function") { proxy.create_new_invoice(); return; }
          }
        } catch (e) {}
      }

      // ── 3. Walk all elements for __vueParentComponent (Vue 3 SFC) ──
      try {
        var allEls = document.querySelectorAll("*");
        for (var m = 0; m < allEls.length; m++) {
          var comp = allEls[m].__vueParentComponent;
          if (!comp) continue;
          var p = comp.proxy || comp.exposed || {};
          if (typeof p.new_invoice === "function") {
            p.new_invoice();
            console.log("mobile_payments: called new_invoice via Vue3 component proxy");
            return;
          }
          if (typeof p.create_new_invoice === "function") {
            p.create_new_invoice();
            console.log("mobile_payments: called create_new_invoice via Vue3 component proxy");
            return;
          }
        }
      } catch (e) {}

      console.log("mobile_payments: could not find POS new_invoice method — state reset will handle it");
    } catch (e) {
      console.warn("mobile_payments: _make_new_pos_invoice error", e);
    }
  },

  _re_enable_buttons: function () {
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").replace(/\s+/g, " ").trim().toUpperCase();
      if (label.indexOf("SUBMIT") !== -1) btn.disabled = false;
    });
  },

  // Get the full POS invoice doc object from Vue store (Vue 2 + Vue 3)
  _get_pos_doc: function () {
    try {
      var allEls = document.querySelectorAll("*");

      // ── 1. Vue 2 path (legacy) ──
      for (var i = 0; i < allEls.length; i++) {
        var vue = allEls[i].__vue__;
        if (!vue) continue;
        if (vue.$store && vue.$store.state && vue.$store.state.invoice_doc) {
          var doc = vue.$store.state.invoice_doc;
          if (doc && doc.items && doc.items.length > 0) {
            console.log("mobile_payments: found invoice_doc via Vue2 $store");
            return doc;
          }
        }
        if (vue.invoice_doc && vue.invoice_doc.items && vue.invoice_doc.items.length > 0) {
          return vue.invoice_doc;
        }
        if (vue.invoiceDoc && vue.invoiceDoc.items && vue.invoiceDoc.items.length > 0) {
          return vue.invoiceDoc;
        }
      }

      // ── 2. Vue 3 path (__vue_app__ / __vueParentComponent) ──
      for (var j = 0; j < allEls.length; j++) {
        var el = allEls[j];

        // Vue 3 app instance
        var app = el.__vue_app__;
        if (app) {
          // Try Pinia stores (registered on app)
          try {
            var pinia = app.config.globalProperties.$pinia;
            if (pinia && pinia._s) {
              pinia._s.forEach(function (store) {
                // noop — we check below
              });
              var foundDoc = null;
              pinia._s.forEach(function (store) {
                if (foundDoc) return;
                var sd = store.invoice_doc || store.invoiceDoc;
                if (sd && sd.name && sd.items && sd.items.length > 0) {
                  foundDoc = sd;
                }
              });
              if (foundDoc) {
                console.log("mobile_payments: found invoice_doc via Vue3 Pinia store");
                return foundDoc;
              }
            }
          } catch (e) {}

          // Try Vuex on Vue 3
          try {
            var vuexStore = app.config.globalProperties.$store;
            if (vuexStore && vuexStore.state && vuexStore.state.invoice_doc) {
              var vd = vuexStore.state.invoice_doc;
              if (vd && vd.name && vd.items && vd.items.length > 0) {
                console.log("mobile_payments: found invoice_doc via Vue3 Vuex store");
                return vd;
              }
            }
          } catch (e) {}
        }

        // Vue 3 component instance
        var comp = el.__vueParentComponent;
        if (comp) {
          try {
            // Check component's exposed/setup state
            var proxy = comp.proxy || comp.exposed || {};
            var candidates = [
              proxy.invoice_doc, proxy.invoiceDoc,
              proxy.pos_invoice, proxy.posInvoice,
            ];
            for (var c = 0; c < candidates.length; c++) {
              if (candidates[c] && candidates[c].name && candidates[c].items && candidates[c].items.length > 0) {
                console.log("mobile_payments: found invoice_doc via Vue3 component proxy");
                return candidates[c];
              }
            }

            // Check component's setupState (Composition API)
            var ss = comp.setupState || {};
            candidates = [ss.invoice_doc, ss.invoiceDoc, ss.pos_invoice, ss.posInvoice];
            for (var s = 0; s < candidates.length; s++) {
              if (candidates[s] && candidates[s].name && candidates[s].items && candidates[s].items.length > 0) {
                console.log("mobile_payments: found invoice_doc via Vue3 setupState");
                return candidates[s];
              }
            }

            // Check component's data
            var data = comp.data || {};
            candidates = [data.invoice_doc, data.invoiceDoc];
            for (var dd = 0; dd < candidates.length; dd++) {
              if (candidates[dd] && candidates[dd].name && candidates[dd].items && candidates[dd].items.length > 0) {
                console.log("mobile_payments: found invoice_doc via Vue3 component data");
                return candidates[dd];
              }
            }
          } catch (e) {}
        }
      }

      // ── 3. Global POS Awesome references ──
      try {
        if (window.posawesome && window.posawesome.store) {
          var ws = window.posawesome.store;
          var wd = ws.invoice_doc || ws.invoiceDoc;
          if (wd && wd.name) {
            console.log("mobile_payments: found invoice_doc via window.posawesome");
            return wd;
          }
        }
      } catch (e) {}

    } catch (e) {
      console.error("mobile_payments: _get_pos_doc error", e);
    }
    console.warn("mobile_payments: could not find invoice_doc in Vue store — will use server fallback");
    return null;
  },

  // Last-resort: physically click POS Awesome's SUBMIT button
  _click_submit_button: function () {
    var submitBtn = null;
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var rect = btn.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      var label = (btn.textContent || "").replace(/\s+/g, " ").trim().toUpperCase();
      if (label === "SUBMIT") submitBtn = btn;
    });
    if (submitBtn) {
      submitBtn.click();
      submitBtn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    } else {
      frappe.show_alert({
        message: __("Payment verified ✓ — please click SUBMIT manually."),
        indicator: "orange",
      }, 8);
    }
  },

  _on_error: function (message) {
    mobile_payments.pos._dialog_open = false;
    mobile_payments.pos._processing = false;
    mobile_payments.pos._verified_mop_label = "";
    mobile_payments.pos._cooldown_until = Date.now() + 2000;
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
      // 1. Standard Frappe form
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && cur_frm.doc.currency)
        return cur_frm.doc.currency;

      // 2. Read from the POS payment panel DOM — find "Currency" label + its value
      //    This is the most reliable source on POS Awesome (visible in the UI)
      var allInputs = document.querySelectorAll(".v-input, .v-select, .form-group");
      for (var a = 0; a < allInputs.length; a++) {
        var container = allInputs[a];
        var lbl = container.querySelector(".v-label, label");
        if (!lbl) continue;
        var lblText = (lbl.textContent || "").trim().toLowerCase();
        if (lblText !== "currency") continue;
        // Found the Currency field — read its value
        var valEl = container.querySelector(".v-select__selection, .v-select__selection-text, input, .value");
        if (valEl) {
          var cv = (valEl.textContent || valEl.value || "").trim().toUpperCase();
          if (cv && cv.length >= 2 && cv.length <= 5 && /^[A-Z]{2,5}$/.test(cv)) {
            console.log("mobile_payments.pos: currency from DOM label:", cv);
            return cv;
          }
        }
      }

      // 3. Walk Vue stores (Vue 2 + Vue 3)
      var result = mobile_payments.pos._walk_vue_stores();
      var all = result.stores.concat(result.datas);
      for (var s = 0; s < all.length; s++) {
        var st = all[s];
        if (!st) continue;
        if (typeof st.currency === "string" && st.currency) return st.currency;
        if (st.invoice_doc && st.invoice_doc.currency) return st.invoice_doc.currency;
        if (st.pos_profile && st.pos_profile.currency) return st.pos_profile.currency;
      }

      // 4. Vue 3 — check __vue_app__ directly
      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app'], [data-app], .v-application, #app"
      );
      for (var i = 0; i < roots.length; i++) {
        var app = roots[i].__vue_app__;
        if (!app) continue;
        try {
          var vuexStore = app.config.globalProperties.$store;
          if (vuexStore && vuexStore.state) {
            var vs = vuexStore.state;
            if (vs.currency) return vs.currency;
            if (vs.invoice_doc && vs.invoice_doc.currency) return vs.invoice_doc.currency;
            if (vs.pos_profile && vs.pos_profile.currency) return vs.pos_profile.currency;
          }
        } catch (e) {}
        try {
          var pinia = app.config.globalProperties.$pinia;
          if (pinia && pinia._s) {
            var found = "";
            pinia._s.forEach(function (store) {
              if (found) return;
              if (store.currency) found = store.currency;
              else if (store.invoice_doc && store.invoice_doc.currency) found = store.invoice_doc.currency;
              else if (store.pos_profile && store.pos_profile.currency) found = store.pos_profile.currency;
            });
            if (found) return found;
          }
        } catch (e) {}
      }

      // 5. Look for known currency text in amount displays (e.g., "Sh 10,800.00")
      var amountEls = document.querySelectorAll(
        ".grand-total, .pos-grand-total, [class*='grand-total'], .posa-grand-total, .total-amount, .net-total"
      );
      for (var t = 0; t < amountEls.length; t++) {
        var txt = (amountEls[t].textContent || "").trim();
        if (/^Sh\s/i.test(txt) || /SLSH/i.test(txt)) return "SLSH";
        if (/^\$\s/.test(txt) || /USD/i.test(txt)) return "USD";
      }

      // 6. Check the payment panel rows — they show "Sh 0" or "$ 0" prefix
      var payRows = document.querySelectorAll(".v-row input, .payment-input");
      for (var p = 0; p < payRows.length; p++) {
        var row = payRows[p].closest(".v-row, .payment-row");
        if (row) {
          var rowText = (row.textContent || "").trim();
          if (/\bSh\b/i.test(rowText) || /SLSH/i.test(rowText)) return "SLSH";
        }
      }

    } catch (e) {
      console.log("mobile_payments.pos: _get_currency error:", e);
    }

    return frappe.defaults.get_global_default("currency") || "USD";
  },

  /**
   * Get the POS currency conversion rate (e.g. USD→SLSH = 10800).
   * Checks DOM ("Price List USD to SLSH: 10800"), Vue stores, and frm/doc.
   * Returns 1 if no conversion rate is found.
   */
  _get_conversion_rate: function () {
    try {
      // 1) Search the DOM for "Price List ... to ...: <number>"
      var allEls = document.querySelectorAll(
        ".pos-payment, .payment-container, .pos-awesome-app, .posa-pos-payment, [class*='posa'], [class*='payment'], .v-application"
      );
      for (var i = 0; i < allEls.length; i++) {
        var text = allEls[i].textContent || "";
        var m = text.match(/Price\s+List[^:]*:\s*([\d,]+(?:\.\d+)?)/i);
        if (m) {
          var rate = parseFloat(m[1].replace(/,/g, ""));
          if (rate > 1) {
            console.log("mobile_payments.pos: _get_conversion_rate from DOM:", rate);
            return rate;
          }
        }
      }

      // 2) Broader DOM scan — look for any element with "conversion" text
      var body = document.body ? document.body.textContent || "" : "";
      var m2 = body.match(/Price\s+List[^:]*:\s*([\d,]+(?:\.\d+)?)/i);
      if (m2) {
        var r2 = parseFloat(m2[1].replace(/,/g, ""));
        if (r2 > 1) {
          console.log("mobile_payments.pos: _get_conversion_rate from body:", r2);
          return r2;
        }
      }

      // 3) Vue store — look for conversion_rate in store state
      var storeData = mobile_payments.pos._walk_vue_stores();
      if (storeData) {
        var stores = storeData.stores || [];
        var datas = storeData.datas || [];
        var all = stores.concat(datas);
        for (var j = 0; j < all.length; j++) {
          var obj = all[j];
          if (!obj) continue;
          // Check common paths
          var cr = obj.conversion_rate || (obj.pos_profile && obj.pos_profile.conversion_rate)
                || (obj.invoice_doc && obj.invoice_doc.conversion_rate)
                || (obj.doc && obj.doc.conversion_rate);
          if (cr && cr > 1) {
            console.log("mobile_payments.pos: _get_conversion_rate from Vue store:", cr);
            return cr;
          }
        }
      }

      // 4) Walk Vue component tree for conversion_rate in component data/props
      var appEl = document.querySelector("[data-app]") || document.querySelector("#app")
                || document.querySelector("#pos-awesome-root") || document.querySelector(".v-application");
      if (appEl) {
        var queue = [appEl];
        var visited = 0;
        while (queue.length && visited < 1000) {
          var el = queue.shift();
          visited++;
          var comp = el.__vueParentComponent || el.__vue__;
          if (comp) {
            var d = (comp.data && typeof comp.data === "function" ? null : comp.data)
                  || (comp.setupState) || (comp.proxy) || (comp._data);
            if (d && d.conversion_rate && d.conversion_rate > 1) {
              console.log("mobile_payments.pos: _get_conversion_rate from Vue component:", d.conversion_rate);
              return d.conversion_rate;
            }
            if (d && d.pos_profile && d.pos_profile.conversion_rate > 1) {
              console.log("mobile_payments.pos: _get_conversion_rate from Vue pos_profile:", d.pos_profile.conversion_rate);
              return d.pos_profile.conversion_rate;
            }
          }
          var children = el.children || [];
          for (var c = 0; c < children.length; c++) queue.push(children[c]);
        }
      }

    } catch (e) {
      console.log("mobile_payments.pos: _get_conversion_rate error:", e);
    }

    console.log("mobile_payments.pos: _get_conversion_rate — no rate found, using 1");
    return 1;
  },

  /** Walk ALL Vue instances on the page and collect stores + component data. */
  _walk_vue_stores: function () {
    var stores = [];
    var datas  = [];
    try {
      function _getVue(el) {
        if (!el) return null;
        if (el.__vue__) return el.__vue__;
        if (el.__vue_app__) {
          var app = el.__vue_app__;
          if (app.config && app.config.globalProperties && app.config.globalProperties.$store) {
            return { $store: app.config.globalProperties.$store };
          }
          if (app._instance && app._instance.proxy) return app._instance.proxy;
        }
        return null;
      }

      var roots = [
        document.querySelector("[data-app]"),
        document.querySelector("#app"),
        document.querySelector("#pos-awesome-root"),
        document.querySelector(".pos-awesome-app"),
        document.querySelector("[class*='posa']"),
        document.querySelector(".v-application"),
      ];
      for (var r = 0; r < roots.length; r++) {
        if (!roots[r]) continue;
        var vue = _getVue(roots[r]);
        if (!vue) continue;
        if (vue.$store && vue.$store.state && stores.indexOf(vue.$store.state) === -1) {
          stores.push(vue.$store.state);
        }
        if (vue._data && datas.indexOf(vue._data) === -1) {
          datas.push(vue._data);
        }
        var queue = vue.$children ? vue.$children.slice() : [];
        var depth = 0;
        while (queue.length && depth < 500) {
          var child = queue.shift();
          depth++;
          if (child.$store && child.$store.state && stores.indexOf(child.$store.state) === -1) {
            stores.push(child.$store.state);
          }
          if (child._data && datas.indexOf(child._data) === -1) {
            datas.push(child._data);
          }
          if (child.$children) {
            for (var c = 0; c < child.$children.length; c++) queue.push(child.$children[c]);
          }
        }
      }
    } catch (e) {
      console.log("mobile_payments.pos: _walk_vue_stores error:", e);
    }
    return { stores: stores, datas: datas };
  },

  /** Try to get customer phone directly from POS Awesome's Vue store or DOM. */
  _get_customer_phone_from_vue: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc) {
        var doc = cur_frm.doc;
        if (doc.contact_mobile) return doc.contact_mobile;
        if (doc.contact_phone) return doc.contact_phone;
      }

      var result = mobile_payments.pos._walk_vue_stores();
      var all = result.stores.concat(result.datas);
      for (var s = 0; s < all.length; s++) {
        var st = all[s];
        if (!st) continue;
        if (st.contact_mobile) return st.contact_mobile;
        if (st.mobile_no) return st.mobile_no;
        var ci = st.customer_info;
        if (ci && typeof ci === "object") {
          if (ci.mobile_no) return ci.mobile_no;
          if (ci.phone) return ci.phone;
          if (ci.contact_mobile) return ci.contact_mobile;
        }
        var cust = st.customer;
        if (cust && typeof cust === "object") {
          if (cust.mobile_no) return cust.mobile_no;
          if (cust.phone) return cust.phone;
          if (cust.contact_mobile) return cust.contact_mobile;
        }
        var inv = st.invoice_doc;
        if (inv && typeof inv === "object") {
          if (inv.contact_mobile) return inv.contact_mobile;
          if (inv.contact_phone) return inv.contact_phone;
        }
      }

      var allText = document.body.innerText || "";
      var mobileMatch = allText.match(/Mobile\s*No[:\s]+(\d{6,15})/i);
      if (mobileMatch) return mobileMatch[1];
    } catch (e) {
      console.log("mobile_payments.pos: vue phone extract error:", e);
    }
    return "";
  },

  _get_customer: function () {
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc)
        if (cur_frm.doc.customer || cur_frm.doc.customer_name)
          return cur_frm.doc.customer || cur_frm.doc.customer_name;

      // Walk Vue stores for customer name (Vue 2 + Vue 3)
      var result = mobile_payments.pos._walk_vue_stores();
      var all = result.stores.concat(result.datas);
      for (var s = 0; s < all.length; s++) {
        var st = all[s];
        if (!st) continue;
        if (typeof st.customer === "string" && st.customer) return st.customer;
        if (typeof st.customer_name === "string" && st.customer_name) return st.customer_name;
        if (st.customer_info && typeof st.customer_info === "object") {
          if (st.customer_info.customer_name) return st.customer_info.customer_name;
          if (st.customer_info.name) return st.customer_info.name;
        }
        if (st.invoice_doc && st.invoice_doc.customer) return st.invoice_doc.customer;
      }

      // DOM selectors — POS Awesome shows customer name in various places
      var selectors = [
        ".customer-name", ".pos-customer-name", ".posa-customer",
        "[class*='customer'] strong",
        ".v-toolbar__title",
        ".customer-field .value",
      ];
      for (var k = 0; k < selectors.length; k++) {
        var el = document.querySelector(selectors[k]);
        if (el) {
          var text = (el.textContent || "").trim();
          if (text && text.length > 1 && !/^(customer|select|search|pos)/i.test(text)) return text;
        }
      }

      // Last resort: look for "Customer" label followed by a name in the DOM
      var labels = document.querySelectorAll(".v-label, label, .label");
      for (var l = 0; l < labels.length; l++) {
        if ((labels[l].textContent || "").trim().toLowerCase() === "customer") {
          var parent = labels[l].closest(".v-input, .form-group, .field");
          if (parent) {
            var val = parent.querySelector("input, .v-select__selection, .value");
            if (val) {
              var vt = (val.textContent || val.value || "").trim();
              if (vt && vt.length > 1) return vt;
            }
          }
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
    // 1. Standard Frappe form
    try {
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && cur_frm.doc.name)
        return cur_frm.doc.name;
    } catch (e) {}

    // 2. POS Awesome Vue store — draft invoice name
    try {
      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app']"
      );
      for (var i = 0; i < roots.length; i++) {
        var vue = roots[i].__vue__;
        if (!vue) continue;
        // Check $store state
        if (vue.$store && vue.$store.state) {
          var st = vue.$store.state;
          if (st.invoice_doc && st.invoice_doc.name) return st.invoice_doc.name;
          if (st.invoice_name) return st.invoice_name;
        }
        // Check direct props
        if (vue.invoice_doc && vue.invoice_doc.name) return vue.invoice_doc.name;
        if (vue.invoice_name) return vue.invoice_name;
        // Check children
        if (vue.$children) {
          for (var j = 0; j < vue.$children.length; j++) {
            var child = vue.$children[j];
            if (child.invoice_doc && child.invoice_doc.name) return child.invoice_doc.name;
            if (child.invoice_name) return child.invoice_name;
            if (child.$store && child.$store.state) {
              var cs = child.$store.state;
              if (cs.invoice_doc && cs.invoice_doc.name) return cs.invoice_doc.name;
            }
          }
        }
      }
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

/**
 * Global interceptor: suppress the "Document has been modified after you
 * have opened it" error that POS Awesome throws right after our custom
 * submit. The submit already succeeded; this stale-doc error is harmless
 * but blocks the entire POS page.
 */
(function () {
  var _origMsgprint = frappe.msgprint;
  frappe.msgprint = function (msg) {
    // Check if this is the TimestampMismatchError while we just submitted
    if (mobile_payments && mobile_payments.pos && mobile_payments.pos._just_submitted) {
      var text = "";
      if (typeof msg === "string") text = msg;
      else if (msg && msg.message) text = msg.message;
      else if (msg && msg.title) text = msg.title;

      if (text && text.indexOf("Document has been modified") !== -1) {
        console.log("mobile_payments: suppressed TimestampMismatchError after successful submit");
        // Dismiss any existing error dialogs from this error
        try {
          $(".msgprint-dialog .btn-modal-close, .modal.show .btn-modal-close").trigger("click");
        } catch (e) {}
        return;
      }
    }
    return _origMsgprint.apply(frappe, arguments);
  };

  // Also intercept frappe.throw which is used for some error paths
  var _origThrow = frappe.throw;
  if (_origThrow) {
    frappe.throw = function (msg) {
      if (mobile_payments && mobile_payments.pos && mobile_payments.pos._just_submitted) {
        var text = typeof msg === "string" ? msg : (msg && msg.message) || "";
        if (text && text.indexOf("Document has been modified") !== -1) {
          console.log("mobile_payments: suppressed frappe.throw TimestampMismatchError");
          return;
        }
      }
      return _origThrow.apply(frappe, arguments);
    };
  }

  // Intercept server error responses that trigger the error dialog
  $(document).ajaxError(function (event, jqXHR, ajaxSettings, thrownError) {
    if (!mobile_payments || !mobile_payments.pos || !mobile_payments.pos._just_submitted) return;

    try {
      var responseText = jqXHR.responseText || "";
      if (responseText.indexOf("TimestampMismatchError") !== -1 ||
          responseText.indexOf("Document has been modified") !== -1) {
        console.log("mobile_payments: caught server TimestampMismatchError via ajaxError — suppressing");

        // Close any error dialog that may have popped up
        setTimeout(function () {
          $(".msgprint-dialog:visible, .modal.show").each(function () {
            var content = $(this).text() || "";
            if (content.indexOf("Document has been modified") !== -1) {
              $(this).find(".btn-modal-close, .close").trigger("click");
              $(this).modal("hide");
            }
          });
        }, 200);
      }
    } catch (e) {}
  });
})();

$(document).ready(function () {
  if (!window.mobile_payments) window.mobile_payments = {};
  if (!window.mobile_payments.pos || typeof window.mobile_payments.pos.init !== "function") {
    console.warn("mobile_payments.pos not ready at document.ready — will retry");
    return;
  }
  if (mobile_payments.pos._is_pos_page()) mobile_payments.pos.init();
  [2000, 5000].forEach(function (ms) {
    setTimeout(function () {
      if (mobile_payments.pos && mobile_payments.pos._is_pos_page && mobile_payments.pos._is_pos_page() && !mobile_payments.pos._initialized)
        mobile_payments.pos.init();
    }, ms);
  });
});

var _mp_nav_handler = function () {
  setTimeout(function () {
    if (!window.mobile_payments) window.mobile_payments = {};
    if (!window.mobile_payments.pos || typeof window.mobile_payments.pos.init !== "function") return;

    if (mobile_payments.pos._is_pos_page()) {
      mobile_payments.pos._initialized      = false;
      mobile_payments.pos._watching         = false;
      mobile_payments.pos._submit_guard_on  = false;
      mobile_payments.pos._payment_verified = false;
      mobile_payments.pos._verified_mop_label = "";
      mobile_payments.pos._dialog_open      = false;
      mobile_payments.pos._processing       = false;
      mobile_payments.pos._cooldown_until   = 0;
      mobile_payments.pos._just_submitted   = false;
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
    if (!mobile_payments || !mobile_payments.pos) return;
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
    mobile_payments.pos._full_reset();
  },
});

frappe.ui.form.on("Sales Invoice", {
  on_submit: function (frm) {
    if (!frm.doc.is_pos) return;
    if (!mobile_payments || !mobile_payments.pos) return;
    var tx = mobile_payments.pos._last_transaction;
    if (!tx || !tx.transaction_log) return;
    frappe.call({
      method: "mobile_payments.api.pos.link_pos_invoice",
      args: { transaction_log: tx.transaction_log, invoice_name: frm.doc.name },
    });
    mobile_payments.pos._full_reset();
  },
});

function shouldCommitPreauth(mpTxn) {
    // Frontend no longer commits preauth. Let POS Awesome/backend submit flow handle it.
    return false;
}

async function finalizePaidPosInvoice({ invoice_name, provider_txn_id, mp_txn }) {
    // Skip frontend commit entirely.
    return await frappe.call({
        method: "mobile_payments.api.pos.submit_and_print_pos_invoice",
        args: { invoice_name }
    });
}

const __mpCommitLocks = new Set();

async function commitAndFinalizeOnce({ invoice_name, provider_txn_id }) {
    // No-op commit guard to avoid duplicate/invalid commit attempts from frontend.
    return { ok: true, skipped: true, reason: "frontend_commit_disabled" };
}

// ...existing code...
