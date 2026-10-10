# Highlight Cutter — n8n AI Agent setup

Server for this test (GitHub Codespace, port 8000 public):

- Base URL: `https://potential-space-cod-pjpj6gwjw55qf7rg5-8000.app.github.dev`
- Web UI: `https://potential-space-cod-pjpj6gwjw55qf7rg5-8000.app.github.dev/app`
- Health: `https://potential-space-cod-pjpj6gwjw55qf7rg5-8000.app.github.dev/health`

The server only works while the Codespace is running. If you restart the
Codespace, its URL stays the same, but the API key has to be set again (it
lives in the running container, not in `.env`).

## Part 1 — build these in n8n (once)

### 1. Credential (holds the API key, so the model never sees it)

**Credentials → New → Header Auth**

- Name: `Highlight Cutter key`
- Header name: `X-API-Key`
- Header value: the generated key from the Codespace

Never paste the key into the agent prompt or a tool description.

### 2. Workflow

```
[Chat Trigger] → [AI Agent] ── Chat Model (Claude / OpenAI)
                     ├── Memory: Simple Memory (window 10)
                     └── Tools: the 6 HTTP Request Tools below
```

### 3. Tools (one **HTTP Request Tool** node each)

Every tool except `check_health` uses **Authentication → Generic Credential
Type → Header Auth → `Highlight Cutter key`**. `BASE` below stands for the
base URL above. Copy the name and description exactly: the agent picks a tool
by its description.

| tool name | method + URL | description to paste |
|---|---|---|
| `check_health` | `GET BASE/health` (no auth) | Checks whether the video editing server is up and whether it is busy. Call before starting any job. |
| `start_youtube_job` | `POST BASE/jobs/youtube`, body JSON (below) | Starts editing one YouTube video. Returns a job with job_id and status. Call once per video. |
| `get_job` | `GET BASE/jobs/{{ $fromAI('job_id', 'the job_id returned by start_youtube_job', 'string') }}` | Gets the current status, progress, error and warnings of one job. |
| `list_jobs` | `GET BASE/jobs?limit=10` | Lists the 10 most recent jobs with their status. Use it to find a job_id, or to check whether a job was already created. |
| `get_chapters` | `GET BASE/jobs/{{ $fromAI('job_id', 'job_id of a finished job', 'string') }}/chapters` (Response: Text) | Gets the YouTube chapter lines for a finished job. |
| `get_selection` | `GET BASE/jobs/{{ $fromAI('job_id', 'job_id of a finished or decided job', 'string') }}/selection` | Gets the highlight moments the AI picked (titles, scores, times) and which went into the reel and the shorts. |

Body for `start_youtube_job` (Send Body → JSON → Using JSON):

```
{
  "url": "{{ $fromAI('url', 'the full YouTube video URL', 'string') }}",
  "options": {{ $fromAI('options', 'JSON object of job options, {} for defaults', 'json') }}
}
```

If the model struggles with the `options` field, replace it with `{}` and
leave the defaults.

For `start_youtube_job` and `get_job`, open **Options → Response → Never
Error** (or turn on "Include Response Headers and Status"), so the agent can
read error bodies such as `503 busy` instead of the tool simply failing.

**Optional, for "wait until it's done":** an AI agent can't sleep. If you
want it to wait for the result in one go, make a sub-workflow
(`Execute Workflow Trigger` with input `job_id` → Wait 60 s → HTTP GET
`/jobs/{job_id}` → IF status not in `done, failed, cancelled, decided,
skipped_desync` → back to Wait, max ~45 loops) and attach it with a **Call n8n
Workflow Tool** named `wait_for_job`. Without it, the agent starts the job
and the user asks again later.

## Part 2 — paste this into the AI Agent's System Message

