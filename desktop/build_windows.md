# Build: Windows Installer

## Prerequisites

- Windows 10/11 (64-bit)
- Python 3.11 (64-bit) — https://python.org
- Git for Windows

## Steps

```bat
git clone https://github.com/w2flovnishpratham/Easy_debit_summary_web_app EDS
cd EDS

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt
pip install pyinstaller

pyinstaller desktop/pyinstaller_windows.spec
```

The output will be in `dist/EasyDebitSummary/`.

## Environment variables baked in

Set these **before** building so PyInstaller freezes them into the bundle:

```bat
set VPS_URL=https://easydebitsummary.com
set APP_MODE=desktop
```

Do **not** bake in LLM provider keys — the desktop app calls the VPS gateway for AI features.

## Packaging to installer

Use [Inno Setup](https://jrsoftware.org/isinfo.php) to wrap `dist/EasyDebitSummary/` into a single `.exe` installer.
Point the installer at `EasyDebitSummary.exe` as the main executable.

## Code signing

Sign the installer with a Windows EV code signing certificate to avoid SmartScreen warnings.
