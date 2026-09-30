/* Admin page: stats, users, all jobs. */
(function () {
  "use strict";
  const { api, toast, busy, fmt, el, qs, statusPill, confirmDialog } = window.HC;

  async function loadStats() {
    const box = qs("[data-stats]");
    try {
      const s = await api("/api/v1/admin/stats");
      const jobs = Object.values(s.jobs_by_status || {}).reduce((a, b) => a + b, 0);
      const cards = [
        [s.users, `accounts (${s.verified_users} verified)`],
        [s.admins, "admins"],
        [jobs, `jobs (${(s.jobs_by_status || {}).done || 0} done)`],
        [s.queue.depth, `in the queue (max ${s.queue.max_depth})`],
        [s.active_api_keys, "active API keys"],
        [s.disk_free_bytes === null ? "?" : fmt.bytes(s.disk_free_bytes), "free disk"],
      ];
      box.replaceChildren(...cards.map(([value, label]) => el("div", { class: "stat-card" }, el("b", { text: String(value) }), el("span", { text: label }))));
    } catch (err) {
      box.textContent = err.message;
    }
  }

  const usersBody = qs("[data-users]");
  let query = "";
  async function loadUsers() {
    try {
      const { items } = await api(`/api/v1/admin/users?limit=100&q=${encodeURIComponent(query)}`);
      if (!items.length) { usersBody.replaceChildren(el("tr", {}, el("td", { colspan: "7", class: "muted", text: "No users match." }))); return; }
      usersBody.replaceChildren(...items.map((u) => {
        const role = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: u.role === "admin" ? "Make user" : "Make admin" });
        const block = el("button", { class: `btn btn-sm ${u.is_active ? "btn-danger" : "btn-outline"}`, type: "button", text: u.is_active ? "Block" : "Unblock" });
        const patch = async (btn, body, question) => {
          if (question && !(await confirmDialog(question))) return;
          busy(btn, true);
          try { await api(`/api/v1/admin/users/${u.id}`, { method: "PATCH", json: body }); toast("Saved."); loadUsers(); loadStats(); }
          catch (err) { toast(err.message, "error"); busy(btn, false); }
        };
        role.addEventListener("click", () => patch(role, { role: u.role === "admin" ? "user" : "admin" }));
        block.addEventListener("click", () => patch(block, { is_active: !u.is_active }, u.is_active
          ? { title: `Block ${u.email}?`, body: "They are signed out everywhere and can't log in until unblocked. Their API keys stop working.", confirm: "Block", danger: true }
          : null));
        return el("tr", {},
          el("td", {}, el("strong", { text: u.name || u.email }), u.name ? el("div", { class: "row-sub", text: u.email }) : null),
          el("td", {}, el("span", { class: `badge ${u.role === "admin" ? "badge-accent" : ""}`, text: u.role })),
          el("td", {}, el("span", { class: `badge ${u.is_active ? (u.email_verified ? "badge-ok" : "badge-warn") : "badge-cut"}`,
            text: u.is_active ? (u.email_verified ? "active" : "unverified") : "blocked" })),
          el("td", { class: "num", text: String(u.jobs) }),
          el("td", { class: "time", text: u.last_login_at ? fmt.ago(u.last_login_at) : "never" }),
          el("td", { class: "time", text: fmt.ago(u.created_at) }),
          el("td", { class: "actions" }, role, block));
      }));
    } catch (err) {
      usersBody.replaceChildren(el("tr", {}, el("td", { colspan: "7", text: err.message })));
    }
  }
  let searchTimer = null;
  qs("[data-user-search]").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { query = e.target.value.trim(); loadUsers(); }, 250);
  });

  async function loadJobs() {
    const body = qs("[data-all-jobs]");
    try {
      const { items } = await api("/api/v1/admin/jobs?limit=100");
      if (!items.length) { body.replaceChildren(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No jobs yet." }))); return; }
      body.replaceChildren(...items.map((j) => el("tr", {},
        el("td", {}, el("a", { class: "row-link", href: `/app/jobs/${encodeURIComponent(j.job_id)}`, text: j.title || "Untitled job" }),
          el("div", { class: "row-sub mono", text: j.job_id.slice(0, 12) })),
        el("td", { class: "nowrap", text: j.owner_email || "—" }),
        el("td", {}, statusPill(j.status)),
        el("td", { text: { youtube: "YouTube", upload: "Upload" }[j.source_kind] || j.source_kind }),
        el("td", { class: "time", text: fmt.ago(j.created_at) }))));
    } catch (err) {
      body.replaceChildren(el("tr", {}, el("td", { colspan: "5", text: err.message })));
    }
  }

  loadStats();
  loadUsers();
  loadJobs();
})();