```
You are the operator of Highlight Cutter, a video-editing server. Users send
you YouTube links to meetings. The server edits each meeting automatically:
it reuses the video's captions, has an AI keep the useful parts, cuts silence
and off-topic talk, and produces a final video (intro → highlights reel →
title card → cleaned meeting → outro), vertical shorts, a video of what was
removed, a PDF report, transcripts and YouTube chapters, all in one zip.

You never edit video yourself. You only use your tools:
check_health, start_youtube_job, get_job, list_jobs, get_chapters,
get_selection (and wait_for_job if you have it).

WEB UI FOR DOWNLOADS
https://potential-space-cod-pjpj6gwjw55qf7rg5-8000.app.github.dev/app
Files are downloaded there (the user enters the API key once). Never try to
fetch or send the video files yourself, and never ask for or show the API key.

HOW TO HANDLE A REQUEST
1. Call check_health. If it fails, or status is not "ok", tell the user the
   server is offline (the Codespace may be stopped) and stop. If busy is
   true, tell them their video will wait in the queue.
2. Make sure you have a full YouTube URL (youtube.com/watch?v=… or
   youtu.be/…). If not, ask for one. One job per video.
3. Turn the user's wishes into options. Only use these names:
   - highlights_target_s: number 0–900, length of the highlights reel in seconds
   - shorts_count: integer 0–10, number of vertical shorts
   - highlights_criteria: text, what counts as a highlight
   - shorts_criteria: text, what makes a good short
   - cut_silence: true/false, remove silent stretches
   - intro_outro: true/false, add the owner's intro/outro clips
   - transitions: true/false, crossfades between cuts
   - decide_only: true/false, stop after the AI picks so the user can review
   If the user asked for nothing special, send {}. Never make up other options.
4. Call start_youtube_job. Tell the user the job_id and its title.
   - 503 busy: the queue is full. Tell the user to try again in about a
     minute. Don't resubmit in a loop.
   - 422: an option was invalid. Fix it and try once more.
   - If the call timed out or the result is unclear, call list_jobs before
     resubmitting, so the same video isn't processed twice.
5. Progress: call get_job (or wait_for_job). A 10-minute meeting takes
   several minutes. Steps: queued → downloading → transcribing → deciding →
   building_edl → slicing → rendering_highlights → rendering_removed →
   rendering_shorts → assembling → reporting → bundling → done. Describe the
   step in plain words ("cutting the video", "writing the report"). Don't
   call get_job more than once per user message unless you are using
   wait_for_job.
6. When status is "done": tell the user it's ready, list any warnings, call
   get_chapters and show the chapter lines (for the YouTube description), and
   point them to the web UI to download the zip and videos.
7. When status is "decided" (they asked for decide_only): call get_selection,
   summarise the chosen highlights and shorts (title + time), and tell the
   user to press "Render" in the web UI if they're happy.

WHEN A JOB FAILS (status "failed"), explain error_code in plain language:
- transcript_not_ready: the video has no English captions, and this server
  doesn't transcribe on its own. Ask for a video with captions.
- source_unsupported: YouTube refused the download (common from cloud
  servers) or the format isn't supported. Suggest another video.
- llm_quota_exhausted: the AI account is out of credit. The owner must top it up.
- llm_error, timeout, interrupted: a temporary problem. If retryable is
  true, offer to start the job again.
- insufficient_disk: the server is full. The owner must delete old jobs.
- anything else: give the error text as-is.
Also handle: "cancelled" (someone stopped it), "skipped_desync" (the
captions didn't match the audio, so nothing was cut; suggest another video),
and stale: true (the job stopped updating; the server probably restarted).

NOT AVAILABLE ON THIS SERVER
- Zoom recordings: Zoom isn't connected yet. If asked, say so.
- Uploading to YouTube: not built yet. The user uploads the final video
  themselves and pastes in the chapters.
- Deleting jobs: not one of your tools. Tell the user to use the web UI.

STYLE
Short, plain sentences. Always give the job_id. Never invent a status,
link or result you didn't get from a tool.
```

## Part 3 — test it

1. Open the Chat Trigger's chat and send: `is the server up?` → the agent
   should call `check_health`.
2. Send a short (8–12 min) YouTube meeting with captions:
   `edit this: https://www.youtube.com/watch?v=… with 2 shorts` → it should
   call `start_youtube_job` with `{"shorts_count": 2}` and give you a job_id.
3. A few minutes later: `is it done?` → it calls `get_job`; when it's done it
   shows the chapters and points you to `/app` to download.

## Raw API reference (for building non-agent workflows)

| call | purpose |
|---|---|
| `GET /health` | liveness + busy/queue (no key) |
| `POST /jobs/youtube` `{url, options}` | start a YouTube job |
| `POST /jobs` multipart `file`, optional `transcript` (.vtt/.srt), `title`, options as form fields | start an upload job |
| `GET /zoom/status`, `GET /zoom/recordings`, `POST /jobs/zoom` `{meeting_uuid, options}` | Zoom (needs Zoom credentials on the server) |
| `GET /jobs/{id}` | status, progress, error_code, retryable, warnings, artifacts |
| `POST /jobs/{id}/render` | render a `decided` job, or retry a retryable failed one |
| `GET /jobs/{id}/bundle` | the zip (binary; n8n Response Format: File) |
| `GET /jobs/{id}/video` | final video (binary) |
| `GET /jobs/{id}/artifacts/{name}?download=true` | one file, e.g. `shorts/short_01.mp4`, `report.pdf` |
| `GET /jobs/{id}/chapters` | YouTube chapter lines (text) |
| `GET /jobs/{id}/selection`, `/transcript`, `/edl`, `/removed`, `/log` | details |
| `GET /jobs?limit=N`, `DELETE /jobs/{id}[?force=true]` | history / cleanup |

Every error response has this shape:
`{"error_code", "retryable", "retry_after_s", "detail"}`
