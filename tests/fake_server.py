"""DEV ONLY: run the UI with the fake model from the tests (no AI calls), to check layout.

    .venv/bin/python tests/fake_server.py

Every sample is answered with the gold extraction of sample 01, so only sample 01 gives a
correct plan. The status pill says FAKE so this can never be mistaken for the real app.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn  # noqa: E402

from app import agents, llm, main  # noqa: E402
from fake_llm import FakeLLM  # noqa: E402

agents.call_structured = FakeLLM()
llm.provider = lambda: "fake"
llm.status = lambda: {"provider": "fake", "ready": True, "message": "FAKE test model (dev only, no AI)"}

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8011)
