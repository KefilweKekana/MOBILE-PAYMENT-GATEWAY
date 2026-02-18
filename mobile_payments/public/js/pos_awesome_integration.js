/**
 * POS Awesome Integration for Mobile Payments
 *
 * Injects mobile money payment methods (WaafiPay: ZAAD/SAHAL/EVCPlus, Edahab)
 * into POS Awesome's payment dialog.
 *
 * POS Awesome uses Vue.js and has an event bus for component communication.
 * This script hooks into the POS events to add the mobile payment flow.
 */

frappe.provide("mobile_payments.pos");

mobile_payments.pos = {
  /**
   * Cached available methods
   */
  _methods: null,
  _enabled: false,
  _initialized: false,

  /**
   * Detect whether the current page is any variant of POS.
   */
  _is_pos_page: function () {
    let posPattern = /\/(pos-awesome|point-of-sale|pos-system|pos-closing-entry|pos(?:[\/\#\?]|$))/;
    if (posPattern.test(window.location.pathname) || posPattern.test(window.location.hash)) return true;
    if (document.querySelector(".pos-awesome-app, #pos-awesome-root, .pay-btn, .summary-btn, .pos-container")) return true;
    try { if (posPattern.test(frappe.get_route_str())) return true; } catch(e) {}
    return false;
  },

  /**
   * Initialize POS Awesome integration.
   * Called when POS Awesome loads.
   */
  init: function () {
    if (mobile_payments.pos._initialized) return;
    mobile_payments.pos._initialized = true;

    // Load available methods from backend
    frappe.call({
      method: "mobile_payments.api.pos.get_mobile_payment_methods",
      async: false,
      callback: function (r) {
        if (r.message) {
          mobile_payments.pos._enabled = r.message.enabled;
          mobile_payments.pos._methods = r.message.methods || [];
        }
      },
    });

    if (!mobile_payments.pos._enabled || !mobile_payments.pos._methods.length) {
      return;
    }

    // Hook into POS Awesome events
    mobile_payments.pos._hook_pos_awesome();
  },

  /**
   * Hook into POS Awesome's payment workflow.
   * POS Awesome uses a custom event bus and Vue components.
   */
  _hook_pos_awesome: function () {
    // Method 1: Extend the POS payment dialog via frappe.ui.form events
    // POS Awesome triggers events when the payment dialog opens
    $(document).on("pos-awesome-payment-open", function (e, data) {
      mobile_payments.pos._inject_mobile_payment_option(data);
    });

    // Method 2: Watch for POS Awesome payment component mount
    // Use MutationObserver to detect when POS payment section loads
    mobile_payments.pos._watch_pos_payment_section();

    // Method 3: Override POS payment methods list
    // POS Awesome reads payment methods from POS Profile
    // Our Modes of Payment (ZAAD, SAHAL, etc.) should appear automatically
    // if added to the POS Profile. This script adds the payment handler.
    mobile_payments.pos._register_payment_handler();
  },

  /**
   * Watch for POS payment section to load using MutationObserver.
   * Supports both POS Awesome (classic) and Vuetify-based POS System.
   */
  _watch_pos_payment_section: function () {
    // Also try to inject immediately if POS is already loaded
    mobile_payments.pos._try_inject_now();

    let observer = new MutationObserver(function (mutations) {
      for (let mutation of mutations) {
        for (let node of mutation.addedNodes) {
          if (node.nodeType !== 1) continue;

          // ── Vuetify POS System (pay-btn, summary-btn) ──
          let payBtn = node.querySelector && node.querySelector(".pay-btn, .summary-btn");
          if (!payBtn && node.classList) {
            payBtn = (node.classList.contains("pay-btn") || node.classList.contains("summary-btn")) ? node : null;
          }
          if (payBtn) {
            setTimeout(function () {
              mobile_payments.pos._add_mobile_money_button_vuetify(payBtn);
            }, 500);
            continue;
          }

          // ── Classic POS Awesome (payment-container, pos-payment) ──
          let paymentSection =
            node.querySelector && node.querySelector(".payment-container, .pos-payment, .payment-methods");

          if (!paymentSection) {
            paymentSection = node.classList && (
              node.classList.contains("payment-container") ||
              node.classList.contains("pos-payment") ||
              node.classList.contains("payment-methods")
            ) ? node : null;
          }

          if (paymentSection) {
            setTimeout(function () {
              mobile_payments.pos._add_mobile_payment_button(paymentSection);
            }, 300);
          }
        }
      }
    });

    observer.observe(document.body, {
      childList: true,
      subtree: true,
    });

    // Periodic check as fallback — Vuetify POS may render after observer attaches
    let retries = 0;
    let interval = setInterval(function () {
      retries++;
      if (document.querySelector(".mobile-payment-pos-btn") || retries > 30) {
        clearInterval(interval);
        return;
      }
      mobile_payments.pos._try_inject_now();
    }, 1000);
  },

  /**
   * Try to inject the Mobile Money button immediately if POS is already rendered.
   */
  _try_inject_now: function () {
    // Already injected?
    if (document.querySelector(".mobile-payment-pos-btn")) return;

    // Vuetify POS: look for .pay-btn
    let payBtn = document.querySelector(".pay-btn");
    if (payBtn) {
      mobile_payments.pos._add_mobile_money_button_vuetify(payBtn);
      return;
    }

    // Classic POS Awesome
    let section = document.querySelector(".payment-container, .pos-payment, .payment-methods");
    if (section) {
      mobile_payments.pos._add_mobile_payment_button(section);
    }
  },

  /**
   * Add "Mobile Money" button for Vuetify-based POS System.
   * Inserts a button next to the PAY button.
   */
  _add_mobile_money_button_vuetify: function (payBtn) {
    // Don't add if already exists
    if (document.querySelector(".mobile-payment-pos-btn")) return;

    let methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) return;

    // Create a Vuetify-styled Mobile Money button
    let btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mobile-payment-pos-btn v-btn v-btn--block v-btn--elevated v-theme--dark v-btn--density-default v-btn--size-large v-btn--variant-elevated";
    btn.style.cssText = "background-color: #2ecc71 !important; color: white !important; margin-bottom: 8px; border: none; display: flex; align-items: center; justify-content: center; min-height: 44px; border-radius: 4px; font-weight: 600; font-size: 14px; cursor: pointer; width: 100%;";
    btn.innerHTML = `
      <span class="v-btn__content" style="display:flex; align-items:center; gap:8px;">
        <i class="fa fa-mobile" style="font-size:20px;"></i>
        <span>MOBILE MONEY</span>
      </span>
    `;

    // Hover effect
    btn.addEventListener("mouseenter", function() {
      this.style.setProperty("background-color", "#27ae60", "important");
      this.style.opacity = "0.9";
    });
    btn.addEventListener("mouseleave", function() {
      this.style.setProperty("background-color", "#2ecc71", "important");
      this.style.opacity = "1";
    });

    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      mobile_payments.pos.show_pos_payment_dialog();
    });

    // Insert BEFORE the PAY button's parent
    let parentContainer = payBtn.parentElement;
    if (parentContainer) {
      parentContainer.insertBefore(btn, payBtn);
    }
  },

  /**
   * Add a "Mobile Payment" button to the POS payment section.
   */
  _add_mobile_payment_button: function (container) {
    // Don't add if already exists
    if (container.querySelector(".mobile-payment-pos-btn")) return;

    let methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) return;

    // Create the mobile payment button
    let btn = document.createElement("div");
    btn.className = "mobile-payment-pos-btn payment-mode-wrapper";
    btn.innerHTML = `
      <div class="mobile-payment-mode" style="
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 12px 16px;
        margin: 6px;
        border: 2px solid #2ecc71;
        border-radius: 8px;
        cursor: pointer;
        background: linear-gradient(135deg, #f0fff4 0%, #ffffff 100%);
        transition: all 0.2s ease;
        min-height: 50px;
      ">
        <i class="fa fa-mobile" style="font-size:24px; color:#2ecc71; margin-right:10px;"></i>
        <span style="font-weight:600; font-size:14px; color:#333;">
          ${__("Mobile Money")}
        </span>
      </div>
    `;

    // Hover effects
    let modeDiv = btn.querySelector(".mobile-payment-mode");
    modeDiv.addEventListener("mouseenter", function() {
      this.style.background = "linear-gradient(135deg, #d4edda 0%, #f0fff4 100%)";
      this.style.borderColor = "#27ae60";
    });
    modeDiv.addEventListener("mouseleave", function() {
      this.style.background = "linear-gradient(135deg, #f0fff4 0%, #ffffff 100%)";
      this.style.borderColor = "#2ecc71";
    });

    btn.addEventListener("click", function () {
      mobile_payments.pos.show_pos_payment_dialog();
    });

    // Insert the button - try different container structures
    let methodsContainer = container.querySelector(".payment-modes, .mode-of-payment");
    if (methodsContainer) {
      methodsContainer.appendChild(btn);
    } else {
      container.appendChild(btn);
    }
  },

  /**
   * Inject mobile payment into POS Awesome's payment dialog data.
   */
  _inject_mobile_payment_option: function (data) {
    if (!data || !data.payment_methods) return;

    // Add Mobile Money as a payment option
    data.payment_methods.push({
      mode_of_payment: "Mobile Money",
      amount: 0,
      is_mobile_payment: true,
    });
  },

  /**
   * Register a payment handler for when mobile payment modes are selected
   * in POS Awesome natively (i.e., ZAAD/SAHAL etc. are in the POS Profile).
   */
  _register_payment_handler: function () {
    // Listen for POS payment mode selection events
    frappe.realtime.on("pos_payment_method_selected", function (data) {
      let mobile_methods = ["ZAAD", "SAHAL", "EVCPlus", "Edahab", "WaafiPay"];
      if (data && data.mode_of_payment && mobile_methods.includes(data.mode_of_payment)) {
        mobile_payments.pos.show_pos_payment_dialog(data.mode_of_payment, data.amount);
      }
    });

    // Also intercept via custom event that POS Awesome may trigger
    $(document).on("pos-payment-mode-click", function (e, mode, amount) {
      let mobile_methods = ["ZAAD", "SAHAL", "EVCPlus", "Edahab", "WaafiPay"];
      if (mobile_methods.includes(mode)) {
        e.preventDefault();
        e.stopPropagation();
        mobile_payments.pos.show_pos_payment_dialog(mode, amount);
      }
    });
  },

  /**
   * Show the POS mobile payment dialog.
   * This is the main entry point for POS mobile payments.
   *
   * @param {string} preselected_method - Optional pre-selected payment method
   * @param {number} amount - Optional amount (from POS numpad)
   */
  show_pos_payment_dialog: function (preselected_method, amount) {
    let methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) {
      frappe.show_alert({
        message: __("No mobile payment methods available"),
        indicator: "orange",
      });
      return;
    }

    // Try to get amount from POS if not passed
    if (!amount) {
      amount = mobile_payments.pos._get_pos_outstanding_amount();
    }

    // Build method options
    let method_options = methods.map(function (m) {
      return {
        label: m.label + " (" + m.provider + ")",
        value: m.provider + "|" + m.method,
      };
    });

    // Find pre-selected if applicable
    let default_method = method_options[0]?.value;
    if (preselected_method) {
      let match = method_options.find(function (o) {
        return o.value.includes(preselected_method);
      });
      if (match) default_method = match.value;
    }

    // Get customer info from POS
    let customer_info = mobile_payments.pos._get_pos_customer();

    let d = new frappe.ui.Dialog({
      title: __("Mobile Money Payment - POS"),
      size: "small",
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "pos_payment_header",
          options: `
            <div style="text-align:center; margin-bottom:15px; padding:10px; background:#f8f9fa; border-radius:8px;">
              <i class="fa fa-mobile fa-3x" style="color:#2ecc71;"></i>
              <h4 style="margin-top:8px;">${__("POS Mobile Payment")}</h4>
              ${customer_info.name ?
                '<p class="text-muted">' + __("Customer") + ": <strong>" + customer_info.name + "</strong></p>" : ""}
            </div>
          `,
        },
        {
          fieldname: "payment_method",
          fieldtype: "Select",
          label: __("Payment Method"),
          options: method_options.map(function (o) { return o.value; }).join("\n"),
          reqd: 1,
          default: default_method,
        },
        {
          fieldname: "amount",
          fieldtype: "Currency",
          label: __("Amount"),
          reqd: 1,
          default: amount || 0,
        },
        {
          fieldname: "phone_number",
          fieldtype: "Data",
          label: __("Phone Number"),
          description: __("Customer's mobile wallet number (e.g., 252612345678)"),
          reqd: 1,
          default: customer_info.phone || "",
        },
      ],
      primary_action_label: __("Process Payment"),
      primary_action: function (values) {
        d.hide();

        let parts = values.payment_method.split("|");
        let provider = parts[0];
        let method = parts[1];

        mobile_payments.pos._process_pos_payment(
          provider,
          method,
          values.phone_number,
          values.amount,
          customer_info
        );
      },
    });

    d.show();
  },

  /**
   * Process the POS mobile payment.
   */
  _process_pos_payment: function (provider, method, phone, amount, customer_info) {
    // Show processing dialog
    let processing = new frappe.ui.Dialog({
      title: __("Processing Payment"),
      fields: [
        {
          fieldtype: "HTML",
          fieldname: "processing_html",
          options: `
            <div style="text-align:center; padding:30px;">
              <div class="mobile-payment-spinner">
                <i class="fa fa-spinner fa-pulse fa-3x" style="color:#3498db;"></i>
              </div>
              <h4 style="margin-top:15px;">${provider} - ${method}</h4>
              <p>${__("Sending payment request...")}</p>
              <p class="text-muted" id="pos-payment-status">
                ${__("A prompt will be sent to")} ${phone}
              </p>
            </div>
          `,
        },
      ],
      static: true,
    });
    processing.show();
    processing.$wrapper.find(".modal-footer").hide();

    // Get POS Profile
    let pos_profile = mobile_payments.pos._get_pos_profile();

    frappe.call({
      method: "mobile_payments.api.pos.initiate_pos_payment",
      args: {
        provider: provider,
        method: method,
        phone: phone,
        amount: amount,
        pos_profile: pos_profile,
        customer: customer_info.name || "",
        invoice_name: mobile_payments.pos._get_current_invoice_name() || "",
      },
      callback: function (r) {
        if (r.message) {
          let result = r.message;

          if (result.success) {
            processing.hide();
            mobile_payments.pos._handle_pos_success(result, amount, provider, method);
          } else if (result.pending) {
            // Start polling
            mobile_payments.pos._poll_pos_payment(
              result.transaction_log,
              processing,
              amount,
              provider,
              method
            );
          } else {
            processing.hide();
            mobile_payments.pos._handle_pos_error(result.message || __("Payment failed"));
          }
        }
      },
      error: function () {
        processing.hide();
        mobile_payments.pos._handle_pos_error(__("An error occurred while processing the payment"));
      },
    });
  },

  /**
   * Poll for POS payment status.
   */
  _poll_pos_payment: function (transaction_log, dialog, amount, provider, method) {
    let poll_count = 0;
    let max_polls = 60;
    let poll_interval = 2000;

    // Show cancel button
    dialog.$wrapper.find(".modal-footer").show();
    dialog.set_primary_action(__("Cancel"), function () {
      clearInterval(poller);
      dialog.hide();
      frappe.show_alert({
        message: __("Payment cancelled"),
        indicator: "orange",
      });
    });

    let poller = setInterval(function () {
      poll_count++;

      // Update status text
      let status_el = dialog.$wrapper.find("#pos-payment-status");
      if (status_el.length) {
        status_el.html(__("Waiting for confirmation... ({0}s)", [poll_count * 2]));
      }

      if (poll_count > max_polls) {
        clearInterval(poller);
        dialog.hide();
        mobile_payments.pos._handle_pos_timeout(transaction_log);
        return;
      }

      frappe.call({
        method: "mobile_payments.api.pos.check_pos_payment_status",
        args: { transaction_log: transaction_log },
        async: true,
        callback: function (r) {
          if (r.message) {
            if (r.message.status === "Completed") {
              clearInterval(poller);
              dialog.hide();
              mobile_payments.pos._handle_pos_success(
                r.message,
                amount,
                provider,
                method
              );
            } else if (r.message.status === "Failed" || r.message.status === "Cancelled") {
              clearInterval(poller);
              dialog.hide();
              mobile_payments.pos._handle_pos_error(
                r.message.error_message || __("Payment {0}", [r.message.status.toLowerCase()])
              );
            }
          }
        },
      });
    }, poll_interval);
  },

  /**
   * Handle successful POS payment.
   */
  _handle_pos_success: function (result, amount, provider, method) {
    frappe.show_alert({
      message: __("Payment of {0} via {1} successful!", [
        format_currency(amount),
        method,
      ]),
      indicator: "green",
    }, 7);

    // Store transaction reference for linking after POS invoice submission
    mobile_payments.pos._last_transaction = {
      transaction_log: result.transaction_log,
      transaction_id: result.transaction_id || result.provider_transaction_id,
      provider: provider,
      method: method,
      amount: amount,
    };

    // Try to update POS Awesome's payment state
    mobile_payments.pos._update_pos_payment_state(amount, method, result);

    // Show success indicator in POS
    let successHTML = `
      <div class="mobile-payment-pos-success" style="
        position:fixed; top:60px; right:20px; z-index:9999;
        background:#d4edda; color:#155724; padding:15px 25px;
        border-radius:8px; border:1px solid #c3e6cb;
        box-shadow:0 4px 12px rgba(0,0,0,0.15);
        animation: slideInRight 0.3s ease;
      ">
        <i class="fa fa-check-circle" style="margin-right:8px;"></i>
        <strong>${method}</strong>: ${format_currency(amount)} ${__("paid")}
        ${result.provider_transaction_id ? '<br><small>Ref: ' + result.provider_transaction_id + '</small>' : ''}
      </div>
    `;

    let el = $(successHTML).appendTo("body");
    setTimeout(function () {
      el.fadeOut(500, function () { el.remove(); });
    }, 5000);
  },

  /**
   * Update POS Awesome's internal payment state after successful mobile payment.
   */
  _update_pos_payment_state: function (amount, method, result) {
    // Try to trigger POS Awesome's payment completion
    // Method 1: Trigger custom event
    $(document).trigger("mobile-payment-completed", {
      amount: amount,
      mode_of_payment: method,
      transaction_id: result.transaction_id || result.provider_transaction_id,
      transaction_log: result.transaction_log,
    });

    // Method 2: Try to find and update POS Awesome Vue component
    try {
      // POS Awesome stores state in a Vuex-like store or component data
      let posApp = document.querySelector("#pos-awesome-root, .pos-awesome-app, [data-pos-app]");
      if (posApp && posApp.__vue__) {
        let vue = posApp.__vue__;

        // Try to access payment state
        if (vue.$store) {
          vue.$store.commit("addPayment", {
            mode_of_payment: method,
            amount: amount,
            reference_no: result.transaction_id || result.provider_transaction_id,
          });
        } else if (vue.payments !== undefined) {
          vue.payments.push({
            mode_of_payment: method,
            amount: amount,
          });
        }
      }
    } catch (e) {
      // Vue integration not available, payment will be linked after submission
      console.log("POS Awesome Vue integration not accessible, using fallback");
    }

    // Method 3: Set values in the current form if available
    if (cur_frm && cur_frm.doc.doctype === "POS Invoice") {
      // Add payment row
      let payment_row = cur_frm.add_child("payments");
      payment_row.mode_of_payment = method;
      payment_row.amount = amount;
      cur_frm.refresh_field("payments");
    }
  },

  /**
   * Handle POS payment error.
   */
  _handle_pos_error: function (message) {
    frappe.show_alert({
      message: message,
      indicator: "red",
    }, 7);

    frappe.msgprint({
      title: __("Payment Failed"),
      indicator: "red",
      message: `
        <div style="text-align:center; padding:15px;">
          <i class="fa fa-times-circle fa-3x" style="color:#e74c3c; margin-bottom:10px;"></i>
          <p>${message}</p>
          <p class="text-muted">${__("Please try again or use another payment method.")}</p>
        </div>
      `,
    });
  },

  /**
   * Handle POS payment timeout.
   */
  _handle_pos_timeout: function (transaction_log) {
    frappe.msgprint({
      title: __("Payment Timeout"),
      indicator: "orange",
      message: `
        <div style="text-align:center; padding:15px;">
          <i class="fa fa-clock-o fa-3x" style="color:#f39c12; margin-bottom:10px;"></i>
          <h4>${__("Payment Timed Out")}</h4>
          <p>${__("The payment confirmation was not received in time.")}</p>
          <p class="text-muted">${__("The system will continue checking. You can also retry.")}</p>
        </div>
      `,
    });
  },

  // ─── Helper Methods ──────────────────────────────────────────

  /**
   * Get the outstanding amount from the current POS session.
   */
  _get_pos_outstanding_amount: function () {
    // Try multiple ways to get the sales total
    try {
      // From POS Awesome Vue state
      let posApp = document.querySelector("#pos-awesome-root, .pos-awesome-app, [data-pos-app]");
      if (posApp && posApp.__vue__) {
        let vue = posApp.__vue__;
        if (vue.grand_total !== undefined) return flt(vue.grand_total);
        if (vue.$store && vue.$store.state.grand_total) return flt(vue.$store.state.grand_total);
      }

      // From current form (Sales Invoice / POS Invoice)
      if (cur_frm && cur_frm.doc) {
        if (cur_frm.doc.grand_total) return flt(cur_frm.doc.grand_total);
        if (cur_frm.doc.rounded_total) return flt(cur_frm.doc.rounded_total);
        if (cur_frm.doc.outstanding_amount) return flt(cur_frm.doc.outstanding_amount);
      }

      // From Vuetify POS DOM — look for total display elements
      // Common selectors: text with currency, total labels, summary sections
      let totalSelectors = [
        ".grand-total .value",
        ".pos-grand-total",
        "[data-grand-total]",
        ".total-amount",
        ".summary-total",
        ".v-card .text-h5",
        ".v-card .text-h4",
        ".v-card .text-h6",
      ];
      for (let sel of totalSelectors) {
        let el = document.querySelector(sel);
        if (el) {
          let val = flt(el.textContent.replace(/[^0-9.,]/g, "").replace(",", ""));
          if (val > 0) return val;
        }
      }

      // Broader search: find elements containing "Total" label near a number
      let allText = document.querySelectorAll(".v-list-item, .v-row, .summary-row, tr");
      for (let row of allText) {
        let text = row.textContent || "";
        if (/grand\s*total|net\s*total|total\s*amount/i.test(text)) {
          let val = flt(text.replace(/[^0-9.,]/g, "").replace(",", ""));
          if (val > 0) return val;
        }
      }
    } catch (e) {
      console.log("Could not detect POS amount:", e);
    }

    return 0;
  },

  /**
   * Get the current POS customer info.
   * Fetches phone from the customer's primary Contact mobile number.
   */
  _get_pos_customer: function () {
    let info = { name: "", phone: "" };

    try {
      // From POS Awesome Vue state
      let posApp = document.querySelector("#pos-awesome-root, .pos-awesome-app, [data-pos-app]");
      if (posApp && posApp.__vue__) {
        let vue = posApp.__vue__;
        if (vue.customer) info.name = vue.customer;
        if (vue.$store && vue.$store.state.customer) info.name = vue.$store.state.customer;
      }

      // From current form
      if (!info.name && cur_frm && cur_frm.doc) {
        info.name = cur_frm.doc.customer || cur_frm.doc.customer_name || "";
      }

      // Fetch phone from primary Contact linked to Customer
      if (info.name) {
        // Step 1: Get primary contact's mobile_no from Contact via Dynamic Link
        frappe.call({
          method: "frappe.client.get_list",
          args: {
            doctype: "Contact",
            filters: [
              ["Dynamic Link", "link_doctype", "=", "Customer"],
              ["Dynamic Link", "link_name", "=", info.name],
            ],
            fields: ["name", "mobile_no", "phone", "is_primary_contact"],
            order_by: "is_primary_contact desc",
            limit_page_length: 5,
          },
          async: false,
          callback: function (r) {
            if (r.message && r.message.length) {
              // Prefer primary contact, otherwise first contact
              for (let contact of r.message) {
                if (contact.mobile_no) {
                  info.phone = contact.mobile_no;
                  break;
                }
                if (contact.phone && !info.phone) {
                  info.phone = contact.phone;
                }
              }
            }
          },
        });

        // Fallback: try Customer doctype mobile_no if no contact found
        if (!info.phone) {
          frappe.call({
            method: "frappe.client.get_value",
            args: {
              doctype: "Customer",
              filters: { name: info.name },
              fieldname: "mobile_no",
            },
            async: false,
            callback: function (r) {
              if (r.message && r.message.mobile_no) {
                info.phone = r.message.mobile_no;
              }
            },
          });
        }
      }
    } catch (e) {
      console.log("Could not detect POS customer:", e);
    }

    return info;
  },

  /**
   * Get the current POS Profile name.
   */
  _get_pos_profile: function () {
    try {
      if (cur_frm && cur_frm.doc && cur_frm.doc.pos_profile) {
        return cur_frm.doc.pos_profile;
      }

      let posApp = document.querySelector("#pos-awesome-root, .pos-awesome-app, [data-pos-app]");
      if (posApp && posApp.__vue__) {
        let vue = posApp.__vue__;
        if (vue.pos_profile) return vue.pos_profile;
        if (vue.$store && vue.$store.state.pos_profile) return vue.$store.state.pos_profile;
      }
    } catch (e) {
      // Ignore
    }

    return "";
  },

  /**
   * Get the current invoice name if available.
   */
  _get_current_invoice_name: function () {
    try {
      if (cur_frm && cur_frm.doc && cur_frm.doc.name) {
        return cur_frm.doc.name;
      }
    } catch (e) {
      // Ignore
    }
    return "";
  },
};

