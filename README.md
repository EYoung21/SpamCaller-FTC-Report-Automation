# SpamCaller FTC Report Automation

> Turn a Google Voice inbox full of spam voicemails into hundreds of filed
> Do Not Call Registry complaints — without spending your week clicking through
> a 1990s government form.

An end-to-end pipeline that ingests Google Voice voicemails, uses an LLM to
classify spam and extract the structured fields the FTC complaint form wants,
lets you approve or reject each flagged voicemail in a local web UI (with
inline audio playback), and then drives `donotcall.gov` via headless browser
automation — retrying hourly via Windows Task Scheduler when the FTC
rate-limits us.

---

## Why this exists

Many households get five-to-fifteen unwanted robocalls a day. Auto warranty scams,
fake debt-consolidation pitches, "your Amazon order has been charged,"
Medicare bait, the works. Third-party call-blocking and screening apps often
fall short:

- Some can't filter spam on certain carriers at all.
- Others misfire on legitimate calls — doctor's offices,
  pharmacy refill lines, school announcements — so people turn them off.
- A few seem to make things *worse* over time, as if the number was
  being shared with the very ecosystem they claimed to protect against.

Reading the FCC and FTC's own consumer guidance, the actual recommended
action is to **report each unwanted call to the National Do Not Call Registry
at [donotcall.gov](https://www.donotcall.gov/report.html)**. Those reports
feed enforcement actions and the agency does, eventually, fine the worst
offenders — but only if enough of us actually file.

Filing one report by hand takes ~3 minutes (two-page form, datepicker,
dropdown chains). Filing hundreds is a non-starter. So I built this.

---

## What it does

1. **Ingest** every voicemail from a Google Voice account — both the historical
   backlog (one-time Playwright sweep of `voice.google.com`) and ongoing new
   ones (via Gmail-forwarded transcripts).
2. **Classify** each voicemail with `gpt-4o-mini`, producing a structured JSON
   verdict: is it spam, what category (auto-warranty, debt-relief, Medicare,
   tax, IRS, etc.), claimed company, callback number, and a pre-written
   complaint comment in the user's voice.
3. **Re-scrape audio** so each spam voicemail has an inline `<audio>` player
   in the review UI — the human reviewer can listen before approving.
4. **Review** every flagged voicemail in a local Flask UI. Approve, reject,
   or edit any field. Keyboard-driven (`A` / `R` / `S` / `J` / `K`). A "bulk
   approve at confidence ≥ 0.90" button handles the obvious duplicate clusters.
5. **Submit** approved complaints to `donotcall.gov` via headless Playwright,
   filling both pages of the form, handling the jQuery datepicker, watching
   for the success panel, and screenshotting `submissions/vm-<id>.png` for audit.
6. **Auto-retry hourly** via Windows Task Scheduler when the FTC throttles us
   (which happens after every few dozen submissions). The scheduler drains the
   queue over a day or two with zero user attention.

---

## Architecture

```
+---------------------------+
| Playwright GV scraper     | --\
+---------------------------+    \
                                  \
+---------------------------+      \    +-------------------+
| Gmail forwarded-VM watch  | -----> SQLite -->| OpenAI classifier |
+---------------------------+                  +-------------------+
                                                        |
                                                        v
                                                +-------------------+
                                                | Audio re-scraper  |
                                                +-------------------+
                                                        |
                                                        v
                                                +-------------------+
                                                | Flask review UI   |
                                                |  - audio playback |
                                                |  - keyboard nav   |
                                                |  - bulk approve   |
                                                +-------------------+
                                                        |
                                                        v
                                                +-------------------+
                                                | FTC submitter     |
                                                |  - Playwright     |
                                                |  - throttle aware |
                                                |  - audit shots    |
                                                +-------------------+
                                                        ^
                                                        |
                                              every hour, via Task Scheduler
```

Stages are decoupled and idempotent. Each voicemail walks through
`new -> classified -> approved | rejected | skipped -> submitted | submit_failed`.
Re-running any stage is safe.

---

## Quick start

```powershell
git clone git@github.com:EYoung21/SpamCaller-FTC-Report-Automation.git
cd SpamCaller-FTC-Report-Automation

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ftc_automation\requirements.txt
playwright install chromium

# config + secrets
copy ftc_automation\config.example.yaml ftc_automation\config.yaml   # edit it
echo "OPENAI_API_KEY=sk-..." > .env

# one-time GV login (real browser window opens; sign in manually)
python -m ftc_automation login

# one-time historical backlog
python -m ftc_automation ingest --backlog
python -m ftc_automation classify
python -m ftc_automation rescrape-audio    # downloads mp3s for inline playback

# review in your browser
python -m ftc_automation review            # http://127.0.0.1:5000

# submit approved complaints (loops; --once for a single drain pass)
python -m ftc_automation submit

# OR install the hourly auto-submitter and walk away
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
```

Full setup details (Gmail forwarding for ongoing ingestion, OAuth client
creation, configuration reference, FTC form-field mapping table) are in
[`ftc_automation/README.md`](ftc_automation/README.md).

---

## Example throughput (single inbox)

On one real Google Voice account used during development:

| Metric                                           | Example   |
| ------------------------------------------------ | --------- |
| Voicemails ingested (multi-year backlog)         | ~3,000    |
| Classified by `gpt-4o-mini`                      | ~2,750    |
| Flagged as spam (≥ 0.65 confidence)              | ~250      |
| Approved after human review                      | ~220      |
| OpenAI cost to classify the full backlog         | ~$2       |
| Wall-clock time to classify                      | ~30 min   |
| Hands-on time per voicemail in the review UI     | ~3 sec    |

The hourly auto-submitter then files approved complaints unattended over
the next day or two, sleeping politely between donotcall.gov throttle windows.

---

## Tech stack

- **Python 3.11** + **SQLAlchemy 2** + **SQLite** (zero-deploy, single-user)
- **Playwright** for both Google Voice scraping and FTC form submission
- **OpenAI API** (`gpt-4o-mini`) with structured-output prompting
- **Flask** + vanilla HTML/CSS for the review UI (keyboard-first)
- **Pydantic** for config validation, **python-dotenv** for secrets
- **Windows Task Scheduler** + PowerShell for unattended hourly retries
- **Google Gmail API** for ongoing voicemail-transcript ingestion

---

## Project layout

```
SpamCaller-FTC-Report-Automation/
  README.md                              <- you are here
  .env                                   <- OPENAI_API_KEY (gitignored)
  ftc_automation/
    README.md                            <- detailed dev / ops doc
    cli.py                               <- `python -m ftc_automation <cmd>`
    config.example.yaml
    requirements.txt
    ftc/
      config.py                          <- pydantic config loader
      db.py                              <- SQLite schema + upsert helpers
      ingest/
        gv_playwright.py                 <- one-time backlog scraper
        gv_audio.py                      <- one-time audio re-fetcher
        gmail_watcher.py                 <- ongoing forwarded-email ingest
      classify/
        openai_classifier.py
        prompts.py
        ftc_mapping.py                   <- scam_category -> FTC ddlSubjectMatter
      review/
        app.py                           <- Flask review UI
        templates/                       <- queue.html, review.html
      submit/
        base.py
        ftc_playwright.py                <- default Playwright submitter
        ftc_http.py                      <- v2 stub: raw HTTP POST
  scripts/
    run_submit.bat                       <- Task Scheduler wrapper
    install_scheduled_task.ps1           <- one-shot installer
    queue_status.py                      <- helper: dump pipeline counts
    sample_missing_audio.py              <- helper: list rows missing audio
    inspect_gv_row.py                    <- dev: probe GV DOM
```

---

## Disclaimers and scope

- **Personal use.** This is a consumer automation tool. It is not affiliated
  with the FTC, FCC, Google, or any third party. It does not promise to stop
  any specific call from happening. Reporting numbers to the Do Not Call
  Registry is what the FTC asks consumers to do; this just automates the
  clerical part.
- **Single-account.** The pipeline targets one Google Voice number per
  installation. Multi-tenant use is intentionally out of scope.
- **No bulk filing.** The submitter is rate-limited and respects the FTC's
  throttle responses. It files complaints one at a time, the same as a human
  would, just on a schedule.
- **Google Voice TOS.** The scraper uses your own browser session against
  your own account. Use accordingly.
- **MIT-style permissive intent.** Use, fork, modify, and contribute freely.

---

## Roadmap / ideas

- Replace the Playwright submitter with a raw `requests` POST after
  reverse-engineering the form (~10× faster, no browser).
- Smart de-duplication: detect "same operation, different caller ID" clusters
  by callback number, so the FTC sees one rich complaint per campaign instead
  of dozens of singletons.
- Pattern dashboard showing callback-number clusters and time-of-day heatmaps.
- macOS / Linux scheduling parity (currently the auto-retry helper is
  Windows-only).
- Optional Twilio / VoIP.ms ingestion path for non-Google-Voice users.

---

## License

MIT-style permissive. No warranty. See repo for details.
