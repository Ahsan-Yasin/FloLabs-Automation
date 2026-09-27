/* API keys page: list, create (shown once), rotate, revoke. */
(function () {
  "use strict";
  const { api, toast, busy, showFormError, fmt, el, icon, qs, confirmDialog } = window.HC;

  const tbody = qs("[data-keys]");
  const dialog = qs("[data-key-dialog]");
  const form = qs("[data-key-form]");
  const errorBox = qs("[data-form-error]", form);
  const scopeList = qs("[data-scope-list]", form);
  let scopes = {};

  function reveal(key) {
    const box = qs("[data-key-reveal]");
    qs("[data-key-value]", box).textContent = key.key;
    const origin = location.origin;
    qs("[data-key-curl]", box).textContent =
      `curl ${origin}/api/v1/jobs \\\n  -H "Authorization: Bearer ${key.key}"`;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function row(key) {
    const rotate = el("button", { class: "btn btn-sm btn-ghost", type: "button", title: "Replace with a new secret" }, icon("refresh"), "Rotate");
    const revoke = el("button", { class: "btn btn-sm btn-danger", type: "button" }, icon("trash"), "Revoke");
    rotate.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: `Rotate "${key.name}"?`, body: "The current secret stops working immediately; you get a new one with the same name and scopes.", confirm: "Rotate" });
      if (!ok) return;
      busy(rotate, true);
      try {
        reveal(await api(`/api/v1/keys/${key.id}/rotate`, { method: "POST" }));
        toast("Key rotated. Update your automations.");
        load();
      } catch (err) {
        toast(err.message, "error");
        busy(rotate, false);
      }
    });
    revoke.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: `Revoke "${key.name}"?`, body: "Automations using this key stop working immediately. This can't be undone.", confirm: "Revoke", danger: true });
      if (!ok) return;
      busy(revoke, true);
      try {
        await api(`/api/v1/keys/${key.id}`, { method: "DELETE" });
        toast("Key revoked.");
        load();
      } catch (err) {
        toast(err.message, "error");
        busy(revoke, false);
      }
    });
    const expires = key.expires_at ? el("div", { class: "row-sub", text: `expires ${fmt.date(key.expires_at)}` }) : null;
    return el("tr", {},
      el("td", {}, el("strong", { text: key.name }), expires),
      el("td", {}, el("code", { text: key.display })),
      el("td", {}, ...key.scopes.map((s) => el("span", { class: "badge", style: "margin:2px", text: s }))),
      el("td", { class: "time", text: key.last_used_at ? fmt.ago(key.last_used_at) : "never" }),
      el("td", { class: "time", text: fmt.ago(key.created_at) }),
      el("td", { class: "actions" }, rotate, revoke));
  }

  async function load() {
    try {
      const body = await api("/api/v1/keys");
      scopes = body.scopes || {};
      if (!body.items.length) {
        tbody.replaceChildren(el("tr", {}, el("td", { colspan: "6" },
          el("div", { class: "empty", style: "border:0" }, el("span", { class: "icon-tile" }, icon("key")),
            el("h3", { text: "No API keys yet" }), el("p", { class: "small", text: "Create one to connect an automation." })))));
      } else {
        tbody.replaceChildren(...body.items.map(row));
      }
    } catch (err) {
      tbody.replaceChildren(el("tr", {}, el("td", { colspan: "6", text: err.message })));
    }
  }

  qs("[data-new-key]").addEventListener("click", () => {
    form.reset();
    showFormError(errorBox, "");
    scopeList.replaceChildren(...Object.entries(scopes).map(([scope, text]) =>
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "scope", value: scope, checked: true }),
        el("div", {}, el("strong", { text: scope }), el("span", { text })))));
    dialog.showModal();
    qs("#key-name", form).focus();
  });
  qs("[data-close]", form).addEventListener("click", () => dialog.close());

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError(errorBox, "");
    const name = form.elements.name.value.trim();
    if (!name) { showFormError(errorBox, "Give the key a name, e.g. “n8n production”."); return; }
    const chosen = Array.from(form.querySelectorAll('input[name="scope"]:checked')).map((i) => i.value);
    if (!chosen.length) { showFormError(errorBox, "Pick at least one permission."); return; }
    const expires = form.elements.expires.value;
    const submit = qs('button[type="submit"]', form);
    busy(submit, true);
    try {
      const key = await api("/api/v1/keys", { method: "POST", json: { name, scopes: chosen, expires_in_days: expires ? Number(expires) : null } });
      dialog.close();
      reveal(key);
      load();
    } catch (err) {
      showFormError(errorBox, err.code === "email_not_verified" ? "Confirm your email address first." : err.message);
    } finally {
      busy(submit, false);
    }
  });

  load();
})();