// ─── Auto-Initialize on POS Page Load ──────────────────────────

$(document).ready(function () {
  if (mobile_payments.pos._is_pos_page()) {
    mobile_payments.pos.init();
  }
});

// Also initialize when route changes (SPA navigation)
if (frappe.router && frappe.router.on) {
  frappe.router.on("change", function () {
    setTimeout(function () {
      if (mobile_payments.pos._is_pos_page()) {
        mobile_payments.pos._initialized = false; // allow re-init on navigation
        mobile_payments.pos.init();
      }
    }, 500);
  });
} else {
  $(window).on("hashchange", function () {
    setTimeout(function () {
      if (mobile_payments.pos._is_pos_page()) {
        mobile_payments.pos._initialized = false;
        mobile_payments.pos.init();
      }
    }, 500);
  });
}

/**
 * Hook into POS Invoice submission to link the transaction.
 */
frappe.ui.form.on("POS Invoice", {
  on_submit: function (frm) {
    // Check if there's a pending mobile payment transaction to link
    if (mobile_payments.pos._last_transaction) {
      let tx = mobile_payments.pos._last_transaction;

      frappe.call({
        method: "mobile_payments.api.pos.link_pos_invoice",
        args: {
          transaction_log: tx.transaction_log,
          invoice_name: frm.doc.name,
        },
        callback: function (r) {
          if (r.message && r.message.success) {
            frappe.show_alert({
              message: __("Mobile payment linked to invoice {0}", [frm.doc.name]),
              indicator: "green",
            });
          }
        },
      });

      // Clear the reference
      mobile_payments.pos._last_transaction = null;
    }
  },
});

/**
 * Also hook into Sales Invoice for POS-originated invoices.
 * POS Awesome can create Sales Invoices directly in some configurations.
 */
frappe.ui.form.on("Sales Invoice", {
  on_submit: function (frm) {
    if (frm.doc.is_pos && mobile_payments.pos._last_transaction) {
      let tx = mobile_payments.pos._last_transaction;

      frappe.call({
        method: "mobile_payments.api.pos.link_pos_invoice",
        args: {
          transaction_log: tx.transaction_log,
          invoice_name: frm.doc.name,
        },
      });

      mobile_payments.pos._last_transaction = null;
    }
  },
});
