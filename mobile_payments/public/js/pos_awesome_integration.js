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
  _invoice_total:     0,       // total invoice amount
  _mobile_paid_amount: 0,      // amount paid via mobile (may be partial)
  _hide_submit_interval: null, // interval ID for enforcing submit button hiding
  _print_window:      null,    // pre-opened window ref for print (avoids popup blocker)

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

    var _prev_mobile_amount = 0;
    var _prev_mobile_label  = "";

    setInterval(function () {
      if (!mobile_payments.pos._enabled) return;
      if (!mobile_payments.pos._payment_panel_open()) {
        _prev_mobile_amount = 0;
        _prev_mobile_label  = "";
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
        && (_prev_mobile_amount === 0 || mobileLabel !== _prev_mobile_label)
      );

      _prev_mobile_amount = mobileAmount;
      _prev_mobile_label  = mobileLabel;

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
    // Fallback: use tracked paid amount if POS rows don't reflect it yet
    if (mobileAmount <= 0 && mobile_payments.pos._mobile_paid_amount > 0) {
      mobileAmount = mobile_payments.pos._mobile_paid_amount;
    }

    var needsVerification = mobileAmount > 0 && !mobile_payments.pos._payment_verified;
    var alreadyVerified   = mobileAmount > 0 && mobile_payments.pos._payment_verified;

    // Check if this was a partial payment — if so, cashier needs SUBMIT visible
    var invoiceTotal = mobile_payments.pos._invoice_total || 0;
    var mobilePaid   = mobile_payments.pos._mobile_paid_amount || 0;
    var isPartial    = alreadyVerified && mobilePaid > 0 && mobilePaid < invoiceTotal && (invoiceTotal - mobilePaid) > 0.005;

    if (alreadyVerified && !isPartial) {
      // Full payment verified — start interval enforcer that keeps hiding
      // submit buttons even if Vue re-renders the DOM
      mobile_payments.pos._hide_submit_now();
      mobile_payments.pos._start_hide_enforcer();
    } else if (alreadyVerified && isPartial) {
      // Partial payment — stop enforcer, keep SUBMIT visible
      mobile_payments.pos._stop_hide_enforcer();
      mobile_payments.pos._style_submit_buttons("partial");
    } else if (needsVerification) {
      mobile_payments.pos._stop_hide_enforcer();
      mobile_payments.pos._style_submit_buttons("needs-verify");
    } else {
      mobile_payments.pos._stop_hide_enforcer();
      mobile_payments.pos._style_submit_buttons("normal");
    }
  },

  /** Immediately hide all SUBMIT buttons in the DOM */
  _hide_submit_now: function () {
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").trim().toUpperCase();
      if (label.indexOf("SUBMIT") === -1) return;
      if (btn.getBoundingClientRect().width === 0) return;
      btn.style.display = "none";
      btn.style.pointerEvents = "none";
      btn.disabled = true;
      btn.title = __("Payment already submitted via mobile payment");
    });
  },

  /** Start a periodic enforcer that keeps submit buttons hidden (survives Vue re-renders) */
  _start_hide_enforcer: function () {
    // Don't start if already running
    if (mobile_payments.pos._hide_submit_interval) return;
    mobile_payments.pos._hide_submit_interval = setInterval(function () {
      // Stop enforcing once payment_verified is reset (after submit completes)
      if (!mobile_payments.pos._payment_verified) {
        mobile_payments.pos._stop_hide_enforcer();
        return;
      }
      mobile_payments.pos._hide_submit_now();
    }, 400);
  },

  /** Stop the periodic hide enforcer */
  _stop_hide_enforcer: function () {
    if (mobile_payments.pos._hide_submit_interval) {
      clearInterval(mobile_payments.pos._hide_submit_interval);
      mobile_payments.pos._hide_submit_interval = null;
    }
  },

  /** Apply styling to submit buttons for partial / needs-verify / normal states */
  _style_submit_buttons: function (mode) {
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").trim().toUpperCase();
      if (label.indexOf("SUBMIT") === -1) return;
      if (btn.getBoundingClientRect().width === 0) return;

      if (mode === "partial") {
        btn.style.display = "";
        btn.style.pointerEvents = "";
        btn.disabled = false;
        btn.style.opacity = "";
        btn.title = __("Mobile payment verified — collect remaining and submit");
      } else if (mode === "needs-verify") {
        btn.style.display = "";
        btn.style.pointerEvents = "";
        btn.disabled = false;
        btn.style.opacity = "0.45";
        btn.title = __("Complete mobile payment verification first");
      } else {
        btn.style.display = "";
        btn.style.pointerEvents = "";
        btn.disabled = false;
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
    mobile_payments.pos._invoice_total = 0;
    mobile_payments.pos._mobile_paid_amount = 0;
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

    console.log("mobile_payments.pos: dialog context — customer:", customer, "invoice:", invoice, "currency:", currency, "amount:", amount);

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
          fieldtype: "Currency",
          label: __("Amount"),
          reqd: 1,
          default: amount,
          read_only: 0,
          description: __("You can reduce this for partial payment — remaining balance stays for cash or another method."),
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

        var pay_amount = flt(values.amount);
        if (pay_amount <= 0) {
          frappe.show_alert({ message: __("Amount must be greater than zero"), indicator: "red" });
          return;
        }
        if (pay_amount > flt(amount)) {
          frappe.show_alert({ message: __("Amount cannot exceed {0}", [flt(amount).toFixed(2)]), indicator: "red" });
          return;
        }

        // Track the total invoice amount and how much is being paid via mobile
        mobile_payments.pos._invoice_total = flt(amount);
        mobile_payments.pos._mobile_paid_amount = pay_amount;

        // Mark as processing BEFORE hiding — so close handler doesn't reset
        mobile_payments.pos._processing = true;
        d.hide();

        mobile_payments.pos._update_submit_buttons();

        var parts      = values.payment_method.split("|");
        var provider   = parts[0];
        var method     = parts[1];
        var acct_type  = values.account_type || "Subscriber (Mobile Wallet)";
        mobile_payments.pos._process(provider, method, phone_clean, pay_amount, currency, invoice, customer, acct_type);
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

    // Debug: dump what the Vue walker found
    var _debug = mobile_payments.pos._walk_vue_stores();
    console.log("mobile_payments.pos: Vue stores found:", _debug.stores.length, "datas:", _debug.datas.length);
    _debug.stores.forEach(function (st, idx) {
      console.log("  store[" + idx + "] keys:", Object.keys(st).join(", "));
      if (st.customer) console.log("    .customer:", st.customer);
      if (st.customer_info) console.log("    .customer_info:", JSON.stringify(st.customer_info).substring(0, 200));
      if (st.invoice_doc) console.log("    .invoice_doc.customer:", st.invoice_doc.customer, "contact_mobile:", st.invoice_doc.contact_mobile);
    });

    // Auto-fill phone: try Vue store first (instant), then API fallback
    var vue_phone = mobile_payments.pos._get_customer_phone_from_vue();
    console.log("mobile_payments.pos: phone auto-fill — vue_phone:", vue_phone, "customer:", customer);
    if (vue_phone) {
      d.set_value("phone_number", vue_phone);
    } else if (customer) {
      frappe.call({
        method: "mobile_payments.api.pos.get_customer_phone",
        args: { customer: customer },
        callback: function (r) {
          console.log("mobile_payments.pos: phone API result:", r.message);
          if (r.message && r.message.phone) {
            var current = d.get_value("phone_number");
            if (!current) d.set_value("phone_number", r.message.phone);
          }
        },
      });
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

    var invoiceTotal = mobile_payments.pos._invoice_total || flt(amount);
    var paidAmount   = flt(amount);
    var isPartial    = paidAmount < invoiceTotal && (invoiceTotal - paidAmount) > 0.005;
    var remaining    = invoiceTotal - paidAmount;

    if (isPartial) {
      // Partial payment: set the mobile MOP to the paid amount and Cash to the remainder
      mobile_payments.pos._set_partial_mop_amounts(paidAmount, remaining);
      mobile_payments.pos._show_partial_success_dialog(paidAmount, remaining, method);
    } else {
      // Full payment: normal flow
      mobile_payments.pos._update_submit_buttons();
      mobile_payments.pos._show_success_dialog(amount, method);
    }
  },

  /** Set MOP amounts for partial payment: mobile MOP = paid, Cash = remaining */
  _set_partial_mop_amounts: function (paidAmount, remaining) {
    var verifiedLabel = mobile_payments.pos._verified_mop_label || "";
    var foundMobile = false;
    var foundCash   = false;

    mobile_payments.pos._get_payment_rows().forEach(function (r) {
      var isMobileMop = verifiedLabel && r.label.toUpperCase().indexOf(verifiedLabel) !== -1;
      if (!foundMobile && isMobileMop) {
        mobile_payments.pos._set_input(r.inp, paidAmount.toFixed(2));
        foundMobile = true;
        console.log("mobile_payments.pos: partial — setting", r.label, "=", paidAmount.toFixed(2));
      } else if (!foundCash && /cash/i.test(r.label)) {
        mobile_payments.pos._set_input(r.inp, remaining.toFixed(2));
        foundCash = true;
        console.log("mobile_payments.pos: partial — setting", r.label, "=", remaining.toFixed(2));
      } else {
        mobile_payments.pos._set_input(r.inp, "0");
      }
    });

    // Fallback: if mobile MOP not found, use first mobile MOP
    if (!foundMobile) {
      mobile_payments.pos._get_payment_rows().forEach(function (r) {
        if (!foundMobile && mobile_payments.pos._is_mobile_mop(r.label)) {
          mobile_payments.pos._set_input(r.inp, paidAmount.toFixed(2));
          foundMobile = true;
        }
      });
    }
  },

  /** Show success dialog for partial payment — no auto-submit, instructs cashier to collect rest */
  _show_partial_success_dialog: function (paidAmount, remaining, method) {
    var currency = mobile_payments.pos._get_currency();

    var d = new frappe.ui.Dialog({
      title: __("Partial Payment Received"),
      static: true,
      fields: [{
        fieldtype: "HTML",
        fieldname: "partial_html",
        options:
          '<div style="text-align:center; padding: 24px 16px 8px;">'

          // ── Green/orange tick ──
          + '<div style="'
          +   'width:72px; height:72px; border-radius:50%;'
          +   'background:#fff3cd; margin:0 auto 16px;'
          +   'display:flex; align-items:center; justify-content:center;">'
          +   '<i class="fa fa-check" style="font-size:36px; color:#ffc107;"></i>'
          + '</div>'

          // ── Paid amount ──
          + '<div style="font-size:22px; font-weight:700; color:#28a745; margin-bottom:4px;">'
          +   parseFloat(paidAmount).toFixed(2) + " " + currency + " " + __("paid")
          + '</div>'

          // ── Method label ──
          + '<div style="font-size:14px; color:#6c757d; margin-bottom:12px;">'
          +   __("via") + " " + (method || __("Mobile Payment"))
          + '</div>'

          // ── Remaining amount ──
          + '<div style="'
          +   'background:#fff3cd; border-radius:8px; padding:12px; margin-bottom:12px;">'
          +   '<div style="font-size:18px; font-weight:700; color:#856404;">'
          +     parseFloat(remaining).toFixed(2) + " " + currency + " " + __("remaining")
          +   '</div>'
          +   '<div style="font-size:13px; color:#856404; margin-top:4px;">'
          +     __("Collect the remaining amount via Cash or another payment method, then submit.")
          +   '</div>'
          + '</div>'

          + '</div>',
      }],

      primary_action_label: __("OK — Continue to Collect Remaining"),
      primary_action: function () {
        d.hide();
        // Don't auto-submit — cashier needs to collect rest and click SUBMIT themselves
        mobile_payments.pos._update_submit_buttons();
        frappe.show_alert({
          message: __("Collect {0} {1} remaining, then click SUBMIT & PRINT.", [remaining.toFixed(2), currency]),
          indicator: "orange",
        }, 8);
      },
    });

    // Style primary button orange
    d.$wrapper.find(".btn-primary").css({
      "background-color": "#ffc107",
      "border-color":     "#ffc107",
      "color":            "#212529",
      "font-size":        "15px",
      "padding":          "8px 28px",
    });

    // Hide the X close button
    d.$wrapper.find(".btn-modal-close, .modal-header .close").hide();

    d.show();
  },

  _show_success_dialog: function (amount, method) {
    var currency  = mobile_payments.pos._get_currency();
    var countdown = 2;   // seconds before auto-submit

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

          // ── Countdown bar ──
          + '<div style="margin-bottom:12px;">'
          +   '<div style="'
          +     'background:#e9ecef; border-radius:4px; height:6px; overflow:hidden;'
          +     'margin-bottom:6px;">'
          +     '<div id="mp-countdown-bar" style="'
          +       'height:100%; width:100%;'
          +       'background:#28a745;'
          +       'transition: width 1s linear;"></div>'
          +   '</div>'
          +   '<div id="mp-countdown-text" style="font-size:12px; color:#6c757d;">'
          +     __("Auto-submitting in {0}s…", [countdown])
          +   '</div>'
          + '</div>'

          + '</div>',
      }],
    });

    // Hide ALL dialog buttons and close button — fully automatic
    d.$wrapper.find(".modal-footer").hide();
    d.$wrapper.find(".btn-modal-close, .modal-header .close").hide();

    d.show();

    // ── Countdown ticker ──
    var remaining = countdown;
    d._mp_timer = setInterval(function () {
      remaining--;

      // Update bar width
      var pct = (remaining / countdown) * 100;
      var bar = d.$wrapper.find("#mp-countdown-bar");
      if (bar.length) bar.css("width", pct + "%");

      // Update text
      var txt = d.$wrapper.find("#mp-countdown-text");
      if (remaining > 0) {
        if (txt.length) txt.text(__("Auto-submitting in {0}s…", [remaining]));
      } else {
        clearInterval(d._mp_timer);
        if (txt.length) txt.text(__("Submitting…"));
        d.hide();
        setTimeout(function () {
          mobile_payments.pos._delegate_submit_print();
        }, 1500);
      }
    }, 1000);
  },

  /**
   * Submit the POS invoice directly via API, then open the print view in a new tab.
   * This completely bypasses POS Awesome's button click — avoiding any
   * TimestampMismatchError caused by Vue auto-saves on the draft invoice.
   */
  _delegate_submit_print: function () {
    if (!mobile_payments.pos._payment_verified) return;

    // MUTEX: if already submitting, wait 2s and check again once
    if (mobile_payments.pos._submitting) {
      setTimeout(function () {
        // By now the first submit should be done — if invoice already submitted,
        // just open print. If still submitting, give up silently.
        if (mobile_payments.pos._submitted_name) {
          mobile_payments.pos._open_print(mobile_payments.pos._submitted_name);
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
      mobile_payments.pos._submitting = false;
      mobile_payments.pos._re_enable_buttons();
      frappe.show_alert({
        message: __("Payment verified ✓ — please click SUBMIT & PRINT manually."),
        indicator: "orange",
      }, 8);
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
            message: __("Payment verified ✓ — please click SUBMIT & PRINT manually."),
            indicator: "orange",
          }, 8);
          return;
        }

        // If already submitted, just open print
        if (fresh.docstatus === 1) {
          mobile_payments.pos._finish_submit(fresh.name, invoice_name);
          return;
        }

        // Fix payment mode: replace "Cash" with the actual mobile MOP (e.g. ZAAD, Edahab)
        var mopLabel   = mobile_payments.pos._verified_mop_label || "";
        var mobilePaid = mobile_payments.pos._mobile_paid_amount || 0;
        var invTotal   = mobile_payments.pos._invoice_total || 0;
        var isPartial  = mobilePaid > 0 && mobilePaid < invTotal && (invTotal - mobilePaid) > 0.005;
        var remaining  = isPartial ? (invTotal - mobilePaid) : 0;

        if (mopLabel && fresh.payments && fresh.payments.length) {
          var correctMop = mobile_payments.pos._find_mop_name(mopLabel);
          var hasMobileMop = false;
          for (var p = 0; p < fresh.payments.length; p++) {
            if ((fresh.payments[p].mode_of_payment || "").toUpperCase() === mopLabel) {
              hasMobileMop = true;
              if (isPartial) fresh.payments[p].amount = mobilePaid;
              break;
            }
          }

          if (!hasMobileMop && correctMop) {
            if (isPartial) {
              // Partial: set Cash to remaining amount and add a new entry for mobile MOP
              // or split the Cash entry
              var cashSplit = false;
              for (var p2 = 0; p2 < fresh.payments.length; p2++) {
                var pay = fresh.payments[p2];
                if ((pay.mode_of_payment || "").toUpperCase() === "CASH" && pay.amount > 0) {
                  console.log("mobile_payments.pos: partial — splitting Cash:", pay.amount, "→ Cash:", remaining.toFixed(2), correctMop + ":", mobilePaid.toFixed(2));
                  pay.amount = remaining;
                  // Add mobile MOP entry
                  fresh.payments.push({
                    mode_of_payment: correctMop,
                    amount: mobilePaid,
                    base_amount: mobilePaid,
                    type: "Phone",
                    default: 0,
                  });
                  cashSplit = true;
                  break;
                }
              }
              // If no cash entry, just change the first non-zero entry
              if (!cashSplit) {
                for (var p3 = 0; p3 < fresh.payments.length; p3++) {
                  if (fresh.payments[p3].amount > 0) {
                    fresh.payments[p3].mode_of_payment = correctMop;
                    fresh.payments[p3].amount = mobilePaid;
                    break;
                  }
                }
              }
            } else {
              // Full payment: replace Cash with mobile MOP
              for (var p4 = 0; p4 < fresh.payments.length; p4++) {
                var pay2 = fresh.payments[p4];
                if (pay2.amount > 0 && (pay2.mode_of_payment || "").toUpperCase() === "CASH") {
                  console.log("mobile_payments.pos: fixing payment mode:", pay2.mode_of_payment, "→", correctMop);
                  pay2.mode_of_payment = correctMop;
                  break;
                }
              }
            }
          }
        }

        frappe.call({
          method: "frappe.client.submit",
          args: { doc: fresh },
          callback: function (r2) {
            if (r2 && r2.exc) {
              // Already submitted? Try to print anyway
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
              message: __("Submit failed — please click SUBMIT & PRINT manually."),
              indicator: "orange",
            }, 8);
          },
        });
      },
    });
  },

  _finish_submit: function (submitted_name, pos_invoice_name) {
    mobile_payments.pos._submitted_name = submitted_name;

    // Stop the hide enforcer now — submit is done
    mobile_payments.pos._stop_hide_enforcer();

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

    mobile_payments.pos._open_print(submitted_name);

    mobile_payments.pos._payment_verified    = false;
    mobile_payments.pos._verified_mop_label  = "";
    mobile_payments.pos._last_transaction    = null;
    mobile_payments.pos._submitting          = false;
    mobile_payments.pos._invoice_total       = 0;
    mobile_payments.pos._mobile_paid_amount  = 0;
    mobile_payments.pos._re_enable_buttons();
  },

  _open_print: function (invoice_name) {
    // Detect the correct doctype — POS Awesome uses "POS Invoice"
    var doctype = "POS Invoice";
    if (invoice_name && invoice_name.indexOf("SINV") !== -1) {
      doctype = "Sales Invoice";
    }

    var print_url = "/printview?doctype=" + encodeURIComponent(doctype)
      + "&name=" + encodeURIComponent(invoice_name)
      + "&trigger_print=1&format=undefined&no_letterhead=0";

    // Try the pre-opened window first (avoids popup blocker)
    var win = mobile_payments.pos._print_window;
    mobile_payments.pos._print_window = null;  // consume it

    if (win && !win.closed) {
      try {
        win.location.href = print_url;
        frappe.show_alert({
          message: __("Invoice submitted ✓ — print view opened"),
          indicator: "green",
        }, 4);
        return;
      } catch (e) {
        console.warn("mobile_payments.pos: pre-opened window failed:", e);
      }
    }

    // Fallback: try window.open directly
    var newWin = window.open(print_url, "_blank");
    if (newWin) {
      frappe.show_alert({
        message: __("Invoice submitted ✓ — print view opened"),
        indicator: "green",
      }, 4);
      return;
    }

    // Popup was blocked — show a clickable link so user can open print manually
    console.warn("mobile_payments.pos: popup blocked, showing print link");
    var printDialog = new frappe.ui.Dialog({
      title: __("Invoice Submitted ✓"),
      fields: [{
        fieldtype: "HTML",
        fieldname: "print_link_html",
        options:
          '<div style="text-align:center; padding: 20px 16px;">'
          + '<div style="'
          +   'width:56px; height:56px; border-radius:50%;'
          +   'background:#d4edda; margin:0 auto 12px;'
          +   'display:flex; align-items:center; justify-content:center;">'
          +   '<i class="fa fa-check" style="font-size:28px; color:#28a745;"></i>'
          + '</div>'
          + '<p style="font-size:14px; margin-bottom:16px;">'
          +   __("Invoice {0} has been submitted.", [invoice_name])
          + '</p>'
          + '<a href="' + print_url + '" target="_blank" '
          +   'style="display:inline-block; padding:10px 24px; '
          +   'background:#28a745; color:#fff; border-radius:4px; '
          +   'text-decoration:none; font-size:15px; font-weight:600;">'
          +   '<i class="fa fa-print"></i> ' + __("Open Print View")
          + '</a>'
          + '</div>',
      }],
    });
    printDialog.show();
    printDialog.$wrapper.find(".modal-footer").hide();
  },

  _re_enable_buttons: function () {
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var label = (btn.textContent || "").replace(/\s+/g, " ").trim().toUpperCase();
      if (label.indexOf("SUBMIT") !== -1) {
        btn.disabled = false;
        btn.style.display = "";
        btn.style.pointerEvents = "";
        btn.style.opacity = "";
        btn.title = "";
      }
    });
  },

  // Get the full POS invoice doc object from Vue store
  _get_pos_doc: function () {
    try {
      // Walk every DOM element looking for __vue__ with a store
      var allEls = document.querySelectorAll("*");
      for (var i = 0; i < allEls.length; i++) {
        var vue = allEls[i].__vue__;
        if (!vue) continue;
        // Try Vuex store
        if (vue.$store && vue.$store.state && vue.$store.state.invoice_doc) {
          var doc = vue.$store.state.invoice_doc;
          if (doc && doc.items && doc.items.length > 0) {
            console.log("mobile_payments: found invoice_doc via $store on", allEls[i], doc);
            return doc;
          }
        }
        // Try direct prop
        if (vue.invoice_doc && vue.invoice_doc.items && vue.invoice_doc.items.length > 0) {
          console.log("mobile_payments: found invoice_doc directly on vue", allEls[i], vue.invoice_doc);
          return vue.invoice_doc;
        }
        // Try invoiceDoc (camelCase)
        if (vue.invoiceDoc && vue.invoiceDoc.items && vue.invoiceDoc.items.length > 0) {
          console.log("mobile_payments: found invoiceDoc camelCase", allEls[i], vue.invoiceDoc);
          return vue.invoiceDoc;
        }
      }
      // Log what stores we DID find for debugging
      for (var j = 0; j < allEls.length; j++) {
        var v = allEls[j].__vue__;
        if (v && v.$store && v.$store.state) {
          console.log("mobile_payments: found store with keys:", Object.keys(v.$store.state));
          break;
        }
      }
    } catch (e) {
      console.error("mobile_payments: _get_pos_doc error", e);
    }
    console.warn("mobile_payments: could not find invoice_doc in Vue store");
    return null;
  },

  /** Find the exact Mode of Payment name from _methods by uppercase label. */
  _find_mop_name: function (upperLabel) {
    if (!upperLabel) return "";
    var methods = mobile_payments.pos._methods || [];
    for (var i = 0; i < methods.length; i++) {
      if ((methods[i].method || "").toUpperCase() === upperLabel) return methods[i].method;
      if ((methods[i].label || "").toUpperCase() === upperLabel) return methods[i].label;
    }
    // Fallback: title-case the label (e.g. "ZAAD" → "ZAAD", "EDAHAB" → "Edahab")
    return upperLabel.charAt(0).toUpperCase() + upperLabel.slice(1).toLowerCase();
  },

  // Last-resort: physically click POS Awesome's SUBMIT & PRINT button
  _click_submit_button: function () {
    var submitPrintBtn = null;
    var submitBtn = null;
    document.querySelectorAll("button, .v-btn").forEach(function (btn) {
      var rect = btn.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      var label = (btn.textContent || "").replace(/\s+/g, " ").trim().toUpperCase();
      if (label.indexOf("SUBMIT") !== -1 && label.indexOf("PRINT") !== -1) submitPrintBtn = btn;
      else if (label === "SUBMIT") submitBtn = btn;
    });
    var target = submitPrintBtn || submitBtn;
    if (target) {
      target.click();
      target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    } else {
      frappe.show_alert({
        message: __("Payment verified ✓ — please click SUBMIT & PRINT manually."),
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

      // 2. POS Awesome Vue store — multiple paths
      var roots = document.querySelectorAll(
        "#pos-awesome-root, .pos-awesome-app, [id^='posa'], [class*='posa-app']"
      );
      for (var i = 0; i < roots.length; i++) {
        var vue = roots[i].__vue__;
        if (!vue) continue;

        // Direct props
        if (vue.currency) return vue.currency;

        // Vuex store
        if (vue.$store && vue.$store.state) {
          var st = vue.$store.state;
          if (st.currency) return st.currency;
          if (st.invoice_doc && st.invoice_doc.currency) return st.invoice_doc.currency;
          if (st.pos_profile && st.pos_profile.currency) return st.pos_profile.currency;
        }

        // Check children (payment component may have it)
        if (vue.$children) {
          for (var j = 0; j < vue.$children.length; j++) {
            var child = vue.$children[j];
            if (child.currency) return child.currency;
            if (child.invoice_doc && child.invoice_doc.currency) return child.invoice_doc.currency;
            if (child.$store && child.$store.state) {
              var cs = child.$store.state;
              if (cs.currency) return cs.currency;
              if (cs.invoice_doc && cs.invoice_doc.currency) return cs.invoice_doc.currency;
            }
          }
        }
      }

      // 3. Try reading from the payment panel UI (currency symbol/label)
      var currLabel = document.querySelector(".posa-currency, [class*='currency-label'], .pos-currency");
      if (currLabel) {
        var ct = (currLabel.textContent || "").trim().toUpperCase();
        if (ct && ct.length <= 5) return ct;
      }
    } catch (e) {}

    return frappe.defaults.get_global_default("currency") || "USD";
  },

  /** Walk ALL Vue instances on the page and collect stores + component data. */
  _walk_vue_stores: function () {
    var stores = [];
    var datas  = [];
    try {
      // 1. Try known Vuetify / POS Awesome mount points first
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
        var vue = roots[r].__vue__;
        if (!vue) continue;
        if (vue.$store && vue.$store.state && stores.indexOf(vue.$store.state) === -1) {
          stores.push(vue.$store.state);
        }
        if (vue._data && datas.indexOf(vue._data) === -1) {
          datas.push(vue._data);
        }
        // Walk children recursively (up to 3 levels)
        var queue = vue.$children ? vue.$children.slice() : [];
        var depth = 0;
        while (queue.length && depth < 300) {
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
      // 1. cur_frm (standard POS / Sales Invoice form)
      if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc) {
        var doc = cur_frm.doc;
        if (doc.contact_mobile) return doc.contact_mobile;
        if (doc.contact_phone) return doc.contact_phone;
      }

      // 2. Walk Vue stores + component data
      var result = mobile_payments.pos._walk_vue_stores();
      var all = result.stores.concat(result.datas);
      for (var s = 0; s < all.length; s++) {
        var st = all[s];
        if (!st) continue;

        // Direct phone fields
        if (st.contact_mobile) return st.contact_mobile;
        if (st.mobile_no) return st.mobile_no;

        // customer_info object (POS Awesome stores customer details here)
        var ci = st.customer_info;
        if (ci && typeof ci === "object") {
          if (ci.mobile_no) return ci.mobile_no;
          if (ci.phone) return ci.phone;
          if (ci.contact_mobile) return ci.contact_mobile;
        }

        // customer field might be an object with phone
        var cust = st.customer;
        if (cust && typeof cust === "object") {
          if (cust.mobile_no) return cust.mobile_no;
          if (cust.phone) return cust.phone;
          if (cust.contact_mobile) return cust.contact_mobile;
        }

        // invoice_doc may carry contact_mobile
        var inv = st.invoice_doc;
        if (inv && typeof inv === "object") {
          if (inv.contact_mobile) return inv.contact_mobile;
          if (inv.contact_phone) return inv.contact_phone;
        }
      }

      // 3. Scrape "Mobile No:" from the visible POS DOM (last resort)
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

      // Walk Vue stores for customer name
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

      // DOM selectors — POS Awesome shows customer name in the header area
      var selectors = [
        ".customer-name", ".pos-customer-name", ".posa-customer",
        "[class*='customer'] strong",
        ".v-toolbar__title",       // Vuetify toolbar title
        ".customer-field .value",  // POS Awesome customer display
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
          // Get the sibling or parent's next text node
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
    mobile_payments.pos._last_transaction  = null;
    mobile_payments.pos._payment_verified  = false;
    mobile_payments.pos._verified_mop_label = "";
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
    mobile_payments.pos._last_transaction  = null;
    mobile_payments.pos._payment_verified  = false;
    mobile_payments.pos._verified_mop_label = "";
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
