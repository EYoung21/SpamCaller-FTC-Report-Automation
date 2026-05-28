# FTC Voicemail Spam Reporter

Pipeline that ingests Google Voice voicemails, uses OpenAI to flag spam and
extract the structured fields the FTC complaint form wants, lets you
review/approve flagged voicemails in a local web UI, and auto-submits
approved complaints to [donotcall.gov](https://www.donotcall.gov/report.html).

```
[ Playwright GV scraper ] --\
                              \
[ Gmail API watcher        ] --> SQLite ---> [ OpenAI classifier ]
                                     |
                                     v
                          [ Flask review UI ] ---> [ Playwright FTC submitter ]
```

The pipeline stages are **decoupled and idempotent**. Each voicemail walks
through `new -> classified -> approved | rejected | skipped ->
submitted | submit_failed`. Re-running any stage is safe.

---

## Setup

### 1. Install Python deps + Playwright browsers

From this directory (`ftc_automation/`):

```powershell
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```powershell
copy config.example.yaml config.yaml
```

Open `config.yaml` and fill in:

- `gv_number` – the Google Voice number that *receives* the spam (digits only).
- `personal.*` – the name/address that will appear on FTC complaints
  (required by the form; only the FTC sees it, not the spammer).
- `openai.api_key` – or set the `OPENAI_API_KEY` env var (preferred). The
  default model is `gpt-4o-mini`, which classifies ~$0.001 per voicemail.
  The easiest way is to drop it in a `.env` file at the repo root:

  ```
  OPENAI_API_KEY=sk-...
  ```

  The app loads `.env` automatically on startup (via `python-dotenv`).
  See `.env.example` for the supported variables.

### 3. Authorize Google Voice (one-time, for backlog scraping)

```powershell
python -m ftc_automation login
```

A real Chromium window opens, you sign in to Google Voice manually,
then press `<Enter>` in the terminal. An authenticated
`secrets/gv_storage_state.json` is written. Future `--backlog` runs are
headless.

### 4. Enable Gmail forwarding (for the ongoing watcher)

In Google Voice:

> ☰ → **Settings** → **Voicemail** → **Get email notifications** → on
> **Include transcript** → on

Now every new voicemail also lands as a Gmail message from
`voice-noreply@google.com` with the transcript inline. We pull those via
the Gmail API.

### 5. Create a Gmail OAuth client (one-time)

1. Go to <https://console.cloud.google.com/>.
2. Create a new project (or pick an existing one).
3. **APIs & Services → Library → Gmail API → Enable**.
4. **APIs & Services → OAuth consent screen** → set up as "External",
   add your own Gmail address as a test user.
5. **APIs & Services → Credentials → Create credentials → OAuth client
   ID → Desktop app**, download the JSON.
6. Save it to `secrets/gmail_client_secret.json` (or wherever
   `gmail.client_secret_path` points).

The first `python -m ftc_automation ingest --gmail` run will pop a
browser for the OAuth handshake and then cache the token at
`secrets/gmail_token.json`.

---

## Daily usage

| Action | Command |
| --- | --- |
| One-time: sweep historical backlog | `python -m ftc_automation ingest --backlog` |
| One-time: classify the backlog | `python -m ftc_automation classify` |
| One-time: download mp3s for spam-flagged rows so the review UI can play them inline | `python -m ftc_automation rescrape-audio` (add `--all` for every row) |
| Recurring (cron / Task Scheduler, every 15 min): | `python -m ftc_automation ingest --gmail && python -m ftc_automation classify` |
| Review queue in your browser | `python -m ftc_automation review` then open <http://127.0.0.1:5000/> |
| Submit approved complaints | `python -m ftc_automation submit` (loops; `--once` to drain) |
| Print pipeline counts | `python -m ftc_automation status` |

### Review UI keyboard shortcuts

- `A` — approve & queue for FTC submission
- `R` — reject (mark as not spam)
- `S` — skip (revisit later)
- `J` / `K` — next / previous voicemail
- `Save edits` button — persist edited fields without changing status

The queue page also has a bulk action: **Approve all in queue at
confidence ≥ 0.90**. Great for the "Marlene / Bennett / debt
consolidation" duplicate clusters.

### Scheduling on Windows

Use Task Scheduler with these two tasks:

```
Name:       FTC – Ingest+Classify
Trigger:    Every 15 minutes
Action:     Start a program
Program:    C:\path\to\.venv\Scripts\python.exe
Arguments:  -m ftc_automation ingest --gmail
Start in:   C:\Users\hello\Documents\FTCReportAutomation
```

```
Name:       FTC – Classify
Trigger:    After "FTC – Ingest"
Arguments:  -m ftc_automation classify
```

For the submitter, the easiest path is the pre-built installer:

```powershell
powershell -ExecutionPolicy Bypass -File ..\scripts\install_scheduled_task.ps1
```

That registers `FTCReportAutomation-HourlySubmit`, which runs
`python -m ftc_automation submit --once` every 60 minutes, logging each
run to `logs/auto_submit.log`. `donotcall.gov` aggressively throttles
bursts; the hourly cadence lets the queue drain over a day or two with
zero user attention. Useful management commands:

```powershell
Get-ScheduledTask -TaskName FTCReportAutomation-HourlySubmit | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName FTCReportAutomation-HourlySubmit    # run now
Get-Content .\logs\auto_submit.log -Tail 80 -Wait                 # tail logs
Unregister-ScheduledTask -TaskName FTCReportAutomation-HourlySubmit -Confirm:$false
```

You can also run the submitter manually as a foreground daemon if you'd
rather watch it in a terminal (`python -m ftc_automation submit`, loops
forever).

---

## How the FTC form is filled in

The submitter targets the IDs that live in donotcall.gov's HTML today:

| Step 1 | Source |
| --- | --- |
| `#PhoneTextBox` | `gv_number` from config |
| `#DateOfCallTextBox` | `received_at` MM/DD/YYYY |
| `#TimeOfCallDropDownList` | `received_at` hour (00–23) |
| `#ddlMinutes` | `received_at` minute (rounded to 5) |
| `#PrerecordMessageYESRadioButton` | always (voicemail = robocall) |
| `#PhoneCallRadioButton` | always |
| `#ddlSubjectMatter` | `ftc_subject_id` mapped from `scam_category` |
| `#txtSubjectMatter` | populated only when subject = 1 ("Other") |

| Step 2 | Source |
| --- | --- |
| `#CallerPhoneNumberTextBox` | `caller_number` |
| `#CallerNameTextBox` | `claimed_company` (LLM-extracted) |
| `#HaveBusinessNoRadioButton` | always |
| `#StopCallingNoRadioButton` | always |
| `#FirstNameTextBox`, `#LastNameTextBox`, … | `personal.*` from config |
| `#CommentTextBox` | `comment_text` (LLM-written, you can edit in UI) |

After clicking `#StepTwoSubmitButton` the submitter waits for
`#StepTwoAcceptedPanel` and screenshots `submissions/vm-<id>.png` for
audit. On failure the row drops to `submit_failed` with the error text.

### CAPTCHA contingency

There isn't one today, but the submitter detects an
`iframe[src*="recaptcha"]`. If it ever appears, the row stays in
`approved` and an error is logged so you can intervene manually.

### Getting past FTC throttling

`donotcall.gov` rate-limits by IP after bursts. The submitter detects
the "system difficulties" page and stops the batch instead of burning
through your queue.

**Easiest fix (no config):** connect the laptop to a **phone hotspot**
so your IP changes, then run `python -m ftc_automation submit --once`.

**Automated proxy rotation:** add one or more proxies under `ftc.proxies`
in `config.yaml`, or set a comma-separated list in `.env`:

```
FTC_PROXIES=http://user:pass@proxy1.example.com:8080,socks5://127.0.0.1:1080
```

Rotation modes (`ftc.proxy_rotate`):

| Mode | Behavior |
| --- | --- |
| `on_throttle` (default) | After a throttle, rotate proxy and retry once; next scheduled run also starts on the next proxy |
| `each_run` | Every `submit --once` invocation uses the next proxy |
| `each_submit` | Rotate between every successful complaint in a batch |

You need real proxy endpoints — the code cannot invent new IPs. Options:

- **Phone hotspot** — toggling hotspot on/off changes IP with zero config
- **VPN client with local SOCKS** — e.g. `socks5://127.0.0.1:1080` while connected; switch VPN server between runs
- **Residential proxy service** — paid providers (Webshare, etc.) give you a list of `http://user:pass@host:port` URLs

Datacenter/free VPN IPs often get blocked faster than your home IP.

**Auto-fetch free proxies (experimental, often dead):**

```powershell
python scripts/fetch_proxies.py --test 80 --country ALL --socks5
```

Working endpoints land in `secrets/proxies.txt` and load automatically via
`ftc.proxy_file`. **Do not file real complaints through untrusted free
proxies** — they can read your mom's name/address on the form. Hotspot or
paid residential is safer.

**There is no magic IP spoof without a network path.** Options ranked:

| Method | Cost | Works for FTC? |
| --- | --- | --- |
| Phone hotspot | Free | Best free option — new mobile IP |
| VPN → local SOCKS in `proxies.txt` | ~$5/mo | Good if you switch servers between runs |
| Paid residential proxy | ~$2–5 trial | Best for automation at scale |
| Free public proxy lists | Free | Usually dead; security risk for PII |
| "Fake IP in code" | — | **Impossible** without one of the above |

---

## Cost & throughput

- Classify the full ~2k voicemail backlog: ~$2 in OpenAI charges, ~30 min wall clock.
- Submitter is rate-limited to ~1 complaint per 20 s with jitter, so a
  full backlog takes ~10–15 hours of background time. Run it overnight.

---

## Layout

```
ftc_automation/
  cli.py                  # `python -m ftc_automation <cmd>` entrypoint
  config.example.yaml
  requirements.txt
  ftc/
    config.py             # pydantic config loader
    db.py                 # SQLite schema + helpers
    ingest/
      gv_playwright.py    # one-time backlog scraper
      gmail_watcher.py    # ongoing forwarded-email ingestion
    classify/
      openai_classifier.py
      prompts.py
      ftc_mapping.py      # scam_category -> FTC ddlSubjectMatter
    review/
      app.py              # Flask review UI
      templates/
    submit/
      base.py
      ftc_playwright.py   # default submitter
      ftc_http.py         # v2 stub
```

---

## Things deferred to v2

- Replace the Playwright submitter with a raw `requests` HTTP POST after
  reverse-engineering the form via DevTools — should drop submission time
  from ~10 s to <1 s.
- Smart de-duplication: detect "same operation, different caller ID"
  clusters by callback number, so the FTC sees one rich complaint per
  campaign instead of dozens of singletons.
- Pattern dashboard showing callback-number clusters across the whole DB.
