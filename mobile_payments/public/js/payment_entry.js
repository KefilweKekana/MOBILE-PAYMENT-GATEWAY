// Payment Entry — "Pay with Mobile" button
// Loaded via doctype_js hook so it is guaranteed to be
// available whenever the Payment Entry form is opened.

frappe.ui.form.on("Payment Entry", {
  refresh: function (frm) {
    maybe_show_pe_mobile_button(frm);
  },
  mode_of_payment: function (frm) {
    maybe_show_pe_mobile_button(frm);
  },
});

function maybe_show_pe_mobile_button(frm) {
  // Only on draft Payment Entries with a mode_of_payment set
  if (!frm || frm.doc.docstatus !== 0) return;
  if (!frm.doc.mode_of_payment) return;

  // Normalise the mode name and check if it's mobile-money related
  let mobile_modes = [
    "mobilemoney",
    "zaad",
    "evcplus",
    "evc",
    "edahab",
    "sahal",
    "waafipay",
    "waafi",
    "mobilepayment",
  ];
  let raw = (frm.doc.mode_of_payment || "")
    .toLowerCase()
    .replace(/[\s_\-]/g, "");

  let is_mobile = mobile_modes.some(function (m) {
    return raw.indexOf(m) !== -1;
  });
  if (!is_mobile) return;

  // Find the first linked Sales Invoice from the references table
  let si_ref = (frm.doc.references || []).find(function (r) {
    return r.reference_doctype === "Sales Invoice" && r.reference_name;
  });

  frm.add_custom_button(__("Pay with Mobile"), function () {
    if (si_ref && window.mobile_payments) {
      // Open the standard mobile payment dialog using the linked Sales Invoice
      frappe.model.with_doc(
        "Sales Invoice",
        si_ref.reference_name,
        function () {
          let si_frm = {
            doc: frappe.get_doc("Sales Invoice", si_ref.reference_name),
          };
          // Monkey-patch reload_doc so the success dialog reloads the PE
          si_frm.reload_doc = function () {
            frm.reload_doc();
          };
          window.mobile_payments.show_payment_dialog(si_frm);
        }
      );
    } else if (window.mobile_payments) {
      // No linked SI — show a standalone dialog to collect phone & provider
      window.mobile_payments._show_pe_standalone_payment_dialog(frm);
    } else {
      frappe.msgprint(
        __(
          "Mobile Payments module is not loaded. Please refresh the page and try again."
        )
      );
    }
  });

  // Make the button stand out as a primary action
  frm.change_custom_button_type(__("Pay with Mobile"), null, "primary");
}
