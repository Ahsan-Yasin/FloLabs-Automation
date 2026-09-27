/* Sign-in pages: login, sign-up, forgot, reset. */
(function () {
  "use strict";
  const { api, busy, showFormError, qs, qsa } = window.HC;

  const FRIENDLY = {
    invalid_credentials: "That email and password don't match. Check both and try again.",
    account_disabled: "This account is disabled. Contact the site's admin.",
    email_taken: "An account with this email already exists. Log in instead.",
    rate_limited: "Too many attempts. Wait a few minutes and try again.",
    signup_closed: "Sign-up is closed on this site.",
  };

  const message = (err) => {
    if (err.code === "rate_limited" && err.retryAfter) {
      const minutes = Math.max(1, Math.ceil(err.retryAfter / 60));
      return `Too many attempts. Try again in about ${minutes} minute${minutes === 1 ? "" : "s"}.`;
    }
    return FRIENDLY[err.code] || err.message;
  };

  /* show/hide password */
  qsa("[data-toggle-password]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = btn.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  /* strength meter: length and variety, with the server's rules as hints */
  qsa("[data-pw-meter]").forEach((meter) => {
    const input = meter.parentElement.querySelector('input[type="password"], input[name="password"]');
    const hint = meter.parentElement.querySelector("[data-pw-hint]");
    const base = hint ? hint.textContent : "";
    input.addEventListener("input", () => {
      const v = input.value;
      let score = 0;
      if (v.length >= 10) score++;
      if (v.length >= 14) score++;
      if (/[A-Z]/.test(v) + /[a-z]/.test(v) + /\d/.test(v) + /[^A-Za-z0-9]/.test(v) >= 2 || /\s/.test(v)) score++;
      if (v.length >= 18 && new Set(v).size >= 8) score++;
      meter.dataset.score = v ? String(Math.max(1, score)) : "0";
      if (hint) hint.textContent = v && v.length < 10 ? `${10 - v.length} more character${10 - v.length === 1 ? "" : "s"}` : base;
    });
  });

  const form = qs("[data-form]");
  if (!form) return;
  const kind = form.dataset.form;
  const errorBox = qs("[data-form-error]", form);
  const submit = qs('button[type="submit"]', form);
  const value = (name) => (form.elements[name] ? form.elements[name].value : "");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError(errorBox, "");
    if (!form.reportValidity()) return;
    busy(submit, true);
    try {
      if (kind === "login") {
        await api("/api/v1/auth/login", { method: "POST", json: { email: value("email"), password: value("password") } });
        location.assign(form.dataset.next || "/app");
        return;
      }
      if (kind === "signup") {
        await api("/api/v1/auth/signup", {
          method: "POST",
          json: { name: value("name"), email: value("email"), password: value("password") },
        });
        const next = form.dataset.next && form.dataset.next !== "/app" ? form.dataset.next : "/app?welcome=1";
        location.assign(next);
        return;
      }
      if (kind === "forgot") {
        await api("/api/v1/auth/forgot", { method: "POST", json: { email: value("email") } });
        qs("[data-form-success]", form).hidden = false;
        form.elements.email.value = "";
        busy(submit, false);
        return;
      }
      if (kind === "reset") {
        await api("/api/v1/auth/reset", { method: "POST", json: { token: form.dataset.token, new_password: value("password") } });
        form.hidden = true;
        qs("[data-form-done]").hidden = false;
        return;
      }
    } catch (err) {
      showFormError(errorBox, message(err));
      busy(submit, false);
      const first = qs("input", form);
      if (err.code === "invalid_credentials" && form.elements.password) form.elements.password.select();
      else if (first) first.focus();
    }
  });
})();
