/* Dashboard: new job (YouTube link or file upload) and the jobs list. */
(function () {
  "use strict";
  const { api, upload, toast, busy, showFormError, fmt, el, icon, qs, qsa, statusPill, TERMINAL } = window.HC;

  const form = qs("[data-new-job]");
  const errorBox = qs("[data-form-error]", form);
  const submitBtn = qs("[data-submit]", form);
  const tabsEl = qs("[data-source-tabs]");
  let source = (qs('[role="tab"][aria-selected="true"]', tabsEl) || {}).dataset?.source || "youtube";

  /* ------------------------------------------------------------ errors */
  function startErrorMessage(err) {
    switch (err.code) {
      case "busy": return "The queue is full right now. Try again in a minute.";
      case "too_many_jobs": return "You already have the maximum number of jobs waiting or running. Wait for one to finish.";
      case "plan_limit": return err.message;
      case "email_not_verified": return "Confirm your email address first: use the link we sent you (or resend it from the banner above).";
      case "webhook_url_invalid": return `Webhook URL: ${err.message}`;
      case "validation_error": return `Check the options: ${err.message}`;
      default: return err.message || "Couldn't start the job.";
    }
  }

  /* ------------------------------------------------------------ source tabs */
  tabsEl.addEventListener("tabchange", (e) => { source = e.detail.tab.dataset.source; showFormError(errorBox, ""); });

  /* ------------------------------------------------------------ options */
  function options() {
    const f = form.elements;
    const o = { decide_only: f.decide_only.checked, cut_silence: f.cut_silence.checked, transitions: f.transitions.checked };
    const reel = parseFloat(f.highlights_minutes.value);
    if (!Number.isNaN(reel)) o.highlights_target_s = Math.round(reel * 60);
    const shorts = parseInt(f.shorts_count.value, 10);
    if (!Number.isNaN(shorts)) o.shorts_count = shorts;
    const hc = f.highlights_criteria.value.trim();
    if (hc) o.highlights_criteria = hc;
    const sc = f.shorts_criteria.value.trim();
    if (sc) o.shorts_criteria = sc;
    return o;
  }
  const callbackUrl = () => form.elements.callback_url.value.trim() || null;
  const openJob = (job) => location.assign(`/app/jobs/${encodeURIComponent(job.job_id)}`);

  /* ------------------------------------------------------------ files */
  const files = { file: null, transcript: null };
  qsa("[data-dropzone]").forEach((zone) => {
    const kind = zone.dataset.dropzone;
    const input = qs(`[data-file-input="${kind}"]`);
    const label = qs("[data-file-label]", zone);
    const placeholder = label.textContent;
    const set = (file) => {
      files[kind] = file || null;
      label.textContent = file ? `${file.name}, ${fmt.bytes(file.size)}` : placeholder;
      zone.classList.toggle("has-file", !!file);
    };
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => set(input.files[0]));
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("is-over"); }));
    zone.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) set(f); });
  });

  /* ------------------------------------------------------------ submit (YouTube / upload) */
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError(errorBox, "");
    if (source === "youtube") {
      const url = qs("#yt-url").value.trim();
      if (!url) { showFormError(errorBox, "Paste a YouTube link first."); qs("#yt-url").focus(); return; }
      busy(submitBtn, true);
      try {
        openJob(await api("/api/v1/jobs/youtube", { method: "POST", json: { url, options: options(), callback_url: callbackUrl() } }));
      } catch (err) {
        showFormError(errorBox, startErrorMessage(err));
        busy(submitBtn, false);
      }
      return;
    }
    if (source === "upload") {
      if (!files.file) { showFormError(errorBox, "Choose a recording to upload first."); return; }
      const data = new FormData();
      data.append("file", files.file);
      if (files.transcript) data.append("transcript", files.transcript);
      Object.entries(options()).forEach(([k, v]) => data.append(k, String(v)));
      const title = qs("#up-title").value.trim();
      if (title) data.append("title", title);
      const cb = callbackUrl();
      if (cb) data.append("callback_url", cb);
      const box = qs("[data-upload-progress]");
      const bar = qs(".progress > span", box);
      const label = qs("[data-upload-label]", box);
      box.hidden = false;
      busy(submitBtn, true);
      try {
        openJob(await upload("/api/v1/jobs", data, (p) => {
          bar.style.width = `${Math.round(p * 100)}%`;
          label.textContent = p < 1 ? `Uploading… ${Math.round(p * 100)}% of ${fmt.bytes(files.file.size)}` : "Uploaded. Starting the job…";
        }));
      } catch (err) {
        box.hidden = true;
        bar.style.width = "0";
        showFormError(errorBox, startErrorMessage(err));
        busy(submitBtn, false);
      }
    }
  });

  /* ------------------------------------------------------------ usage */
  async function loadUsage() {
    const box = qs("[data-usage]");
    try {
      const u = await api("/api/v1/account/usage");
      const month = u.limits && u.limits.jobs_per_month ? `${u.jobs_this_month} / ${u.limits.jobs_per_month}` : String(u.jobs_this_month);
      box.textContent = "";
      [[month, "jobs this month"], [String(u.jobs_active), "in progress"], [String(u.minutes_processed), "minutes cut"]].forEach(([v, label]) => {
        box.appendChild(el("span", {}, el("b", { text: v }), label));
      });
    } catch (err) {
      box.textContent = "";
    }
  }

  /* ------------------------------------------------------------ jobs list */
  const list = qs("[data-jobs]");
  const more = qs("[data-jobs-more]");
  const PAGE = 12;
  let filter = "";
  let shown = 0;
  let pollTimer = null;
  const SOURCE = { youtube: ["youtube", "YouTube"], upload: ["upload", "Upload"] };

  function jobCard(item) {
    const [iconName, sourceLabel] = SOURCE[item.source_kind] || ["film", "Job"];
    const bits = [sourceLabel, fmt.ago(item.created_at)];
    if (item.source_duration_s) bits.push(fmt.minutes(item.source_duration_s));
    const end = el("div", { class: "end" }, statusPill(item.status));
    if (!TERMINAL.includes(item.status)) {
      if (item.queue_position > 0) end.appendChild(el("span", { class: "tiny subtle", text: `#${item.queue_position} in line` }));
      else if (item.progress_total > 0) {
        const pct = Math.round((item.progress_current / item.progress_total) * 100);
        end.appendChild(el("div", { class: "progress" }, el("span", { style: `width:${pct}%` })));
      }
    }
    return el("a", { class: "job-row", href: `/app/jobs/${encodeURIComponent(item.job_id)}` },
      el("span", { class: "icon-tile" }, icon(iconName)),
      el("div", { style: "min-width:0" },
        el("strong", { text: item.title || "Untitled job" }),
        el("div", { class: "row-sub" }, bits.filter(Boolean).map((b) => el("span", { text: b })))),
      end);
  }

  function emptyState() {
    const text = filter ? "Nothing here with this filter." : "Your cut meetings will show up here. Start with a YouTube link or a recording from your computer.";
    return el("div", { class: "empty" }, el("span", { class: "icon-tile" }, icon("film")),
      el("h3", { text: filter ? "No matching jobs" : "No jobs yet" }), el("p", { class: "small", text }));
  }

  async function loadJobs(reset) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const offset = reset ? 0 : shown;
    const limit = reset ? Math.max(PAGE, shown) : PAGE;
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (filter) params.set("status", filter);
    let body;
    try {
      body = await api(`/api/v1/jobs?${params}`);
    } catch (err) {
      if (reset && !shown) list.replaceChildren(el("div", { class: "alert alert-danger" }, icon("alert"), el("span", { text: err.message })));
      pollTimer = setTimeout(() => loadJobs(true), 10000);
      return;
    }
    const cards = body.items.map(jobCard);
    if (reset) {
      list.replaceChildren(...(cards.length ? cards : [emptyState()]));
      shown = body.items.length;
    } else {
      cards.forEach((c) => list.appendChild(c));
      shown += body.items.length;
    }
    list.setAttribute("aria-busy", "false");
    more.hidden = shown >= body.total;
    if (body.items.some((i) => !TERMINAL.includes(i.status))) pollTimer = setTimeout(() => loadJobs(true), 4000);
  }

  more.addEventListener("click", () => loadJobs(false));
  qsa("[data-job-filter] [data-status]").forEach((btn) => {
    btn.addEventListener("click", () => {
      qsa("[data-job-filter] [data-status]").forEach((b) => b.setAttribute("aria-selected", b === btn ? "true" : "false"));
      filter = btn.dataset.status;
      shown = 0;
      loadJobs(true);
    });
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadJobs(true); });

  const params = new URLSearchParams(location.search);
  if (params.get("welcome")) {
    toast("Account created. Check your inbox to confirm your email address.");
    history.replaceState(null, "", "/app");
  }
  /* arriving from the home page: a pasted YouTube link, or "upload a recording" */
  const pasted = params.get("url");
  if (pasted) qs("#yt-url").value = pasted;
  if (params.get("source") === "upload") qs("#tab-upload").click();
  if (pasted || params.get("source")) {
    history.replaceState(null, "", "/app#new");
    qs("#new").scrollIntoView({ block: "start" });
    (pasted ? submitBtn : qs('[data-dropzone="file"]')).focus({ preventScroll: true });
  } else if (location.hash === "#new") qs("#new").scrollIntoView({ block: "start" });

  loadUsage();
  loadJobs(true);
})();
