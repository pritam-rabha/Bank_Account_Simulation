/* BankSim front-end helpers. No inline scripts are used (strict CSP). */
(function () {
  "use strict";

  // 0. Mobile navigation drawer.
  var toggles = document.querySelectorAll("[data-nav-toggle]");
  var setNav = function (open) {
    document.body.classList.toggle("nav-open", open);
    toggles.forEach(function (t) { if (t.tagName === "BUTTON") { t.setAttribute("aria-expanded", open ? "true" : "false"); } });
  };
  toggles.forEach(function (t) {
    t.addEventListener("click", function () { setNav(!document.body.classList.contains("nav-open")); });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { setNav(false); } });

  // 1. Prevent double submission and confirm risky actions.
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      if (form.dataset.submitted === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.submitted = "1";
      form.querySelectorAll("button[data-once]").forEach(function (btn) {
        btn.disabled = true;
        var spinner = btn.querySelector(".spinner-border");
        if (spinner) { spinner.classList.remove("d-none"); }
      });
    });
  });
  // Re-enable buttons when a page is restored from the back/forward cache.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      document.querySelectorAll("form").forEach(function (f) { f.dataset.submitted = ""; });
      document.querySelectorAll("button[data-once]").forEach(function (b) { b.disabled = false; });
    }
  });

  // 2. PIN inputs: digits only.
  document.querySelectorAll("input[data-pin], input[name='destination_account']").forEach(function (input) {
    input.addEventListener("input", function () {
      var cleaned = input.value.replace(/[^0-9]/g, "");
      if (cleaned !== input.value) { input.value = cleaned; }
    });
  });

  // 3. Money inputs: keep digits and one decimal point, max two decimals.
  document.querySelectorAll("input[data-money]").forEach(function (input) {
    input.addEventListener("input", function () {
      var v = input.value.replace(/[^0-9.]/g, "");
      var parts = v.split(".");
      if (parts.length > 2) { v = parts[0] + "." + parts.slice(1).join(""); }
      parts = v.split(".");
      if (parts.length === 2) { v = parts[0] + "." + parts[1].slice(0, 2); }
      if (v !== input.value) { input.value = v; }
    });
  });

  // 4. Registration: show the FD term only for fixed deposits.
  var typeSelect = document.getElementById("account_type");
  var fdGroup = document.querySelector("[data-show-for='FIXED_DEPOSIT']");
  if (typeSelect && fdGroup) {
    var syncType = function () { fdGroup.classList.toggle("is-hidden", typeSelect.value !== "FIXED_DEPOSIT"); };
    typeSelect.addEventListener("change", syncType);
    syncType();
  }

  // 5. Calculator: show compounding frequency only for compound interest.
  var compGroup = document.getElementById("compounding-group");
  var methodRadios = document.querySelectorAll("input[name='method']");
  if (compGroup && methodRadios.length) {
    var syncMethod = function () {
      var checked = document.querySelector("input[name='method']:checked");
      compGroup.classList.toggle("is-hidden", !checked || checked.value !== "compound");
    };
    methodRadios.forEach(function (r) { r.addEventListener("change", syncMethod); });
    syncMethod();
  }
})();
