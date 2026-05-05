"""Development server entry-point.

Run with:
    python run.py

`uvicorn --reload` watches every file in the directory, including
`ai_cache.json`, `gmail_email_cache.json`, `gmail_watch_state.json` and
`token.pkl` — all of which are rewritten constantly by the running app. That
caused the server to restart every ~60 seconds and dropped any open
Server-Sent Events connection before it could deliver an event.

This script:
  * limits reload watching to the `app/` directory,
  * only watches `*.py` files,
  * explicitly excludes the JSON / pickle / log state files,
  * requires `watchfiles` (installed via `pip install watchfiles`) so the
    include / exclude patterns are actually honoured (StatReload ignores them).
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[os.path.join(os.path.dirname(__file__), "app")],
        reload_includes=["*.py"],
        reload_excludes=[
            "*.json",
            "*.pkl",
            "*.log",
            "*.tmp",
            "*ai_cache*",
            "*gmail_email_cache*",
            "*gmail_watch_state*",
            "*token.pkl*",
        ],
    )
