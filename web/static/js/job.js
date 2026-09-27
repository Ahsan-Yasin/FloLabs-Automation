/* Job page: live progress, the AI's picks, and every output. */
(function () {
  "use strict";
  const { api, toast, busy, fmt, el, icon, qs, qsa, statusPill, TERMINAL, confirmDialog, STATUS_TEXT } = window.HC;

  const root = qs("[data-job]");
  if (!root) return;
  const jobId = root.dataset.job;
  const base = `/api/v1/jobs/${encodeURIComponent(jobId)}`;
  const panel = (name) => qs(`[data-panel="${name}"]`);
  const show = (node, on) => { if (node) node.hidden = !on; };

  const STEP_LABELS = {
    queued: "Queued",
    downloading: "Downloading the recording",
    transcribing: "Reading the transcript",
    deciding: "AI marking up the transcript",
    building_edl: "Planning the cuts",
    slicing: "Rendering the cleaned meeting",
    rendering_highlights: "Rendering the highlights reel",
    assembling: "Joining reel and meeting",
    rendering_removed: "Rendering the removed parts",
    rendering_shorts: "Rendering shorts",
    reporting: "Transcripts, chapters and report",
    bundling: "Packing the zip",
    done: "Done",
  };
  const WITH_DOWNLOAD = ["queued", "downloading", "transcribing", "deciding", "building_edl", "slicing",
    "rendering_highlights", "assembling", "rendering_removed", "rendering_shorts", "reporting", "bundling", "done"];
  const NO_DOWNLOAD = WITH_DOWNLOAD.filter((s) => s !== "downloading");
  const PAST_TRANSCRIBING = WITH_DOWNLOAD.slice(WITH_DOWNLOAD.indexOf("deciding")).concat(["decided"]);
  const PROGRESS_UNITS = {
    downloading: "MB downloaded", deciding: "sentences judged", slicing: "render steps", rendering_highlights: "render steps",
    rendering_removed: "render steps", rendering_shorts: "shorts rendered",
  };
  const TRANSCRIPT_SOURCES = {
    asr: "Transcribed from scratch (no platform transcript).",
    youtube_captions: "Reused YouTube's own captions: no transcription needed.",
    uploaded_transcript: "Reused your uploaded transcript: no transcription needed.",
    zoom_transcript: "Reused the meeting platform's transcript: no transcription needed.",
  };
  const WORKING = {
    queued: ["Waiting in line", "One job renders at a time. Yours starts as soon as the one ahead of it finishes."],
    downloading: ["Fetching the recording", "Downloading the video and its transcript."],
    transcribing: ["Reading the transcript", "Splitting the transcript into sentences and speakers."],
    deciding: ["The AI is marking up the transcript", "Every sentence gets a keep-or-cut decision and a highlight score."],
    building_edl: ["Planning the cuts", "Snapping every cut to the video's frames and shortening silences."],
    slicing: ["Rendering the cleaned meeting", "Frame-exact cuts with soft dissolves. This is the longest step."],
    rendering_highlights: ["Rendering the highlights reel", "The moments worth learning from, joined into a reel."],
    assembling: ["Assembling the final video", "Reel, title card, then the cleaned meeting."],
    rendering_removed: ["Rendering the removed parts", "Every cut, labelled with its time and reason."],
    rendering_shorts: ["Rendering shorts", "Vertical clips with captions."],
    reporting: ["Writing the report", "Transcripts, chapters and the PDF report."],
    bundling: ["Packing the zip", "Almost there."],
  };
  const CATEGORY = { funny: "Funny", new_architecture: "Architecture", new_feature: "New feature", concept: "Concept",
    decision: "Decision", insight: "Insight", none: "" };
  const ARTIFACT_LABELS = {
    "final.mp4": "Final video", "highlights.mp4": "Highlights reel", "cleaned.mp4": "Cleaned meeting",
    "removed.mp4": "Removed parts", "report.pdf": "Report (PDF)", "transcript_clean.txt": "Transcript",
    "transcript_clean.json": "Transcript (JSON)", "transcript_removed.txt": "Removed text",
    "transcript_removed.json": "Removed text (JSON)", "chapters.txt": "YouTube chapters", "manifest.json": "Manifest",
    "shorts/shorts.json": "Shorts index",
  };

  let job = null;
  let resultsShown = false;
  let picksShown = null;
  let timer = null;

  const artifactUrl = (name, download) =>
    `${base}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}${download ? "?download=true" : ""}`;
  const has = (name) => {
    const a = (job.artifacts || {})[name];
    return a && a.status === "ok" && a.on_disk !== false;
  };

  /* ------------------------------------------------------------ polling */
  async function poll() {
    clearTimeout(timer);
    try {
      job = await api(base);
    } catch (err) {
      if (err.status === 404) {
        showError("This job no longer exists", "It was deleted, or it belongs to another account.");
        show(panel("working"), false);
        return;
      }
      timer = setTimeout(poll, 8000);
      return;
    }
    render();
    if (!TERMINAL.includes(job.status)) timer = setTimeout(poll, job.status === "queued" ? 5000 : 2500);
  }

  function showError(title, text) {
    const box = qs("[data-job-error]");
    qs("[data-error-title]", box).textContent = title;
    qs("[data-error-text]", box).textContent = text || "";
    box.hidden = false;
  }

  /* ------------------------------------------------------------ render */
  function render() {
    const title = job.title || "Untitled job";
    qs("[data-job-title]").textContent = title;
    document.title = `${title} — ${document.title.split(" — ").pop()}`;
    qs("[data-job-status]").replaceChildren(statusPill(job.status));
    const sourceLabel = job.source_url ? "YouTube" : "Upload";
    qs("[data-job-source]").textContent = sourceLabel;
    qs("[data-job-created]").textContent = job.created_at ? `Started ${fmt.date(job.created_at)}` : "";
    qs("[data-job-duration]").textContent = job.source_duration_s ? `${fmt.clock(job.source_duration_s)} long` : "";

    const running = !TERMINAL.includes(job.status);
    const bundle = qs('[data-action="bundle"]');
    bundle.hidden = !(job.status === "done" && job.links && job.links.bundle);
    bundle.href = `${base}/bundle`;
    show(qs('[data-action="render"]'), !!job.can_render);
    qs("[data-delete-label]").textContent = running && job.status !== "queued" ? "Cancel job" : "Delete";

    // errors and warnings
    const errorBox = qs("[data-job-error]");
    if (job.status === "failed" || job.status === "cancelled" || job.status === "skipped_desync") {
      const title = job.status === "cancelled" ? "The job was cancelled"
        : job.status === "skipped_desync" ? "Skipped: the audio and video are out of sync" : "This job failed";
      let text = job.error || "";
      if (job.error_code) text += `${text ? " " : ""}(${job.error_code})`;
      if (job.status === "failed") text += job.can_render ? " Its picks were saved, so you can render it again without new AI calls."
        : job.retryable ? " This is usually temporary: submit it again." : "";
      showError(title, text);
    } else {
      errorBox.hidden = true;
    }
    const warnings = ["decided", "done", "failed"].includes(job.status) ? (job.warnings || []) : [];
    const wbox = qs("[data-job-warnings]");
    qs("[data-warnings-list]", wbox).replaceChildren(...warnings.map((w) => el("div", { text: w })));
    wbox.hidden = !warnings.length;

    renderStages();
    renderWorking(running);

    if (job.status === "decided" || (job.status === "failed" && job.can_render)) {
      if (picksShown !== "render") { picksShown = "render"; renderPicks(true); }
    }
    if (job.status === "done" && !resultsShown) {
      resultsShown = true;
      renderResults();
    }
    if (!running && job.callback_url) renderWebhooks();
  }

  function renderStages() {
    const list = qs("[data-stages]");
    const steps = job.source_url ? WITH_DOWNLOAD : NO_DOWNLOAD;
    const failed = ["failed", "cancelled", "skipped_desync"].includes(job.status);
    const current = job.status === "decided" ? steps.indexOf("slicing") : steps.indexOf(job.status);
    list.replaceChildren(...steps.map((step, i) => {
      let cls = "stage";
      if (job.status === "done" || (current > i && !failed)) cls += " done";
      else if (i === current && job.status !== "decided") cls += " active";
      const li = el("li", { class: cls }, el("span", { class: "dot" }, icon("check")), el("span", { text: STEP_LABELS[step] }));
      if (i === current && job.status !== "decided" && job.status !== "done") li.setAttribute("aria-current", "step");
      return li;
    }));
    if (failed) {
      list.appendChild(el("li", { class: "stage failed" }, el("span", { class: "dot" }), el("span", { text: STATUS_TEXT[job.status] })));
    }
    const unit = PROGRESS_UNITS[job.status];
    const box = qs("[data-stage-progress]");
    if (unit && job.progress_total > 0) {
      const pct = Math.round((job.progress_current / job.progress_total) * 100);
      qs("[data-stage-progress-label]", box).textContent = `${job.progress_current} of ${job.progress_total} ${unit} (${pct}%)`;
      qs("[data-stage-progress-fill]", box).style.width = `${pct}%`;
      box.hidden = false;
    } else {
      box.hidden = true;
    }
    const note = qs("[data-transcript-source]");
    const label = PAST_TRANSCRIBING.includes(job.status) && TRANSCRIPT_SOURCES[job.transcript_source];
    note.textContent = label || "";
    note.hidden = !label;
  }

  function renderWorking(running) {
    const card = panel("working");
    show(card, running);
    if (!running) return;
    const [title, text] = WORKING[job.status] || ["Working", ""];
    qs("[data-working-title]", card).textContent = job.status === "queued" && job.queue_position > 1
      ? `Waiting in line (#${job.queue_position})` : title;
    qs("[data-working-text]", card).textContent = job.stale
      ? "No progress for a while. The server may have restarted; if nothing changes, submit the job again."
      : `${text} You can close this page: we'll email you when it's done.`;
    const bar = qs("[data-working-progress]", card);
    if (job.progress_total > 0) {
      bar.classList.remove("is-indeterminate");
      qs("span", bar).style.width = `${Math.round((job.progress_current / job.progress_total) * 100)}%`;
    } else {
      bar.classList.add("is-indeterminate");
      qs("span", bar).style.width = "";
    }
  }

  /* ------------------------------------------------------------ results */
  function renderResults() {
    const video = qs("[data-final-video]");
    if (has("final.mp4")) {
      video.src = artifactUrl("final.mp4");
      const offset = job.final_offset_s || 0;
      qs("[data-final-note]").textContent = offset > 0
        ? `Opens with the highlights reel; the full meeting starts at ${fmt.clock(offset)}.`
        : "The cleaned meeting (not enough standout moments for a highlights reel).";
      show(panel("video"), true);
    }
    if (has("removed.mp4")) {
      qs("[data-removed-video]").src = artifactUrl("removed.mp4");
      const secs = job.artifacts["removed.mp4"].duration_s;
      qs("h2", panel("removed")).textContent = `Removed parts${secs ? ` (${fmt.clock(secs)})` : ""}`;
      show(panel("removed"), true);
    }
    renderShorts();
    renderDownloads();
    renderChapters();
    renderMarkedTranscript();
    if (job.links && job.links.selection) renderPicks(false);
  }

  function renderShorts() {
    const names = Object.keys(job.artifacts || {}).filter((n) => n.startsWith("shorts/") && n.endsWith(".mp4") && has(n));
    if (!names.length) return;
    const grid = qs("[data-shorts]");
    grid.replaceChildren(...names.map((name) => el("figure", { class: "short-card", "data-name": name, style: "margin:0" },
      el("video", { controls: true, preload: "metadata", playsinline: true, src: artifactUrl(name) }),
      el("strong", { "data-short-title": true, text: name.replace("shorts/", "") }),
      el("a", { class: "link small", href: artifactUrl(name, true), download: true, text: "Download" }))));
    show(panel("shorts"), true);
    if (has("shorts/shorts.json")) {
      api(artifactUrl("shorts/shorts.json")).then((list) => {
        qsa("figure[data-name]", grid).forEach((fig) => {
          const item = (list || []).find((s) => s.file === fig.dataset.name);
          if (item && item.title) qs("[data-short-title]", fig).textContent = item.title;
        });
      }).catch(() => {});
    }
  }

  function renderDownloads() {
    const list = qs("[data-artifacts]");
    const rows = [];
    if (job.links && job.links.bundle) {
      rows.push(el("a", { class: "artifact", href: `${base}/bundle`, download: true },
        icon("box"), el("span", { class: "name", text: "Everything (.zip)" }), el("span", { class: "meta", text: fmt.bytes(job.bundle_bytes) })));
    }
    Object.entries(job.artifacts || {}).forEach(([name, info]) => {
      if (name.startsWith("shorts/") && name.endsWith(".srt")) return;
      const shortNo = /^shorts\/short_(\d+)\.mp4$/.exec(name);
      const label = ARTIFACT_LABELS[name] || (shortNo ? `Short ${Number(shortNo[1])}` : name.replace("shorts/", ""));
      const iconName = name.endsWith(".mp4") ? "film" : name.endsWith(".pdf") ? "file" : "list";
      if (info.status !== "ok") {
        rows.push(el("div", { class: "artifact is-missing", title: info.reason || info.status },
          icon("x"), el("span", { class: "name", text: label }), el("span", { class: "meta", text: info.status })));
        return;
      }
      const meta = [info.duration_s ? fmt.clock(info.duration_s) : "", fmt.bytes(info.bytes)].filter(Boolean);
      if (info.on_disk === false) {
        rows.push(el("div", { class: "artifact is-missing", title: "Only inside the zip" },
          icon(iconName), el("span", { class: "name", text: label }), el("span", { class: "meta", text: "in the zip" })));
        return;
      }
      rows.push(el("a", { class: "artifact", href: artifactUrl(name, !name.endsWith(".pdf")), ...(name.endsWith(".pdf") ? { target: "_blank", rel: "noopener" } : { download: true }) },
        icon(iconName), el("span", { class: "name", text: label }), el("span", { class: "meta" }, meta.map((m) => el("span", { text: m })))));
    });
    list.replaceChildren(...rows);
    show(panel("downloads"), rows.length > 0);
  }

  async function renderChapters() {
    let text;
    try {
      text = await api(`${base}/chapters`, { quiet401: true });
    } catch (err) {
      return;
    }
    if (typeof text !== "string" || !text.trim()) return;
    qs("[data-chapters]").value = text.trim();
    show(panel("chapters"), true);
    const video = qs("[data-final-video]");
    const list = qs("[data-chapter-list]");
    const items = text.trim().split(/\r?\n/).map((line) => {
      const m = line.match(/^(\d+(?::\d{2}){1,2})\s+(.+)$/);
      if (!m) return null;
      const seconds = m[1].split(":").map(Number).reduce((acc, n) => acc * 60 + n, 0);
      const btn = el("button", { type: "button" }, el("span", { class: "ts", text: m[1] }), el("span", { text: m[2] }));
      btn.addEventListener("click", () => { video.currentTime = seconds; video.play().catch(() => {}); });
      return el("li", {}, btn);
    }).filter(Boolean);
    list.replaceChildren(...items);
  }

  async function renderMarkedTranscript() {
    let words, edl;
    try {
      [words, edl] = await Promise.all([api(`${base}/transcript?clean=false`), api(`${base}/edl`)]);
    } catch (err) {
      return;  // a bonus view; the transcripts are in the downloads
    }
    if (!Array.isArray(words) || !words.length) return;
    const ranges = (edl && edl.ranges) || [];
    const kept = (w) => ranges.some((r) => w.start >= r.start && w.end <= r.end);
    const box = qs("[data-marked]");
    const frag = document.createDocumentFragment();
    words.forEach((w) => frag.appendChild(el("span", { class: `w ${kept(w) ? "kept" : "cut"}`, text: `${w.word} ` })));
    box.replaceChildren(frag);
    show(panel("transcript"), true);
  }

  async function renderPicks(canRender) {
    let sel;
    try {
      sel = await api(`${base}/selection`);
    } catch (err) {
      return;
    }
    const moments = (sel.moments || []).slice().sort((a, b) => b.score - a.score || a.start - b.start);
    const reelS = (sel.highlights && sel.highlights.total_s) || 0;
    const reelCount = moments.filter((m) => m.in_highlights).length;
    qs("[data-picks-summary]").textContent =
      (reelS ? `Highlights reel: ${fmt.clock(reelS)} from ${reelCount} moments.` : "No highlights reel (not enough standout moments).") +
      ` Shorts: ${(sel.shorts || []).length}. ${moments.length} candidate moments, best first.`;
    qs("[data-picks-body]").replaceChildren(...moments.map((m) => {
      const used = el("td", {});
      if (m.in_highlights) used.appendChild(el("span", { class: "badge badge-keep", text: "reel" }));
      if (m.in_shorts) used.appendChild(el("span", { class: "badge", text: "short", style: "margin-left:4px" }));
      return el("tr", {},
        el("td", { class: "time", text: `${fmt.clock(m.start)}–${fmt.clock(m.end)}` }),
        el("td", { class: "num", text: String(Math.round(m.score)) }),
        el("td", {}, el("div", {}, m.title || "(untitled)", CATEGORY[m.category] ? el("span", { class: "badge", style: "margin-left:8px", text: CATEGORY[m.category] }) : null),
          m.hook ? el("div", { class: "row-sub", text: m.hook }) : null),
        used);
    }));
    show(qs('[data-action="render-picks"]'), canRender);
    show(panel("picks"), true);
  }

  async function renderWebhooks() {
    let body;
    try {
      body = await api(`${base}/webhooks`);
    } catch (err) {
      return;
    }
    const items = body.items || [];
    const box = qs("[data-webhooks]");
    if (!items.length) {
      box.replaceChildren(el("p", { class: "small muted", text: "Queued: the webhook is sent within a minute." }));
    } else {
      box.replaceChildren(...items.map((d) => {
        const tone = d.state === "delivered" ? "badge-ok" : d.state === "pending" ? "badge-warn" : "badge-cut";
        const detail = d.state === "delivered" ? `HTTP ${d.status_code}` : d.state === "pending"
          ? `retrying ${d.next_attempt_at ? fmt.ago(d.next_attempt_at).replace(" ago", "") : "soon"}${d.error ? `: ${d.error}` : ""}` : (d.error || "gave up");
        return el("div", { class: "artifact" }, icon("zap"),
          el("span", { class: "name", text: d.event }),
          el("span", { class: "meta" }, el("span", { class: `badge ${tone}`, text: d.state }), ` ${detail}`));
      }));
      if (items.some((d) => d.state === "pending")) setTimeout(renderWebhooks, 30000);
    }
    show(panel("webhooks"), true);
  }

  /* ------------------------------------------------------------ actions */
  async function renderNow(button) {
    busy(button, true);
    try {
      await api(`${base}/render`, { method: "POST" });
      toast("Rendering started.");
      picksShown = null;
      resultsShown = false;
      show(panel("picks"), false);
      qs("[data-job-error]").hidden = true;
      poll();
    } catch (err) {
      toast(err.code === "busy" ? "The queue is full right now. Try again in a minute." : err.message, "error");
    } finally {
      busy(button, false);
    }
  }
  qs('[data-action="render"]').addEventListener("click", (e) => renderNow(e.currentTarget));
  qs('[data-action="render-picks"]').addEventListener("click", (e) => renderNow(e.currentTarget));

  qs('[data-action="delete"]').addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const running = job && !TERMINAL.includes(job.status) && job.status !== "queued";
    const ok = await confirmDialog(running
      ? { title: "Cancel this job?", body: "It stops at its next checkpoint and everything it made so far is deleted.", confirm: "Cancel and delete", danger: true }
      : { title: "Delete this job?", body: "The recording, the outputs and the log are deleted for good.", confirm: "Delete", danger: true });
    if (!ok) return;
    busy(button, true);
    try {
      await api(`${base}${running ? "?force=true" : ""}`, { method: "DELETE" });
      toast(running ? "Cancelling the job." : "Job deleted.");
      setTimeout(() => location.assign("/app"), 600);
    } catch (err) {
      toast(err.code === "protected" ? "This job is protected and can't be deleted." : err.message, "error");
      busy(button, false);
    }
  });

  const logBox = qs("[data-log]");
  logBox.addEventListener("toggle", async () => {
    if (!logBox.open) return;
    const view = qs("[data-log-view]", logBox);
    try {
      const text = await api(`${base}/log`);
      view.textContent = (typeof text === "string" && text.trim()) || "Nothing logged yet.";
      view.scrollTop = view.scrollHeight;
    } catch (err) {
      view.textContent = err.message;
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && job && !TERMINAL.includes(job.status)) poll();
  });
  poll();
})();
