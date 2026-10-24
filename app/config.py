"""Settings. An optional .env file is read from the repo folder first, then from the folder above it."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# Repo-level .env wins; the parent-folder .env (where the teaching staff keep their keys) fills gaps.
load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT.parent / ".env", override=False)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# Per-agent models for the Claude subscription. The CLI takes aliases; the exact model id
# that answered is read back from each response and logged.
CLAUDE_MODELS = {
    "transcriber": os.getenv("TRANSCRIBER_MODEL", "sonnet"),
    "reader": os.getenv("READER_MODEL", "opus"),
    "checker": os.getenv("CHECKER_MODEL", "sonnet"),
    "explainer": os.getenv("EXPLAINER_MODEL", "sonnet"),
}

# Safety and spending limits.
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 10)
MAX_PAGES = _int("MAX_PAGES", 10)
MAX_DOC_CHARS = _int("MAX_DOC_CHARS", 40_000)
MAX_TOKENS_PER_RUN = _int("MAX_TOKENS_PER_RUN", 150_000)   # input + output, cache reads excluded
MAX_LLM_CALLS_PER_RUN = _int("MAX_LLM_CALLS_PER_RUN", 8)
MAX_RUNS_PER_DAY = _int("MAX_RUNS_PER_DAY", 40)
MAX_REPAIR_ROUNDS = _int("MAX_REPAIR_ROUNDS", 1)
MAX_USD_PER_CALL = float(os.getenv("MAX_USD_PER_CALL", "0.60"))   # hard cap passed to each claude -p call
LLM_TIMEOUT_S = _int("LLM_TIMEOUT_S", 240)

SAMPLES_DIR = ROOT / "data" / "samples"
LOG_DIR = ROOT / "logs"
STATIC_DIR = ROOT / "app" / "static"
PROMPTS_DIR = ROOT / "app" / "prompts"
