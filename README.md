# Kayi PDF Bot

A production-style Telegram PDF utility bot built with **aiogram 3**, **FastAPI**, **pypdf**, and **Pillow**. It runs on a Telegram webhook, works with in-memory state, and is designed to fit a small Render Free instance without Redis, a database, Celery, or paid APIs.

> **Status:** the source has been implemented and verified locally. The bot has **not** been created with BotFather, the repository has **not** been published, and no Render service or webhook has been deployed. The test suite currently reports **276 passed** (see [Tests](#tests)).

---

## Features

### Five PDF tools

| Tool | What it does |
| --- | --- |
| **Merge PDFs** | Upload 2–10 PDFs, reorder or remove them, then merge in the shown order. Duplicate content is rejected. |
| **Split PDF** | Send one PDF and comma-separated ranges such as `1-3,4-6,7-10`. Each range becomes a separate PDF. Up to 3 files are sent individually; larger results are packed into a ZIP. |
| **Extract Pages** | Keep selected pages, in the requested order, inside one new PDF. Example: `1,3,5-8`. |
| **Rotate Pages** | Rotate all pages or selected pages by 90°, 180°, or 270° clockwise. |
| **Images to PDF** | Send 1–10 JPG/PNG images (or Telegram photos), reorder them, and produce one PDF. EXIF orientation is applied, transparency becomes white, and images are never enlarged. |

### Telegram UX

- Branded welcome menu with the five tools plus **Updates** (`https://t.me/KayiBots`) and **Developer** (`https://t.me/qouba`).
- Optional welcome video via `WELCOME_VIDEO_FILE_ID`, with a text fallback if Telegram rejects the video.
- Send a PDF or image directly and choose an operation immediately.
- Inline queue controls for merging and image conversion: reorder, remove, confirm.
- `/start`, `/start help`, `/help`, `/cancel`, topic-based help, and safe stale-button handling.
- Clear progress, result, validation, and error messages.
- Forced channel subscription with a 45-second positive membership cache, fail-closed behaviour, and restoration of the user's intended destination after verification.
- `/start`, `/cancel`, and the **I've Joined** callback bypass the subscription gate so users can always enter or leave.

### Operational behaviour

- FastAPI webhook endpoint with constant-time secret-token validation.
- Bounded update queue, duplicate update suppression, and a bounded concurrent update worker pool.
- One active PDF job per user, plus a small global waiting queue.
- CPU work runs off the event loop in worker threads with a hard wait limit and cooperative cancellation.
- Per-user serialization prevents concurrent FSM mutations; callbacks fail fast while a task is running.
- Per-user temporary workspaces, generated filenames, no public file serving, and cleanup after success, cancellation, validation failure, or shutdown.
- Deferred cleanup after a timed-out worker actually finishes, so a still-running thread cannot delete a replacement job's files.
- `/`, `/ping`, and `/health` endpoints; optional self-ping for non-Render keep-alive scenarios.
- Structured, level-controlled logging without secrets.

---

## Project structure

```text
PDF bot/
├─ app/
│  ├─ bot.py                 # Bot, dispatcher, middleware, error handler, service wiring
│  ├─ config.py              # Environment parsing, validation, immutable Settings
│  ├─ main.py                # FastAPI app, webhook, health endpoints, lifespan
│  ├─ runtime.py             # Webhook registration, ingress workers, cleanup loops
│  ├─ texts.py               # Telegram HTML copy and result/error messages
│  ├─ handlers/
│  │  ├─ start.py            # /start, /help, help callbacks, FSub verification
│  │  ├─ common.py           # /cancel and main-menu navigation
│  │  ├─ menu.py             # Tool selection callbacks
│  │  ├─ uploads.py          # Direct uploads and operation-choice screen
│  │  ├─ merge.py            # Merge workflow
│  │  ├─ split.py            # Split workflow
│  │  ├─ extract.py          # Extract workflow
│  │  ├─ rotate.py           # Rotate workflow
│  │  ├─ images.py           # Images-to-PDF workflow
│  │  ├─ fallback.py         # Stale/unknown callback handling
│  │  └─ shared.py           # Upload classification, downloads, item helpers
│  ├─ keyboards/main.py      # All inline keyboards
│  ├─ middleware/
│  │  ├─ fsub.py             # Outer forced-subscription gate
│  │  └─ user_lock.py        # Per-user serialization
│  ├─ services/
│  │  ├─ cancellation.py     # Thread + asyncio cooperative cancel token
│  │  ├─ errors.py           # User-safe exception hierarchy
│  │  ├─ files.py            # Bounded downloads, delivery, ZIP, aggregate limits
│  │  ├─ fsub.py             # Membership checks/cache and pending destinations
│  │  ├─ image_ops.py        # Image validation and PDF conversion
│  │  ├─ jobs.py             # Job admission, queueing, timeout, thread offload
│  │  ├─ navigation.py       # Menu opening, help, post-verification restoration
│  │  ├─ page_ranges.py      # Strict page/range parsing
│  │  ├─ pdf_ops.py          # Validate, merge, split, extract, rotate
│  │  ├─ webhook.py          # Bounded ingress and update dedupe
│  │  ├─ welcome.py          # Welcome video with safe text fallback
│  │  └─ workspaces.py       # Isolated temp directories and cleanup
│  ├─ states/workflows.py    # FSM states
│  └─ utils/                 # Logging, Telegram UI helpers, text utilities
├─ tests/                    # 18 test modules (config, handlers, ops, runtime, etc.)
├─ requirements.txt
├─ requirements-dev.txt
├─ render.yaml
├─ .env.example
├─ .python-version
├─ pytest.ini
└─ README.md
```

---

## Architecture

```text
Telegram
   │  HTTPS POST + X-Telegram-Bot-Api-Secret-Token
   ▼
FastAPI  ──►  TelegramRuntime  ──►  bounded asyncio.Queue
   │                │                     │
   │                └── dedupe update_id  ▼
   │                              bounded update workers
   │                                      │
   │                              aiogram Dispatcher
   │                                      │
   │                       subscription middleware (outer)
   │                                      │
   │                       per-user serialization middleware
   │                                      │
   │                              handlers / FSM
   │                                      │
   │                          JobManager admission
   │                                      │
   │                     asyncio.to_thread / run_cpu
   │                                      │
   │                     pypdf / Pillow file operations
   ▼                                      ▼
/health, /ping                  per-user temp workspace
```

Key design points:

1. **Webhook only.** No polling loop. Telegram delivery is acknowledged quickly; processing happens in bounded background tasks.
2. **Backpressure.** The ingress queue and the job queue both have fixed capacity. A full ingress queue returns HTTP 503 with `Retry-After`, so Telegram retries instead of the process growing without bound.
3. **Two levels of concurrency control.** The update semaphore limits how many updates are processed at once; the `JobManager` limits CPU jobs to one active job per user and a small number globally. Per-user locks prevent the same user's FSM from being mutated concurrently.
4. **CPU isolation.** All pypdf/Pillow work runs in worker threads. A 60-second wait limit returns a user-facing timeout; the worker is asked to cancel cooperatively at page/image boundaries. Python cannot forcibly kill a thread, so cleanup is deferred until it really finishes.
5. **Workspace isolation.** Each user gets a generated `job_*` directory under `TEMP_ROOT`. Stored names are generated; user-supplied names are display-only. Path resolution refuses escapes. Workspaces are removed after every terminal path and on shutdown.
6. **Fail closed.** Membership check failures block protected actions with an explanatory message. Unknown Telegram statuses block rather than allow.
7. **Stateless deployment.** FSM state, membership cache, pending actions, and job queues live in memory. A restart loses them; this is intentional for a free single-instance deployment and is documented as a limitation.

---

## Requirements

- Python **3.12**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The bot added as an **administrator** of the forced-subscription channel (required for `getChatMember`)
- A public HTTPS URL for webhooks (Render provides one; for local testing use a tunnel)

The pinned runtime versions are in `requirements.txt`:

```text
aiogram==3.34.0
fastapi==0.165.0
Pillow==12.3.0
pypdf==6.21.4
uvicorn==0.54.0
```

Development tools are in `requirements-dev.txt`:

```text
pytest==9.2.0
pytest-asyncio==1.5.0
httpx==0.28.1
```

---

## Local setup

The application reads environment variables directly. `.env.example` is a reference template; it is **not** loaded automatically.

### Windows PowerShell

```powershell
cd "D:\KayiBots\PDF bot"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

$env:BOT_TOKEN = "123456789:replace-with-your-token"
$env:WEBHOOK_SECRET = "replace-with-a-long-random-secret"
$env:PUBLIC_BASE_URL = "https://your-public-tunnel.example"
$env:FSUB_CHANNEL = "@KayiBots"
$env:FSUB_INVITE_URL = "https://t.me/KayiBots"
$env:LOG_LEVEL = "INFO"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Bash

```bash
cd "D:/KayiBots/PDF bot"
python3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows venv path

export BOT_TOKEN="123456789:replace-with-your-token"
export WEBHOOK_SECRET="replace-with-a-long-random-secret"
export PUBLIC_BASE_URL="https://your-public-tunnel.example"
export FSUB_CHANNEL="@KayiBots"
export FSUB_INVITE_URL="https://t.me/KayiBots"

.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Webhook registration

- If `WEBHOOK_AUTO_SET=true` and a public URL is available, the app registers:

  ```text
  {PUBLIC_BASE_URL}{WEBHOOK_PATH}
  ```

  using `WEBHOOK_SECRET` as Telegram's `secret_token`.
- On Render, `PUBLIC_BASE_URL` may be omitted; `RENDER_EXTERNAL_URL` is used automatically.
- Without a public URL, webhook registration is skipped with a warning. The HTTP endpoints still start, but Telegram cannot deliver updates until a URL is configured.
- If `WEBHOOK_SECRET` is omitted, the app generates an ephemeral secret in memory. Set an explicit secret in production so restarts do not change it.

For local end-to-end testing, expose port 8080 through a tunnel (for example Cloudflare Tunnel or ngrok), set `PUBLIC_BASE_URL` to the tunnel URL, and start the app. The webhook is registered during startup.

Useful endpoints:

```text
GET  /        → service name and status
GET  /ping    → lightweight liveness response
GET  /health  → readiness, uptime, queue and job counters (503 while starting)
POST {WEBHOOK_PATH} → authenticated Telegram webhook
```

---

## Render deployment

`render.yaml` describes a Render Blueprint. It is a deployment definition, **not** evidence that a service exists.

### Via Blueprint

1. Push this directory to a Git repository you control.
2. In Render, create a new **Blueprint** and point it at that repository.
3. Provide `BOT_TOKEN` when prompted (`sync: false`).
4. Render generates `WEBHOOK_SECRET` and injects `RENDER_EXTERNAL_URL`.
5. Confirm the service starts with `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
6. Wait for `/health` to return HTTP 200 before sending Telegram updates.

### Manual web service

| Setting | Value |
| --- | --- |
| Runtime | Python 3.12 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Plan | Free is supported; see limitations |

Set at least:

```text
BOT_TOKEN=<from BotFather>
WEBHOOK_SECRET=<long random value>
FSUB_CHANNEL=@KayiBots
FSUB_INVITE_URL=https://t.me/KayiBots
```

Add the bot as a channel administrator in Telegram. Without that, `getChatMember` can fail and protected actions stay blocked (fail closed).

### Telegram registration checklist

- [ ] Create the bot with BotFather and keep the token private.
- [ ] Add the bot to `@KayiBots` as an administrator.
- [ ] Set `BOT_TOKEN` in the hosting environment.
- [ ] Deploy and confirm `GET /health` returns 200.
- [ ] Confirm the logs say the webhook was registered.
- [ ] Send `/start` from a non-member account and verify the subscription prompt.
- [ ] Join the channel, tap **I've Joined**, and confirm the intended screen returns.
- [ ] Exercise all five tools with small files.

None of these live steps have been performed in this workspace.

---

## Environment variables

### Core

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | required | Telegram bot token. Never commit it. |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` or empty | Public HTTPS base URL used for webhook registration. |
| `WEBHOOK_SECRET` | generated | Secret token Telegram must send; 1–256 chars from `A-Z a-z 0-9 _ -`. |
| `WEBHOOK_PATH` | `/telegram/webhook` | Webhook route. Must start with `/`. |
| `WEBHOOK_AUTO_SET` | `true` | Register the webhook at startup when a public URL exists. |
| `DROP_PENDING_UPDATES` | `false` | Passed to `setWebhook`; drops queued Telegram updates on registration. |
| `LOG_LEVEL` | `INFO` | Logging level. |

### Forced subscription

| Variable | Default | Purpose |
| --- | --- | --- |
| `FSUB_CHANNEL` | `@KayiBots` | Channel username or numeric ID used by `getChatMember`. |
| `FSUB_INVITE_URL` | `https://t.me/KayiBots` | Join button URL. |
| `FSUB_CACHE_TTL` | `45` | Positive membership cache lifetime in seconds. |
| `FSUB_CACHE_MAX` | `10000` | Maximum cached users. |
| `PENDING_ACTION_TTL` | `900` | How long a post-verification destination is remembered. |
| `PENDING_ACTION_MAX` | `10000` | Maximum pending destinations. |
| `WELCOME_VIDEO_FILE_ID` | empty | Optional `file_id` for the welcome video. If invalid, the bot falls back to text. |

### File and job limits

| Variable | Default | Notes |
| --- | --- | --- |
| `MAX_FILE_SIZE_MB` | `20` | Per uploaded file. |
| `MAX_FILES_MERGE` | `10` | Validated to 2–10. |
| `MAX_IMAGES` | `10` | Validated to 1–10. |
| `MAX_PAGES` | `200` | Pages per PDF/source and selected pages. |
| `MAX_TOTAL_PAGES_PER_JOB` | `500` | Merge/aggregate page cap. |
| `MAX_ACTIVE_JOBS` | `1` | Global active CPU jobs. |
| `MAX_QUEUED_JOBS` | `2` | Global waiting jobs. |
| `MAX_TOTAL_INPUT_MB` | `60` | Aggregate input size for merge/images. |
| `PROCESSING_TIMEOUT` | `60` | Seconds before the wait returns a timeout. |
| `MAX_OUTPUT_SIZE_MB` | `45` | Per result file. |
| `MAX_TOTAL_OUTPUT_MB` | `60` | Aggregate outputs/ZIP contents. |
| `MAX_SPLIT_OUTPUTS` | `10` | Output documents per split. |
| `MAX_SPLIT_TOTAL_PAGES` | `500` | Total pages across a split. |
| `SPLIT_INDIVIDUAL_MAX` | `3` | Above this count, results are zipped. |
| `MAX_ZIP_ENTRIES` | `10` | ZIP member cap. |
| `MAX_IMAGE_PIXELS` | `25000000` | Per-image pixel cap. |
| `MAX_IMAGE_DIMENSION` | `6000` | Images above this are downscaled; never upscaled. |

### Workspaces, Transport, Ingress

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_WORKSPACES` | `20` | Maximum live per-user temp directories. |
| `STALE_WORKSPACE_TTL` | `7200` | Stale/orphan directory age in seconds. |
| `WORKSPACE_IDLE_TTL` | `900` | Idle workspace eviction age. |
| `WORKSPACE_CLEANUP_INTERVAL` | `600` | Cleanup loop interval. |
| `TEMP_ROOT` | system temp / `kayi-pdf-bot` | Workspace root. |
| `TELEGRAM_REQUEST_TIMEOUT` | `60` | Telegram API request timeout. |
| `DOWNLOAD_TIMEOUT` | `60` | File download timeout. |
| `WEBHOOK_QUEUE_SIZE` | `100` | Bounded ingress queue. |
| `MAX_CONCURRENT_UPDATES` | `16` | Parallel update tasks. |
| `UPDATE_DEDUP_TTL` | `600` | Duplicate update memory lifetime. |
| `UPDATE_DEDUP_MAX` | `2000` | Duplicate update memory size. |
| `ENABLE_SELF_PING` | `false` | Optional keep-alive loop (not recommended for Render Free). |
| `PING_URL` | empty | URL for self-ping. |
| `SELF_PING_INITIAL_DELAY` | `15` | Seconds before the first ping. |
| `SELF_PING_INTERVAL` | `600` | Seconds between pings. |

Invalid values fail startup with a clear configuration error rather than silently using unsafe settings.

---

## User flow summary

1. `/start` checks membership.
   - Member: welcome video (if configured) or welcome text and the five-tool menu.
   - Non-member: subscription prompt. The intended destination is stored (`start`, `help`, a help topic, a tool, or `resend`).
2. **I've Joined** forces a fresh membership check.
   - Member: the stored destination is reopened. `/start help` returns to the Help Center, never to the welcome video.
   - Still not a member: the prompt remains.
   - Check unavailable: a separate fail-closed message is shown.
3. Direct uploads show an operation-choice screen. Selected files are validated before the next step.
4. Merge and image queues support ordering and removal; jobs run only after explicit confirmation.
5. Split/extract/rotate accept strict page selections. Duplicate pages are rejected for extract/rotate; split allows overlap between separate output ranges.
6. Results are delivered as documents. No result URL is exposed.
7. Every terminal path clears the FSM and removes the user's workspace.

---

## Tests

Run with the project interpreter:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Verified result in this workspace:

```text
276 passed, 1 warning
```

The single warning is a `StarletteDeprecationWarning` from FastAPI/Starlette suggesting `httpx2` for `TestClient`; it does not affect the application or test outcomes.

Coverage areas include:

- environment/config validation and URL/channel normalisation
- text utilities and page/range parsing
- PDF validation, merge, split, extract, rotate, encryption and limits
- image validation, EXIF orientation, transparency, dimensions, output limits
- job admission, queueing, cancellation, timeout cleanup, thread offload
- workspace isolation, naming, capacity, stale/orphan cleanup, detached-workspace safety
- forced-subscription statuses, caching, fail-closed handling, pending destinations
- middleware exemptions, per-user locking, FSub prompt delivery
- downloads, size caps, delivery errors, ZIP naming, aggregate limits
- welcome video fallback, texts, keyboards, navigation, `/start help`, `/cancel`, `/help`
- webhook secret validation, body limits, dedupe, full-queue behaviour
- runtime lifecycle, webhook registration, health state, ingestion

The suite does **not** prove live Telegram API compatibility, real channel membership behaviour, Render availability, or real-world PDF variety. Those require a deployed smoke test.

---

## Security and privacy

- **Webhook authentication:** every POST must include the exact secret header; comparison uses `hmac.compare_digest`. Missing configuration returns 503; a wrong secret returns 401.
- **Body limits:** `Content-Length` and streamed chunks are capped at 1 MB.
- **No public file serving:** results are sent through Telegram; workspace paths are never exposed over HTTP.
- **Path handling:** uploaded display names are sanitized, stored filenames are generated, and path resolution rejects workspace escapes. ZIP entry names are sanitized.
- **Isolation:** each user has a separate `job_*` directory with restrictive permissions where the OS supports it.
- **Limits:** per-file, aggregate input, page, image pixel, per-output, aggregate output, ZIP entry, job, queue, and workspace limits prevent unbounded resource use.
- **PDF handling:** encrypted PDFs are rejected; malformed files fail validation; outputs receive clean metadata.
- **Fail closed:** subscription checks that cannot be completed block protected actions instead of granting access.
- **Secrets:** `BOT_TOKEN`, `WEBHOOK_SECRET`, and the welcome `file_id` use `repr=False`; logs do not print them.
- **Temporary data:** workspaces are removed on completion, failure, cancellation, timeout, and shutdown. Files are not retained for future downloads.
- **No database:** no passwords, accounts, or persistent history are stored.

This is not a hardened multi-tenant service. It is a single-instance bot for a small/medium audience. Review the limits and hosting model before using it at scale.

---

## Limitations

- **In-memory state:** FSM state, membership cache, pending actions, dedupe records, and job queues are lost on restart. A Render restart or deploy cancels active work and clears menus.
- **Ephemeral disk:** workspaces live on the instance disk. Render Free may restart or replace the instance; temp files disappear. This is acceptable because files are not meant to persist.
- **Render Free:** the service sleeps after inactivity. The first webhook after sleep may be delayed while the instance starts; Telegram retries, but a 503 during startup is possible. Free instances have limited RAM and CPU.
- **One active job:** `MAX_ACTIVE_JOBS=1` by default. Other jobs wait in a small queue and can time out.
- **Cooperative cancellation:** a Python thread cannot be killed. `/cancel` and timeouts stop at the next page/image boundary, and cleanup waits for the worker to finish.
- **Page-range scope:** split sends up to 3 outputs individually and ZIPs larger result sets. Very large outputs are rejected by limits.
- **No OCR, PDF editing, Office conversion, password removal, or permanent links.**
- **Image scope:** JPG and PNG only. Very large images are downscaled; the bot does not upscale.
- **Welcome video:** the `file_id` is bot-specific and can become invalid; the bot falls back to text but cannot repair the configuration.
- **Single process:** in-memory design assumes one web process. Multiple workers or replicas would have independent state and webhook conflicts.
- **No license file** has been added. Add one before publishing if required.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/health` returns 503 | The app is still starting or failed startup. Check logs for `BOT_TOKEN` or config errors. |
| Webhook not registered | `PUBLIC_BASE_URL` (or `RENDER_EXTERNAL_URL`) is empty, `WEBHOOK_AUTO_SET=false`, or registration is retrying. Check logs. |
| Telegram gets 401 on the webhook | `WEBHOOK_SECRET` changed without re-registering, or the header is missing. Re-register by restarting with the same secret. |
| Everyone is blocked by the subscription prompt | The bot is not an admin in `FSUB_CHANNEL`, the channel name is wrong, or Telegram is unavailable. The app fails closed by design. |
| A task times out | The file is too complex for the 60-second limit or the instance is overloaded. Try fewer/smaller files. A worker may continue briefly until it reaches a cancellation check. |
| `BOT_TOKEN is required` | Set the environment variable before starting; `.env` is not loaded automatically. |
| Welcome video does not appear | `WELCOME_VIDEO_FILE_ID` is empty or invalid for this bot. The text menu is used instead. |
| A new task cannot start | One job is active or the queue is full. Wait, use `/cancel`, or retry shortly. |

---

## Unfinished items and next steps

1. Create the Telegram bot with BotFather, add it to the channel as an administrator, and perform a live smoke test.
2. Publish the repository and create the Render Blueprint/service. Confirm `/health` and webhook registration in the Render logs.
3. Test with real-world PDFs (forms, large images, unusual fonts, mixed page sizes, damaged files) and real channel membership transitions.
4. Add an explicit `WEBHOOK_SECRET` in the hosting dashboard and document a rotation procedure.
5. Consider optional persistent storage for FSM/pending actions if multi-instance or restart resilience becomes a requirement.
6. Consider a monitoring/alerting hook for repeated webhook failures, membership-check failures, and queue saturation.
7. Add a license and contribution guidelines if the project will be shared publicly.

---

## Commands

```powershell
# Install
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Run tests
.\.venv\Scripts\python.exe -m pytest -q

# Compile-check every module
.\.venv\Scripts\python.exe -m compileall -q app tests

# Import the FastAPI app
.\.venv\Scripts\python.exe -c "from app.main import app; print(app.title)"

# Run locally (webhook requires a public URL)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```
