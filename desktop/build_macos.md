# Build: macOS App

## Prerequisites

- macOS 12+ (Apple Silicon or Intel)
- Python 3.11 — `brew install python@3.11`
- Xcode Command Line Tools — `xcode-select --install`

## Steps

```bash
git clone https://github.com/w2flovnishpratham/Easy_debit_summary_web_app EDS
cd EDS

python3.11 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
pip install pyinstaller

export VPS_URL=https://easydebitsummary.com
export APP_MODE=desktop

pyinstaller desktop/pyinstaller_macos.spec
```

Output: `dist/EasyDebitSummary.app`

## Signing & Notarization

```bash
# Sign
codesign --deep --force --sign "Developer ID Application: Your Name (TEAMID)" \
    dist/EasyDebitSummary.app

# Zip for notarization
ditto -c -k --keepParent dist/EasyDebitSummary.app EasyDebitSummary.zip

# Submit
xcrun notarytool submit EasyDebitSummary.zip \
    --apple-id your@email.com \
    --password @keychain:AC_PASSWORD \
    --team-id TEAMID \
    --wait

# Staple
xcrun stapler staple dist/EasyDebitSummary.app
```

## DMG packaging

```bash
npm install -g create-dmg
create-dmg dist/EasyDebitSummary.app --overwrite dist/
```
