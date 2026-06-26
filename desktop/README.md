# Easy Debit Summary — Desktop Mode

Run the web app as a local desktop application (no internet required for PDF parsing).

## How it works

- `launcher.py` starts a local Flask server on port 15432 and opens your browser.
- PDF parsing and CSV export happen entirely on your machine.
- License validation and AI chat features contact `easydebitsummary.com` — no PDFs or passwords are sent to the server.

## Quick start (development)

```bash
APP_MODE=desktop python desktop/launcher.py
```

## Build installers

- Windows: see `build_windows.md`
- macOS: see `build_macos.md`

## Security model

| What stays local | What goes to VPS |
|---|---|
| PDFs, passwords | License token (activation check) |
| Parsed transactions | Chat/AI queries (anonymized) |
| Exported CSVs | — |

LLM provider API keys are stored **only** on the VPS — never in the desktop bundle.
