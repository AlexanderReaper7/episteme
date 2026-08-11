/* Voice catalog editor: dynamic provider params, edit-prefill, drag reorder.

   The param fields under "Generation params" are rendered from the selected
   provider's schema (embedded as JSON) rather than hardcoded, so a new provider is
   picked up without touching this page. "edit" prefills the form from the row's own
   `data-edit` payload. Dragging a row reorders the catalog; the top row becomes the
   default (lowest sort_order).

   Row-level handlers (edit, drag) are delegated on the stable #voice-table, not the
   #voice-rows <tbody> - create/toggle/delete swap that <tbody> out via htmx, so
   binding to it would lose the handlers after the first mutation. */
(function () {
  "use strict";

  function readJSON(id) {
    var el = document.getElementById(id);
    try {
      return el ? JSON.parse(el.textContent) : null;
    } catch (e) {
      return null;
    }
  }

  var schemas = readJSON("voice-param-schemas") || {};

  var form = document.getElementById("voice-form");
  var table = document.getElementById("voice-table");
  var providerSel = document.getElementById("voice-field-provider");
  var paramsBox = document.getElementById("voice-params");
  var idField = document.getElementById("voice-field-id");
  var heading = document.getElementById("voice-form-heading");
  var submitBtn = document.getElementById("voice-submit");
  var resetLink = document.getElementById("voice-form-reset");

  if (!form) return;

  /* --- dynamic provider params ------------------------------------------- */

  function renderParams(provider, values) {
    values = values || {};
    var fields = schemas[provider] || [];
    paramsBox.innerHTML = "";
    fields.forEach(function (f) {
      var label = document.createElement("label");
      label.title = f.help || "";
      label.appendChild(document.createTextNode(f.label));

      var input = document.createElement("input");
      input.name = f.key;
      input.type = f.kind === "number" ? "number" : "text";
      if (f.step != null) input.step = f.step;
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      if (f.placeholder) input.placeholder = f.placeholder;
      if (values[f.key] != null) input.value = values[f.key];
      label.appendChild(input);
      paramsBox.appendChild(label);
    });
  }

  // Preserve values across a provider switch where a key still exists.
  function currentParamValues() {
    var out = {};
    paramsBox.querySelectorAll("input").forEach(function (i) {
      if (i.value !== "") out[i.name] = i.value;
    });
    return out;
  }

  if (providerSel && paramsBox) {
    renderParams(providerSel.value, {});
    providerSel.addEventListener("change", function () {
      renderParams(providerSel.value, currentParamValues());
    });
  }

  /* --- edit-prefill ------------------------------------------------------- */

  function enterEditMode(voice) {
    form.querySelector("[name=label]").value = voice.label || "";
    form.querySelector("[name=provider_voice_id]").value = voice.provider_voice_id || "";
    form.querySelector("[name=sort_order]").value = voice.sort_order != null ? voice.sort_order : 0;
    form.querySelector("[name=enabled]").checked = !!voice.enabled;
    idField.value = voice.id || "";
    if (providerSel) providerSel.value = voice.provider || providerSel.value;
    renderParams(providerSel ? providerSel.value : voice.provider, voice.params || {});

    heading.textContent = "Edit voice";
    submitBtn.textContent = "Update voice";
    if (resetLink) resetLink.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "start" });
    idField.focus();
  }

  function exitEditMode() {
    form.reset();
    heading.textContent = "Add voice";
    submitBtn.textContent = "Save voice";
    if (resetLink) resetLink.hidden = true;
    renderParams(providerSel ? providerSel.value : "", {});
  }

  function rowEdit(row) {
    try {
      return JSON.parse(row.dataset.edit);
    } catch (e) {
      return null;
    }
  }

  // Delegated on the table so it survives the tbody being swapped by a mutation.
  if (table) {
    table.addEventListener("click", function (e) {
      var btn = e.target.closest(".voice-edit-btn");
      if (!btn) return;
      var voice = rowEdit(btn.closest(".voice-row"));
      if (voice) enterEditMode(voice);
    });
  }

  if (resetLink) {
    resetLink.addEventListener("click", function (e) {
      e.preventDefault();
      exitEditMode();
    });
  }

  // A successful create/update swaps the tbody in; clear the form back to "Add".
  form.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail && e.detail.successful) exitEditMode();
  });

  /* --- drag-and-drop reorder --------------------------------------------- */

  if (table) {
    var dragging = null;

    table.addEventListener("dragstart", function (e) {
      var row = e.target.closest(".voice-row");
      if (!row) return;
      dragging = row;
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });

    table.addEventListener("dragend", function () {
      if (dragging) dragging.classList.remove("dragging");
      dragging = null;
    });

    table.addEventListener("dragover", function (e) {
      if (!dragging) return;
      e.preventDefault();
      var over = e.target.closest(".voice-row");
      if (!over || over === dragging) return;
      var rect = over.getBoundingClientRect();
      var after = e.clientY - rect.top > rect.height / 2;
      over.parentNode.insertBefore(dragging, after ? over.nextSibling : over);
    });

    table.addEventListener("drop", function (e) {
      e.preventDefault();
      persistOrder();
    });
  }

  function persistOrder() {
    var tbody = document.getElementById("voice-rows");
    var rows = Array.prototype.slice.call(tbody.querySelectorAll(".voice-row"));
    var order = rows.map(function (r) {
      return r.dataset.voiceId;
    });
    // Optimistic UI: renumber, move the default tag to the new top row, and keep each
    // row's embedded edit payload's sort_order in sync so a subsequent edit doesn't
    // re-save a stale position.
    rows.forEach(function (r, i) {
      var cell = r.querySelector(".voice-sort-cell");
      if (cell) cell.textContent = i;
      var tag = r.querySelector(".voice-default-tag");
      if (tag) tag.hidden = i !== 0;
      var edit = rowEdit(r);
      if (edit) {
        edit.sort_order = i;
        r.dataset.edit = JSON.stringify(edit);
      }
    });
    fetch("/admin/voices/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order: order }),
    })
      .then(function (r) {
        if (!r.ok) resync();
      })
      .catch(resync);
  }

  // On a persist failure, pull the server truth back into the admin region via htmx
  // (a targeted fragment swap - no full page reload).
  function resync() {
    if (window.htmx) {
      window.htmx.ajax("GET", "/admin/voices", { target: "#admin-main", swap: "innerHTML" });
    }
  }
})();
