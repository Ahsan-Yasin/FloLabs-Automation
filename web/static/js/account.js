/* Account settings. */
(function () {
  "use strict";
  const { api, toast, busy, showFormError, fmt, el, icon, qs, confirmDialog } = window.HC;

  /* ------------------------------------------------------------ profile */
  const profile = qs("[data-profile-form]");
  api("/api/v1/auth/me").then((me) => {
    profile.elements.name.value = me.name || "";
    profile.elements.notify_on_done.checked = !!me.notify_on_done;
  }).catch(() => {});
  profile.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = qs('button[type="submit"]', profile);
    busy(btn, true);
    try {
      await api("/api/v1/auth/me", { method: "PATCH", json: { name: profile.elements.name.value, notify_on_done: profile.elements.notify_on_done.checked } });
      toast("Saved.");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      busy(btn, false);
    }
  });

  /* ------------------------------------------------------------ password */
  const pwForm = qs("[data-password-form]");
  pwForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const box = qs("[data-form-error]", pwForm);
    showFormError(box, "");
    if (!pwForm.reportValidity()) return;
    const btn = qs('button[type="submit"]', pwForm);
    busy(btn, true);
    try {
      await api("/api/v1/auth/change-password", { method: "POST", json: {
        current_password: pwForm.elements.current_password.value, new_password: pwForm.elements.new_password.value } });
      pwForm.reset();
      toast("Password changed. Other browsers were signed out.");
      loadSessions();
    } catch (err) {
      showFormError(box, err.message);
    } finally {
      busy(btn, false);
    }
  });

  /* ------------------------------------------------------------ webhook secret + test */
  const secretEl = qs("[data-secret]");
  const copyBtn = qs("[data-secret-copy]");
  const revealBtn = qs("[data-secret-reveal]");
  const showSecret = (value) => { secretEl.textContent = value; copyBtn.hidden = false; revealBtn.hidden = true; };
  revealBtn.addEventListener("click", async () => {
    busy(revealBtn, true);
    try { showSecret((await api("/api/v1/auth/webhook-secret")).webhook_secret); } catch (err) { toast(err.message, "error"); busy(revealBtn, false); }
  });
  qs("[data-secret-rotate]").addEventListener("click", async (e) => {
    const ok = await confirmDialog({ title: "Rotate the webhook secret?", body: "Receivers that check signatures must switch to the new secret, or they will reject the next webhooks.", confirm: "Rotate" });
    if (!ok) return;
    const btn = e.currentTarget;
    busy(btn, true);
    try {
      showSecret((await api("/api/v1/auth/webhook-secret/rotate", { method: "POST" })).webhook_secret);
      toast("New secret created.");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      busy(btn, false);
    }
  });
  const testForm = qs("[data-webhook-test]");
  testForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = qs("[data-webhook-result]");
    const btn = qs('button[type="submit"]', testForm);
    busy(btn, true);
    try {
      const r = await api("/api/v1/webhooks/test", { method: "POST", json: { url: testForm.elements.url.value.trim() } });
      out.textContent = r.delivered ? `Delivered: your server answered HTTP ${r.status_code}.`
        : `Not delivered: ${r.error || `HTTP ${r.status_code}`}.`;
      out.style.color = r.delivered ? "var(--ok)" : "var(--danger)";
    } catch (err) {
      out.textContent = err.message;
      out.style.color = "var(--danger)";
    } finally {
      busy(btn, false);
    }
  });

  /* ------------------------------------------------------------ sessions */
  const sessionsBox = qs("[data-sessions]");
  const describe = (ua) => {
    if (!ua) return "Unknown browser";
    const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
    const os = /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : "";
    return os ? `${browser} on ${os}` : browser;
  };
  async function loadSessions() {
    try {
      const { items } = await api("/api/v1/auth/sessions");
      sessionsBox.replaceChildren(...items.map((s) => {
        const end = s.current ? el("span", { class: "badge badge-ok", text: "this browser" }) : el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Sign out" });
        if (!s.current) {
          end.addEventListener("click", async () => {
            busy(end, true);
            try { await api(`/api/v1/auth/sessions/${s.id}`, { method: "DELETE" }); toast("Signed out."); loadSessions(); }
            catch (err) { toast(err.message, "error"); busy(end, false); }
          });
        }
        return el("div", { class: "artifact" }, icon("shield"),
          el("div", { style: "min-width:0" }, el("div", { class: "name", text: describe(s.user_agent) }),
            el("div", { class: "row-sub", text: `${s.ip || "unknown address"} · active ${fmt.ago(s.last_active_at)}` })),
          el("span", { style: "margin-left:auto" }, end));
      }));
    } catch (err) {
      sessionsBox.textContent = err.message;
    }
  }
  qs("[data-logout-all]").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Sign out everywhere?", body: "Every browser, including this one, is signed out. API keys keep working.", confirm: "Sign out everywhere" });
    if (!ok) return;
    try { await api("/api/v1/auth/logout-all", { method: "POST" }); } catch (err) { /* signed out anyway */ }
    location.assign("/login");
  });
  loadSessions();

  /* ------------------------------------------------------------ delete account */
  const dialog = qs("[data-delete-dialog]");
  const delForm = qs("[data-delete-form]");
  qs("[data-delete-account]").addEventListener("click", () => { delForm.reset(); showFormError(qs("[data-form-error]", delForm), ""); dialog.showModal(); });
  qs("[data-close]", delForm).addEventListener("click", () => dialog.close());
  delForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const box = qs("[data-form-error]", delForm);
    const btn = qs('button[type="submit"]', delForm);
    busy(btn, true);
    try {
      await api("/api/v1/account", { method: "DELETE", json: { confirm_email: delForm.elements.confirm_email.value, password: delForm.elements.password.value } });
      location.assign("/?deleted=1");
    } catch (err) {
      showFormError(box, err.message);
      busy(btn, false);
    }
  });
})();
