# secrets/

This directory holds local credentials and is **git-ignored**.

| File | Purpose | How to create |
| --- | --- | --- |
| `gmail_client_secret.json` | OAuth client (Desktop app) for the Gmail API. | Download from Google Cloud Console → APIs & Services → Credentials → OAuth client ID → Desktop. Rename to this. |
| `gmail_token.json` | Cached Gmail OAuth user token. | Auto-created on first `python -m ftc_automation ingest --gmail` run. |
| `gv_storage_state.json` | Playwright cookies for `voice.google.com`. | Auto-created by `python -m ftc_automation login`. |

See `gmail_client_secret.example.json` for the expected shape of the
OAuth client file.

**Never commit anything in this directory.** `.gitignore` excludes the
whole folder; only `README.md` and `*.example.*` files are tracked.
