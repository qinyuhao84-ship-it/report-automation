from __future__ import annotations

import uvicorn

from report_automation.main import app


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000)
