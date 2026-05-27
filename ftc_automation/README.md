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

The submitter is meant to run as a long-lived daemon you start
manually in a terminal so you can watch it (it opens a real browser by
default until you flip `ftc.headless: true`).

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
