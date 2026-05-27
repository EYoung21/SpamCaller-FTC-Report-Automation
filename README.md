# SpamCaller-FTC-Report-Automation

Pipeline that ingests Google Voice voicemails, uses OpenAI to flag spam
and extract complaint-form fields, lets you review/approve in a local
web UI, then auto-submits approved reports to
[donotcall.gov](https://www.donotcall.gov/report.html).

The whole application lives in [`ftc_automation/`](ftc_automation/) —
see [`ftc_automation/README.md`](ftc_automation/README.md) for full
setup and usage docs.

## Quick start

```powershell
cd ftc_automation
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

copy config.example.yaml config.yaml   # edit it
python -m ftc_automation login          # sign in to Google Voice once
python -m ftc_automation ingest --backlog
python -m ftc_automation classify
python -m ftc_automation review         # http://127.0.0.1:5000
python -m ftc_automation submit         # background daemon
```

## License

Personal-use project. No warranty.
