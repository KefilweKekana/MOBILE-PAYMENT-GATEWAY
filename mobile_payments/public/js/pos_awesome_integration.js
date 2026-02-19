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
    let path = (window.location.pathname + window.location.hash).toLowerCase();
    // Match any POS-related route
    if (/pos/.test(path)) return true;
    // Also match by DOM elements (POS Awesome / POS System UI)
    if (document.querySelector(".pos-awesome-app, #pos-awesome-root, .pay-btn, .summary-btn, .pos-container, .point-of-sale-app")) return true;
    try { if (/pos/.test((frappe.get_route_str() || "").toLowerCase())) return true; } catch(e) {}
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
   *
   * Key: the MODE OF PAYMENT row must only be injected when the payment
   * panel is actually open (REC CASH visible).  If we inject too early
   * the row ends up in the items panel.
   */
  _watch_pos_payment_section: function () {
    // ── Hook the PAY button so we re-attempt injection when payment panel opens ──
    mobile_payments.pos._hook_pay_button();

    let observer = new MutationObserver(function (mutations) {
      for (let mutation of mutations) {
        for (let node of mutation.addedNodes) {
          if (node.nodeType !== 1) continue;

          // ── Vuetify POS System: detect payment-panel content appearing ──
          let hasRecCash = false;
          if (node.querySelectorAll) {
            let btns = node.querySelectorAll("button, .v-btn");
            for (let b of btns) {
              if ((b.textContent || "").trim().toUpperCase() === "REC CASH") {
                hasRecCash = true; break;
              }
            }
          }
          if (hasRecCash) {
            setTimeout(function () {
              mobile_payments.pos._add_mobile_money_button_vuetify();
            }, 400);
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
  },

  /**
   * Attach a delegated click listener on the PAY button so that when the
   * payment panel opens we attempt to inject the MODE OF PAYMENT row.
   */
  _hook_pay_button: function () {
    document.addEventListener("click", function (e) {
      let target = e.target.closest(".pay-btn, .summary-btn");
      if (!target) {
        // Also check for a button whose text is "PAY"
        let btn = e.target.closest("button, .v-btn");
        if (btn && (btn.textContent || "").trim().toUpperCase() === "PAY") {
          target = btn;
        }
      }
      if (!target) return;

      // Payment panel will render shortly — retry a few times
      let attempts = 0;
      let iv = setInterval(function () {
        attempts++;
        // Remove any previously misplaced button before re-injecting
        mobile_payments.pos._remove_misplaced_button();
        mobile_payments.pos._add_mobile_money_button_vuetify();
        if (document.querySelector(".mobile-payment-pos-btn") || attempts > 10) {
          clearInterval(iv);
        }
      }, 300);
    });
  },

  /**
   * Remove a MODE OF PAYMENT row that ended up outside the payment panel.
   */
  _remove_misplaced_button: function () {
    let existing = document.querySelector(".mobile-payment-pos-btn");
    if (!existing) return;

    // Check if it is next to a visible REC CASH row.  If not, remove it.
    let row = existing.closest(".mobile-payment-row");
    if (!row) { existing.remove(); return; }

    // Walk siblings to see if a REC CASH row is nearby
    let sibling = row.previousElementSibling;
    let foundRecCash = false;
    while (sibling) {
      if ((sibling.textContent || "").toUpperCase().includes("REC CASH")) {
        foundRecCash = true; break;
      }
      sibling = sibling.previousElementSibling;
    }
    if (!foundRecCash) {
      row.remove();
    }
  },

  /**
   * Try to inject the MODE OF PAYMENT button when the payment panel is open.
   */
  _try_inject_now: function () {
    // Already injected correctly?
    if (document.querySelector(".mobile-payment-pos-btn")) return;

    // Only inject for Vuetify POS when REC CASH is actually visible
    let allBtns = document.querySelectorAll("button, .v-btn");
    for (let b of allBtns) {
      let t = (b.textContent || "").trim().toUpperCase();
      if (t === "REC CASH") {
        let rect = b.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          mobile_payments.pos._add_mobile_money_button_vuetify();
          return;
        }
      }
    }

    // Classic POS Awesome
    let section = document.querySelector(".payment-container, .pos-payment, .payment-methods");
    if (section) {
      mobile_payments.pos._add_mobile_payment_button(section);
    }
  },

  /**
   * Add "MOBILE PAYMENT" as a native-looking payment method row in the
   * Vuetify-based POS System — clones the exact same row structure as
   * REC CASH so it appears right next to it in the payments area.
   */
  _add_mobile_money_button_vuetify: function () {
    // Don't add if already exists
    if (document.querySelector(".mobile-payment-pos-btn")) return;

    let methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) return;

    // ── 1. Find the REC CASH button to locate the payment row ──
    let allButtons = document.querySelectorAll("button, .v-btn");
    let recCashBtn = null;
    let nativeLabels = ["REC CASH", "ZAAD", "SAHAL", "EVCPLUS", "WAAFIPAY", "EDAHAB"];

    for (let btn of allButtons) {
      let text = (btn.textContent || "").trim().toUpperCase();
      for (let label of nativeLabels) {
        if (text === label || text.includes(label)) {
          recCashBtn = btn;
          break;
        }
      }
      if (recCashBtn) break;
    }

    if (!recCashBtn) {
      // Payment panel not open yet — do nothing; _hook_pay_button will
      // re-try once the user opens the payment panel.
      return;
    }

    // Verify REC CASH is actually visible (payment panel is open)
    let recRect = recCashBtn.getBoundingClientRect();
    if (recRect.width === 0 || recRect.height === 0) return;

    // ── 2. Walk up to find the payment v-row (.v-row.payments or parent row) ──
    let paymentRow = recCashBtn.closest(".v-row, [class*='row']");
    if (!paymentRow) {
      // Try going up through the v-col first
      let col = recCashBtn.closest(".v-col, [class*='col']");
      if (col) paymentRow = col.parentElement;
    }

    if (!paymentRow) {
      mobile_payments.pos._create_fallback_row();
      return;
    }

    // ── 3. Clone the entire payment row to get identical structure ──
    let mobileRow = paymentRow.cloneNode(true);
    mobileRow.classList.add("mobile-payment-row");

    // ── 4. Update the button in the cloned row ──
    let clonedBtn = mobileRow.querySelector("button, .v-btn");
    if (clonedBtn) {
      // Keep all native Vuetify classes for consistent sizing
      clonedBtn.className = clonedBtn.className + " mobile-payment-pos-btn";
      // Override background to green
      clonedBtn.style.cssText = `
        background-color: #00d4ff !important;
        color: white !important;
        border: none;
        cursor: pointer;
      `;
      // Replace button text
      let contentSpan = clonedBtn.querySelector(".v-btn__content");
      if (contentSpan) {
        contentSpan.textContent = "MODE OF PAYMENT";
      } else {
        clonedBtn.textContent = "MODE OF PAYMENT";
      }

      // Remove old Vue event listeners by replacing with a clean clone
      let freshBtn = clonedBtn.cloneNode(true);
      freshBtn.classList.add("mobile-payment-pos-btn");
      clonedBtn.parentNode.replaceChild(freshBtn, clonedBtn);

      // Add hover effects
      freshBtn.addEventListener("mouseenter", function () {
        this.style.setProperty("background-color", "#00bce0", "important");
      });
      freshBtn.addEventListener("mouseleave", function () {
        this.style.setProperty("background-color", "#00d4ff", "important");
      });

      // Click → auto-fill with POS total, then open payment dialog
      freshBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        let amountInput = document.querySelector("#mobile-payment-amount-input");
        // Auto-fill with POS outstanding amount when button is clicked
        let posTotal = mobile_payments.pos._get_pos_outstanding_amount();
        if (amountInput && posTotal > 0) {
          amountInput.value = posTotal;
        }
        let amount = amountInput ? parseFloat(amountInput.value) || 0 : 0;
        if (!amount || amount <= 0) {
          frappe.show_alert({
            message: __("Please enter the amount to pay"),
            indicator: "orange",
          });
          if (amountInput) { amountInput.focus(); amountInput.select(); }
          return;
        }
        mobile_payments.pos.show_pos_payment_dialog(null, amount);
      });
    }

    // ── 5. Update the input in the cloned row ──
    let clonedInput = mobileRow.querySelector("input");
    if (clonedInput) {
      // Remove Vue bindings
      let freshInput = clonedInput.cloneNode(true);
      freshInput.id = "mobile-payment-amount-input";
      freshInput.name = "mobile_payment_amount";
      freshInput.removeAttribute("readonly");
      freshInput.removeAttribute("disabled");
      // Leave empty — cashier manually enters the amount the customer wants to pay
      freshInput.value = "";
      freshInput.setAttribute("placeholder", "0.00");
      clonedInput.parentNode.replaceChild(freshInput, clonedInput);

      freshInput.addEventListener("focus", function () {
        if (this.value === "0.00" || this.value === "0") this.value = "";
        this.select();
      });
      freshInput.addEventListener("blur", function () {
        if (!this.value || this.value === "") this.value = "0.00";
      });
    }

    // ── 6. Update any labels in the cloned row ──
    let labels = mobileRow.querySelectorAll("label, .v-label, .v-field-label");
    for (let lbl of labels) {
      let text = (lbl.textContent || "").trim().toUpperCase();
      if (nativeLabels.includes(text)) {
        lbl.textContent = "Mode of Payment";
      }
    }

    // ── 7. Insert the new row right after the original payment row ──
    if (paymentRow.nextSibling) {
      paymentRow.parentNode.insertBefore(mobileRow, paymentRow.nextSibling);
    } else {
      paymentRow.parentNode.appendChild(mobileRow);
    }

    // ── 8. Clear mobile amount when other payment buttons are clicked ──
    mobile_payments.pos._attach_other_button_listeners();
  },

  /**
   * Fallback: create a full payment row when native row structure can't be cloned.
   * Mimics the v-row > v-col-6 + v-col-6 layout of the POS payment section.
   */
  _create_fallback_row: function () {
    if (document.querySelector(".mobile-payment-pos-btn")) return;

    let methods = mobile_payments.pos._methods;
    if (!methods || !methods.length) return;

    // Try to find the payments area by looking for common POS containers
    let paymentsArea = document.querySelector(
      ".v-row.payments, [class*='payments'][class*='row'], .payments"
    );
    if (!paymentsArea) {
      // Look for the area around REC CASH-like elements
      let container = document.querySelector(
        ".pay-btn, .summary-btn, .v-card, .v-sheet, [class*='payment']"
      );
      if (container) paymentsArea = container.parentElement;
    }
    if (!paymentsArea) return;

    // Build a row that matches the Vuetify payment row structure
    let row = document.createElement("div");
    row.className = "v-row v-row--dense payments pa-1 mobile-payment-row";
    row.innerHTML = `
      <div class="v-col v-col-6" style="padding: 4px;">
        <div style="display:flex; align-items:center; border:1px solid #ccc; border-radius:4px; padding:8px 12px; background:#fff;">
          <span style="color:#666; margin-right:6px; font-size:14px;">$</span>
          <input id="mobile-payment-amount-input" type="number" step="0.01" min="0"
            style="border:none; outline:none; width:100%; font-size:16px; font-weight:500; background:transparent;" />
        </div>
      </div>
      <div class="v-col v-col-6" style="padding: 4px;">
        <button type="button" class="mobile-payment-pos-btn v-btn v-btn--block v-btn--elevated v-theme--dark v-btn--density-default v-btn--size-default v-btn--variant-elevated"
          style="background-color: #00d4ff !important; color: white !important; border: none; min-height: 44px; width: 100%; border-radius: 4px;
          font-weight: 600; font-size: 14px; cursor: pointer; text-transform: uppercase; display: flex; align-items: center; justify-content: center;">
          <span class="v-btn__content">MODE OF PAYMENT</span>
        </button>
      </div>
    `;

    // Wire up input
    let input = row.querySelector("#mobile-payment-amount-input");
    if (input) {
      input.addEventListener("focus", function () {
        if (this.value === "0.00" || this.value === "0") this.value = "";
      });
      input.addEventListener("blur", function () {
        if (!this.value || this.value === "") this.value = "0.00";
      });
      // Leave empty — amount is filled when MODE OF PAYMENT button is clicked
    }

    // Wire up button
    let btn = row.querySelector(".mobile-payment-pos-btn");
    if (btn) {
      btn.addEventListener("mouseenter", function () {
        this.style.setProperty("background-color", "#00bce0", "important");
      });
      btn.addEventListener("mouseleave", function () {
        this.style.setProperty("background-color", "#00d4ff", "important");
      });
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        let amountInput = document.querySelector("#mobile-payment-amount-input");
        // Auto-fill with POS outstanding amount when button is clicked
        let posTotal = mobile_payments.pos._get_pos_outstanding_amount();
        if (amountInput && posTotal > 0) {
          amountInput.value = posTotal;
        }
        let amount = amountInput ? parseFloat(amountInput.value) || 0 : 0;
        if (!amount || amount <= 0) {
          frappe.show_alert({
            message: __("Please enter the amount to pay"),
            indicator: "orange",
          });
          if (amountInput) { amountInput.focus(); amountInput.select(); }
          return;
        }
        mobile_payments.pos.show_pos_payment_dialog(null, amount);
      });
    }

    // Insert after the last payment row or append to the payments area
    let existingRows = paymentsArea.querySelectorAll(".v-row.payments, [class*='payments'][class*='row']");
    if (existingRows.length) {
      let lastRow = existingRows[existingRows.length - 1];
      if (lastRow.nextSibling) {
        lastRow.parentNode.insertBefore(row, lastRow.nextSibling);
      } else {
        lastRow.parentNode.appendChild(row);
      }
    } else {
      paymentsArea.appendChild(row);
    }

    // Clear mobile amount when other payment buttons are clicked
    mobile_payments.pos._attach_other_button_listeners();
  },

  /**
   * Attach click listeners to all NON-mobile payment buttons so that
   * clicking any of them (REC CASH, ZAAD, etc.) clears the mobile
   * payment amount input field. This prevents stale amounts from
   * carrying over when the cashier switches to a different payment mode.
   */
  _attach_other_button_listeners: function () {
    if (mobile_payments.pos._other_btn_listeners_attached) return;
    mobile_payments.pos._other_btn_listeners_attached = true;

    // Use event delegation on the document body for resilience
    document.addEventListener("click", function (e) {
      // Find the nearest button ancestor (or the element itself)
      let btn = e.target.closest("button, .v-btn");
      if (!btn) return;

      // Ignore if this is the MODE OF PAYMENT button itself
      if (btn.classList.contains("mobile-payment-pos-btn")) return;

      // Check if this button is one of the native payment mode buttons
      let text = (btn.textContent || "").trim().toUpperCase();
      let paymentLabels = ["REC CASH", "ZAAD", "SAHAL", "EVCPLUS", "EDAHAB", "WAAFIPAY",
        "CASH", "CARD", "BANK", "CHEQUE", "CHECK", "WIRE TRANSFER"];
      let isPaymentBtn = paymentLabels.some(function (label) {
        return text === label || text.includes(label);
      });

      // Also check if the button is inside a payments row structure
      if (!isPaymentBtn) {
        let row = btn.closest(".v-row.payments, [class*='payments'][class*='row']");
        if (row && !row.classList.contains("mobile-payment-row")) {
          isPaymentBtn = true;
        }
      }

      if (isPaymentBtn) {
        let amountInput = document.querySelector("#mobile-payment-amount-input");
        if (amountInput) {
          amountInput.value = "";
        }
      }
    }, true); // Use capture phase to run before Vue handlers
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

    // The amount must come from the manually-entered input field or the
    // value passed directly by the button click — never auto-guess.
    if (!amount || amount <= 0) {
      let amountInput = document.querySelector("#mobile-payment-amount-input");
      if (amountInput) {
        amount = parseFloat(amountInput.value) || 0;
      }
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

    // Auto-detect currency from the POS transaction (Sales Invoice / POS Invoice)
    let txn_currency = mobile_payments.pos._get_pos_currency();

    // Get current invoice name for memo
    let invoice_name = mobile_payments.pos._get_current_invoice_name() || "";

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
              ${invoice_name ?
                '<p class="text-muted">' + __("Invoice") + ": <strong>" + invoice_name + "</strong></p>" : ""}
              <p class="text-muted">${__("Currency")}: <strong>${txn_currency}</strong></p>
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
        // Validate phone number format before proceeding
        let phone = (values.phone_number || "").replace(/[\s\-()]/g, "");
        if (!mobile_payments.pos._validate_phone_number(phone)) {
          return; // Validation shows its own error
        }

        d.hide();

        let parts = values.payment_method.split("|");
        let provider = parts[0];
        let method = parts[1];

        mobile_payments.pos._process_pos_payment(
          provider,
          method,
          phone,
          values.amount,
          customer_info,
          txn_currency
        );
      },
    });

    d.show();
  },

  /**
   * Process the POS mobile payment.
   */
  _process_pos_payment: function (provider, method, phone, amount, customer_info, currency) {
    currency = currency || "USD";
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
        currency: currency,
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
   * Get the currency from the current POS transaction.
   * Reads from the POS Invoice / Sales Invoice, NOT user selection.
   * This ensures the currency sent to the payment API matches the transaction.
   */
  _get_pos_currency: function () {
    try {
      // From current form (POS Invoice / Sales Invoice)
      if (cur_frm && cur_frm.doc) {
        if (cur_frm.doc.currency) return cur_frm.doc.currency;
      }

      // From POS Awesome Vue state
      let posApp = document.querySelector("#pos-awesome-root, .pos-awesome-app, [data-pos-app]");
      if (posApp && posApp.__vue__) {
        let vue = posApp.__vue__;
        if (vue.currency) return vue.currency;
        if (vue.$store && vue.$store.state.currency) return vue.$store.state.currency;
        // Try reading from the invoice doc in Vue
        if (vue.invoice && vue.invoice.currency) return vue.invoice.currency;
        if (vue.doc && vue.doc.currency) return vue.doc.currency;
      }

      // From company default
      let defaultCurrency = frappe.defaults.get_global_default("currency");
      if (defaultCurrency) return defaultCurrency;
    } catch (e) {
      console.log("Could not detect POS currency:", e);
    }

    return "USD";
  },

  /**
   * Validate a phone number format for mobile money payments.
   * Must be at least 5 digits (to support 6-digit merchant numbers).
   * Somali numbers typically start with 252.
   * Shows an error alert if invalid and returns false.
   *
   * @param {string} phone - The phone number to validate
   * @returns {boolean} True if valid, false otherwise
   */
  _validate_phone_number: function (phone) {
    if (!phone) {
      frappe.show_alert({
        message: __("Phone number is required"),
        indicator: "red",
      });
      return false;
    }

    // Remove any non-digit characters except leading +
    let cleaned = phone.replace(/[^\d+]/g, "");
    if (cleaned.startsWith("+")) cleaned = cleaned.substring(1);

    // Ensure phone is not empty
    if (cleaned.length < 1) {
      frappe.show_alert({
        message: __("Phone number cannot be empty."),
        indicator: "red",
      });
      return false;
    }

    // Must be all digits
    if (!/^\d+$/.test(cleaned)) {
      frappe.show_alert({
        message: __("Phone number contains invalid characters."),
        indicator: "red",
      });
      return false;
    }

    // If starts with 252 (Somalia), must be 12 digits total
    if (cleaned.startsWith("252") && cleaned.length !== 12) {
      frappe.show_alert({
        message: __("Somali phone numbers must be 12 digits (e.g., 252612345678)"),
        indicator: "orange",
      });
      // Still allow — just warn
    }

    return true;
  },
   * Fetches phone from the customer's primary Contact mobile number.
   * Also reads the customer name from POS DOM if Vue state isn't accessible.
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

      // From POS DOM — look for customer name displayed in the header
      if (!info.name) {
        let customerSelectors = [
          ".customer-name", ".pos-customer", "[data-customer]",
          ".v-card-title", ".v-card-subtitle",
          ".customer-info .customer", ".customer-field"
        ];
        for (let sel of customerSelectors) {
          let el = document.querySelector(sel);
          if (el) {
            let text = (el.textContent || "").trim();
            if (text && text.length > 1 && !/^(customer|select|search)/i.test(text)) {
              info.name = text;
              break;
            }
          }
        }
      }

      // Also try to read customer from the POS header text "Customer: XXXX"
      if (!info.name) {
        let allHeaders = document.querySelectorAll(".v-card-title, .v-toolbar-title, h3, h4, .text-subtitle-1");
        for (let h of allHeaders) {
          let text = (h.textContent || "").trim();
          let match = text.match(/customer[:\s]+(.+)/i);
          if (match && match[1]) {
            info.name = match[1].trim();
            break;
          }
        }
      }

      // Fetch phone using robust server-side endpoint
      if (info.name) {
        frappe.call({
          method: "mobile_payments.api.pos.get_customer_phone",
          args: { customer: info.name },
          async: false,
          callback: function (r) {
            if (r.message && r.message.phone) {
              info.phone = r.message.phone;
            }
          },
        });
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
  // Retry after a delay — POS SPA may not be fully loaded on document ready
  setTimeout(function () {
    if (mobile_payments.pos._is_pos_page() && !mobile_payments.pos._initialized) {
      mobile_payments.pos.init();
    }
  }, 2000);
  setTimeout(function () {
    if (mobile_payments.pos._is_pos_page() && !mobile_payments.pos._initialized) {
      mobile_payments.pos.init();
    }
  }, 5000);
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
