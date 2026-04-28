# main.py
# Entry point — starts the FastAPI server and opens the browser.

import uvicorn
import webbrowser
import threading
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from ui import app  # noqa: E402


def open_browser():
    import time
    time.sleep(1.5)
    webbrowser.open("http://localhost:8000")


if __name__ == "__main__":
    print("=" * 60)
    print("  Multi-Agent Competitive Intelligence System")
    print("  MSBA AI Agents — McCombs School of Business")
    print("=" * 60)
    print("\nStarting server at http://localhost:8000 ...")
    print("Press Ctrl+C to stop.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, log_level="warning")
