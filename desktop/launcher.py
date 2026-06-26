"""
Desktop launcher: starts the local Flask server and opens the browser.
Bundled by PyInstaller — this is the entry point.
"""
import os
import sys
import threading
import time
import webbrowser

# Desktop mode flags
os.environ.setdefault("APP_MODE", "desktop")
os.environ.setdefault("FLASK_ENV", "production")

# Add project root to path when running from PyInstaller bundle
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import desktop.app_state as app_state

LOG_FILE = os.path.join(os.path.expanduser("~"), "EasyDebitSummary_error.log")


def _run_server(port: int):
    try:
        # Ensure required directories exist next to the bundle
        data_dir = os.path.join(os.path.expanduser("~"), "EasyDebitSummary_data")
        os.makedirs(os.path.join(data_dir, "outputs"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "uploads"), exist_ok=True)
        os.environ.setdefault("EDS_DATA_DIR", data_dir)

        from app import app  # import here so errors are caught
        app_state.server_port = port
        app_state.server_ready = True
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except Exception as exc:
        import traceback
        with open(LOG_FILE, "w") as f:
            f.write(traceback.format_exc())
        # Show native error dialog on macOS
        os.system(f'osascript -e \'display dialog "Easy Debit Summary failed to start.\\n\\nSee log: {LOG_FILE}\\n\\n{exc}" buttons {{"OK"}} with icon stop\'')


def _open_browser(port: int):
    for _ in range(40):
        if app_state.server_ready:
            break
        time.sleep(0.3)
    if app_state.server_ready:
        webbrowser.open(f"http://127.0.0.1:{port}")


def main():
    port = int(os.environ.get("EDS_PORT", "15432"))
    t = threading.Thread(target=_run_server, args=(port,), daemon=True)
    t.start()
    _open_browser(port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
