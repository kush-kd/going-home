"""Start Going Home:  python run.py   (then open http://localhost:8000)"""

import os
import webbrowser
from threading import Timer

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    if os.getenv("NO_BROWSER") != "1":
        Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)
