/* Highlight Cutter: shared browser code (every page). Exposes window.HC.
   - api(): fetch wrapper for /api/v1 with the CSRF header and one silent
     session refresh on 401 (single-flight: parallel calls share one refresh,
     so the server's refresh-token reuse detection never fires on us);
   - upload(): multipart POST with progress (XMLHttpRequest);
   - toast(), formatting helpers, tabs, copy buttons, theme, nav, logout,
     all respecting reduced motion. */
(function () {
  "use strict";

  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const Motion = window.Motion || null;
  const EASE_OUT = [0.22, 1, 0.36, 1];
  const CSRF = { "X-Requested-With": "hc-web" };

  const qs = (sel, scope) => (scope || document).querySelector(sel);
  const qsa = (sel, scope) => Array.from((scope || document).querySelectorAll(sel));

  /* ------------------------------------------------------------ API */
  class ApiError extends Error {
    constructor(status, body) {
      super(messageFrom(body, status));
      this.status = status;
      this.body = body && typeof body === "object" ? body : {};
      this.code = this.body.error_code || "";
      this.retryAfter = this.body.retry_after_s || null;
    }
  }

  function messageFrom(body, status) {
    if (body && typeof body === "object") {
      const d = body.detail;
      if (typeof d === "string" && d) return d;
      if (d && typeof d === "object" && !Array.isArray(d) && d.message) return d.message;
      if (Array.isArray(d) && d.length) {
        return d.map((e) => {
          const field = Array.isArray(e.loc) ? e.loc.filter((p) => p !== "body").join(".") : "";
          return (field ? field + ": " : "") + (e.msg || "invalid");
        }).join("; ");
      }
    }
    if (status === 0) return "Can't reach the server. Check your connection.";
    return `Something went wrong (HTTP ${status}).`;
  }

  let refreshing = null;
  function refreshSession() {
    if (!refreshing) {
      refreshing = fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin", headers: CSRF })
        .then((r) => r.ok)
        .catch(() => false)
        .finally(() => { setTimeout(() => { refreshing = null; }, 1000); });
    }
    return refreshing;
  }

  function sessionLost() {
    const next = encodeURIComponent(location.pathname + location.search);
    location.assign(`/login?next=${next}`);
  }

  async function api(path, options = {}) {
    const { method = "GET", json, form, headers = {}, raw = false, quiet401 = false } = options;
    const h = new Headers({ ...CSRF, ...headers });
    let body;
    if (json !== undefined) {
      h.set("Content-Type", "application/json");
      body = JSON.stringify(json);
    } else if (form) {
      body = form;
    }
    const send = () => fetch(path, { method, headers: h, body, credentials: "same-origin" });
    let res;
    try {
      res = await send();
    } catch (err) {
      throw new ApiError(0, null);
    }
    if (res.status === 401 && !path.startsWith("/api/v1/auth/")) {
      if (await refreshSession()) res = await send();
      if (res.status === 401 && !quiet401) { sessionLost(); throw new ApiError(401, null); }
    }
    if (raw) return res;
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }

  function upload(path, formData, onProgress) {
    const attempt = () => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      xhr.withCredentials = true;
      xhr.setRequestHeader("X-Requested-With", "hc-web");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = xhr.responseText; }
        resolve({ status: xhr.status, data });
      };
      xhr.onerror = () => reject(new ApiError(0, null));
      xhr.send(formData);
    });
    return attempt().then(async (r) => {
      if (r.status === 401 && await refreshSession()) r = await attempt();
      if (r.status === 401) { sessionLost(); throw new ApiError(401, null); }
      if (r.status < 200 || r.status >= 300) throw new ApiError(r.status, r.data);
      return r.data;
    });
  }

  /* keep a signed-in session warm while a page stays open */
  if (document.body && document.body.dataset.authed === "1") {
    setInterval(() => { refreshSession(); }, 10 * 60 * 1000);
  }

  /* ------------------------------------------------------------ formatting */
  const fmt = {
    clock(seconds) {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
      const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
      return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },
    bytes(n) {
      if (n === null || n === undefined) return "";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let v = Number(n), i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
    },
    ago(iso) {
      if (!iso) return "";
      const then = new Date(iso).getTime();
      if (Number.isNaN(then)) return "";
      const s = Math.round((Date.now() - then) / 1000);
      if (s < 45) return "just now";
      const m = Math.round(s / 60);
      if (m < 60) return `${m} min ago`;
      const h = Math.round(m / 60);
      if (h < 24) return `${h} h ago`;
      const d = Math.round(h / 24);
      if (d < 7) return `${d} day${d === 1 ? "" : "s"} ago`;
      return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    },
    date(iso) {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso || "";
      return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    },
    minutes(seconds) {
      if (!seconds) return "";
      const m = Math.round(seconds / 60);
      return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
    },
  };

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    children.flat().forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  /* inline icons for script-built UI (same paths as partials/icons.html) */
  const ICON_PATHS = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    alert: '<path d="M12 3.5l9 16H3z"/><path d="M12 10v4M12 17v.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
    film: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 9.5v5l4-2.5z"/>',
    video: '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>',
    youtube: '<rect x="2.5" y="5.5" width="19" height="13" rx="3.5"/><path d="M10 9.5v5l4.5-2.5z"/>',
    upload: '<path d="M12 16V5M7 10l5-5 5 5M5 20h14"/>',
    file: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    scissors: '<circle cx="6" cy="7" r="3"/><circle cx="6" cy="17" r="3"/><path d="M8.6 8.6L20 19M8.6 15.4L20 5"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    box: '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3"/>',
    refresh: '<path d="M20 11a8 8 0 10-2.34 5.66M20 4v7h-7"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l3 3M14 9l2 2"/>',
    shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    zap: '<path d="M13 3L5 13.5h6.5L10 21l8-10.5h-6.5z"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  };
  function icon(name) {
    const span = document.createElement("span");
    span.innerHTML = `<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICON_PATHS[name] || ""}</svg>`;
    return span.firstChild;
  }

  /* ------------------------------------------------------------ toasts */
  function toast(message, type = "success") {
    const box = qs("#toasts");
    if (!box) return;
    const node = el("div", { class: `toast${type === "error" ? " is-error" : ""}`, role: type === "error" ? "alert" : "status" },
      icon(type === "error" ? "alert" : "check"), el("div", { text: message }));
    box.appendChild(node);
    if (Motion && !reduced) Motion.animate(node, { opacity: [0, 1], y: [8, 0] }, { duration: 0.2, ease: EASE_OUT });
    setTimeout(async () => {
      if (Motion && !reduced) await Motion.animate(node, { opacity: 0, y: 6 }, { duration: 0.15, ease: [0.55, 0, 1, 0.45] });
      node.remove();
    }, type === "error" ? 7000 : 4200);
  }

  /* ------------------------------------------------------------ busy buttons */
  function busy(button, on) {
    if (!button) return;
    button.classList.toggle("is-loading", !!on);
    button.disabled = !!on;
    button.setAttribute("aria-busy", on ? "true" : "false");
  }

  function showFormError(box, message) {
    if (!box) { toast(message, "error"); return; }
    box.hidden = !message;
    const text = qs("[data-error-text]", box);
    (text || box).textContent = message || "";
  }

  /* ------------------------------------------------------------ copy */
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const area = el("textarea", { class: "visually-hidden" });
      area.value = text;
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      area.remove();
      return ok;
    }
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy], [data-copy-target], [data-copy-panel]");
    if (!btn) return;
    let target = btn.dataset.copyTarget ? qs(btn.dataset.copyTarget) : null;
    if (btn.hasAttribute("data-copy-panel")) {
      /* the visible tab panel of the surrounding code card */
      target = qs('[role="tabpanel"]:not([hidden])', btn.closest(".code-card") || document);
    }
    const text = target ? (target.value !== undefined && target.tagName !== "PRE" && target.tagName !== "CODE" ? target.value : target.textContent) : btn.dataset.copy;
    if (!text) return;
    const ok = await copyText(text.trim());
    const label = qs("[data-copy-label]", btn);
    const before = label ? label.textContent : null;
    btn.classList.toggle("is-copied", ok);
    if (label) label.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { btn.classList.remove("is-copied"); if (label) label.textContent = before; }, 1600);
  });

  /* ------------------------------------------------------------ tabs */
  function setupTabs(list) {
    const tabs = qsa('[role="tab"]', list);
    const select = (tab, focus) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
      list.dispatchEvent(new CustomEvent("tabchange", { detail: { tab } }));
    };
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        select(next, true);
      });
    });
    const initial = tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0];
    if (initial) select(initial, false);
  }
  qsa('[role="tablist"][data-tabs]').forEach(setupTabs);

  /* ------------------------------------------------------------ theme */
  function applyTheme(theme) {
    if (!reduced) {
      root.classList.add("theme-easing");
      setTimeout(() => root.classList.remove("theme-easing"), 250);
    }
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem("hc-theme", theme); } catch (e) { /* not important */ }
    qsa("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.themeSet === theme ? "true" : "false"));
    const meta = qs('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#ffffff" : "#0a0a0b");
  }
  qsa("[data-theme-set]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.themeSet === root.getAttribute("data-theme") ? "true" : "false");
    b.addEventListener("click", () => applyTheme(b.dataset.themeSet));
  });
  if (root.getAttribute("data-theme") === "dark") {
    const meta = qs('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", "#0a0a0b");
  }

  /* ------------------------------------------------------------ header / nav */
  const header = qs("[data-header]");
  if (header) {
    const onScroll = () => header.classList.toggle("is-scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    const toggle = qs("[data-nav-toggle]", header);
    const panel = qs("#mobile-nav");
    if (toggle && panel) {
      toggle.addEventListener("click", () => {
        const open = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        panel.hidden = !open;
        header.classList.toggle("is-open", open);
      });
      qsa("a", panel).forEach((a) => a.addEventListener("click", () => {
        toggle.setAttribute("aria-expanded", "false");
        panel.hidden = true;
        header.classList.remove("is-open");
      }));
    }
  }

  /* dropdowns (<details class="dropdown">): close on outside click / Escape */
  document.addEventListener("click", (e) => {
    qsa("details.dropdown[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    qsa("details.dropdown[open]").forEach((d) => { d.removeAttribute("open"); qs("summary", d).focus(); });
  });

  /* ------------------------------------------------------------ session actions */
  document.addEventListener("click", async (e) => {
    const out = e.target.closest("[data-logout]");
    if (out) {
      e.preventDefault();
      try { await api("/api/v1/auth/logout", { method: "POST" }); } catch (err) { /* leave anyway */ }
      location.assign("/");
      return;
    }
    const resend = e.target.closest("[data-resend-verification]");
    if (resend) {
      e.preventDefault();
      busy(resend, true);
      try {
        await api("/api/v1/auth/resend-verification", { method: "POST" });
        toast(document.body.dataset.emailOff
          ? "Email delivery isn't set up on this server: the new link is in the server log."
          : "Verification email sent. Check your inbox.");
      } catch (err) {
        toast(err.code === "already_verified" ? "Your email is already verified." : err.message, err.code === "already_verified" ? "success" : "error");
      } finally {
        busy(resend, false);
      }
    }
  });

  /* ------------------------------------------------------------ dialogs */
  function confirmDialog({ title, body, confirm = "Confirm", danger = false }) {
    return new Promise((resolve) => {
      const dialog = el("dialog", { class: "modal", "aria-labelledby": "confirm-title" },
        el("div", { class: "modal-body" },
          el("h2", { class: "h3", id: "confirm-title", text: title }),
          el("p", { class: "muted", style: "margin-top:10px", text: body })),
        el("div", { class: "modal-foot" },
          el("button", { class: "btn btn-ghost", type: "button", value: "cancel", text: "Cancel" }),
          el("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "button", value: "ok", text: confirm })));
      document.body.appendChild(dialog);
      const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      qsa("button", dialog).forEach((b) => b.addEventListener("click", () => done(b.value === "ok")));
      dialog.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
      dialog.showModal();
    });
  }

  /* docs table of contents: highlight the section being read */
  const toc = qs(".docs-toc");
  if (toc && "IntersectionObserver" in window) {
    const links = new Map(qsa('a[href^="#"]', toc).map((a) => [a.getAttribute("href").slice(1), a]));
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        links.forEach((a) => a.classList.remove("is-current"));
        const link = links.get(entry.target.id);
        if (link) link.classList.add("is-current");
      });
    }, { rootMargin: "-30% 0px -60% 0px" });
    qsa(".docs-content section[id]").forEach((s) => observer.observe(s));
  }

  /* short, human status names (dashboard list, job page) */
  const STATUS_TEXT = {
    queued: "Queued", downloading: "Downloading", transcribing: "Transcribing",
    deciding: "AI editing", building_edl: "Planning cuts", slicing: "Rendering", rendering_highlights: "Rendering reel",
    assembling: "Assembling", rendering_removed: "Rendering removed parts", rendering_shorts: "Rendering shorts",
    reporting: "Writing report", bundling: "Packing", done: "Done", failed: "Failed", cancelled: "Cancelled",
    decided: "Picks ready", skipped_desync: "Skipped",
  };
  const TERMINAL = ["done", "failed", "cancelled", "decided", "skipped_desync"];
  function statusPill(status) {
    const running = !TERMINAL.includes(status) && status !== "queued";
    return el("span", { class: `status status-${status}${running ? " is-running" : ""}`, text: STATUS_TEXT[status] || status });
  }

  window.HC = { STATUS_TEXT, TERMINAL, statusPill, api, upload, ApiError, toast, busy, showFormError, fmt, el, icon, qs, qsa, copyText, confirmDialog, reduced, Motion, EASE_OUT, CSRF };
  window.__hcReady = true;
})();
