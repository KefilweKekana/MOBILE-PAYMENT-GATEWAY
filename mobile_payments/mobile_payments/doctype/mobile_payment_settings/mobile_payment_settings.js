// Mobile Payment Settings - Client Script
// Adds "Test Connection" buttons for WaafiPay and Edahab

frappe.ui.form.on("Mobile Payment Settings", {
  refresh: function (frm) {
    // WaafiPay Test Connection button
    if (frm.doc.waafipay_enabled) {
      frm.add_custom_button(
        __("Test WaafiPay Connection."),
        function () {
          frappe.call({
            method:
              "mobile_payments.mobile_payments.doctype.mobile_payment_settings.mobile_payment_settings.test_waafipay_connection",
            freeze: true,
            freeze_message: __("Testing WaafiPay connection..."),
            callback: function (r) {
              if (r.message) {
                if (r.message.success) {
                  frappe.msgprint({
                    title: __("WaafiPay Connection Successful"),
                    message: r.message.message,
                    indicator: "green",
                  });
                } else {
                  frappe.msgprint({
                    title: __("WaafiPay Connection Failed"),
                    message: r.message.message,
                    indicator: "red",
                  });
                }
              }
            },
          });
        },
        __("Test Connection")
      );
    }

    // Edahab Test Connection button
    if (frm.doc.edahab_enabled) {
      frm.add_custom_button(
        __("Test Edahab Connection"),
        function () {
          frappe.call({
            method:
              "mobile_payments.mobile_payments.doctype.mobile_payment_settings.mobile_payment_settings.test_edahab_connection",
            freeze: true,
            freeze_message: __("Testing Edahab connection..."),
            callback: function (r) {
              if (r.message) {
                if (r.message.success) {
                  frappe.msgprint({
                    title: __("Edahab Connection Successful"),
                    message: r.message.message,
                    indicator: "green",
                  });
                } else {
                  frappe.msgprint({
                    title: __("Edahab Connection Failed"),
                    message: r.message.message,
                    indicator: "red",
                  });
                }
              }
            },
          });
        },
        __("Test Connection")
      );
    }
  },
});
