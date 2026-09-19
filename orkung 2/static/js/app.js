// Orkung Livestock Manager -- shared client-side behaviour.
// No external libraries/CDNs: everything here is hand-written so the app
// keeps working with no internet connection at all (important on a farm).
(function () {
  "use strict";

  // ---- Mobile drawer ----
  var menuBtn = document.getElementById("menuBtn");
  var overlay = document.getElementById("drawerOverlay");
  if (menuBtn && overlay) {
    menuBtn.addEventListener("click", function () { overlay.classList.add("open"); });
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) overlay.classList.remove("open");
    });
  }

  // ---- Online / offline indicator ----
  var banner = document.getElementById("offlineBanner");
  function updateOnlineState() {
    if (!banner) return;
    if (navigator.onLine) banner.classList.remove("show");
    else banner.classList.add("show");
  }
  window.addEventListener("online", updateOnlineState);
  window.addEventListener("offline", updateOnlineState);
  updateOnlineState();

  // ---- Register service worker (installable PWA shell) ----
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/service-worker.js").catch(function () {});
    });
  }

  // ---- Draft-preserving forms ----
  // Any <form data-draft-key="..."> automatically saves field values to
  // localStorage as the user types, and restores them if the page is
  // reloaded/reopened before the form is submitted (e.g. connection drops
  // mid-entry). Cleared on successful submit.
  function wireDraftForms() {
    document.querySelectorAll("form[data-draft-key]").forEach(function (form) {
      var key = "orkung-draft:" + form.getAttribute("data-draft-key");
      var fields = form.querySelectorAll("input, select, textarea");

      try {
        var saved = JSON.parse(localStorage.getItem(key) || "{}");
        fields.forEach(function (f) {
          if (!f.name || saved[f.name] === undefined) return;
          if (f.type === "checkbox" || f.type === "radio") f.checked = saved[f.name];
          else f.value = saved[f.name];
        });
        if (Object.keys(saved).length) {
          var note = document.createElement("div");
          note.className = "flash info";
          note.textContent = "Restored unsaved changes from your last visit to this form.";
          form.prepend(note);
        }
      } catch (e) { /* ignore corrupt/unavailable storage */ }

      var save = function () {
        try {
          var data = {};
          fields.forEach(function (f) {
            if (!f.name) return;
            data[f.name] = (f.type === "checkbox" || f.type === "radio") ? f.checked : f.value;
          });
          localStorage.setItem(key, JSON.stringify(data));
        } catch (e) { /* private mode / quota -- fail silently */ }
      };
      form.addEventListener("input", save);
      form.addEventListener("submit", function () {
        try { localStorage.removeItem(key); } catch (e) {}
      });
    });
  }
  wireDraftForms();

  // ---- Bulk weight entry: add / remove rows ----
  var addRowBtn = document.getElementById("addWeightRow");
  if (addRowBtn) {
    addRowBtn.addEventListener("click", function () {
      var body = document.getElementById("bulkWeightBody");
      var tmpl = document.getElementById("bulkWeightRowTmpl");
      if (body && tmpl) {
        var clone = tmpl.content.cloneNode(true);
        body.appendChild(clone);
      }
    });
  }
  document.addEventListener("click", function (e) {
    if (e.target && e.target.classList.contains("remove-row")) {
      var row = e.target.closest("tr");
      if (row) row.remove();
    }
  });

  // ---- Confirm on destructive / status-change actions ----
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!window.confirm(el.getAttribute("data-confirm"))) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
  });

  // ---- Simple client-side table search filter ----
  document.querySelectorAll("[data-table-search]").forEach(function (input) {
    var tableId = input.getAttribute("data-table-search");
    var table = document.getElementById(tableId);
    if (!table) return;
    input.addEventListener("input", function () {
      var term = input.value.toLowerCase();
      table.querySelectorAll("tbody tr").forEach(function (row) {
        row.style.display = row.textContent.toLowerCase().indexOf(term) > -1 ? "" : "none";
      });
    });
  });
})();
