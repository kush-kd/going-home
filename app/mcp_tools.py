"""MCP tool server the Checker agent calls while it reviews a plan.

The orchestrator writes the cleaned document and the plan into a per-run folder
(GH_RUN_DIR) and starts this server for that one Checker call. Every tool is plain,
deterministic code, and every call is logged to tool_calls.jsonl for the trace.

Tools:
  list_medication_lines()       lines in the document that look like medicine orders
  search_document(query)        lines that contain the words of a query
  check_quote(quote)            is this exact text in the document? (match score)
  verify_item(item_id)          the code-based grounding result for one plan item
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from app import grounding  # noqa: E402
from app.models import Extraction  # noqa: E402

RUN_DIR = Path(os.environ.get("GH_RUN_DIR", "."))
DOC = (RUN_DIR / "document.txt").read_text() if (RUN_DIR / "document.txt").exists() else ""
LINES = DOC.splitlines()
DOC_NORM = grounding.normalize(DOC)
PLAN = Extraction.model_validate_json((RUN_DIR / "plan.json").read_text()) if (RUN_DIR / "plan.json").exists() else None
_VERIFIED: dict | None = None

server = MCPServer("goinghome", instructions="Tools for checking a discharge plan against its source document.")

_MED_LINE = re.compile(r"\b(\d+(\.\d+)?\s*(mg|mcg|units?|ml|g)\b|tab|cap|inhal|puff|patch|inject|PO\b|BID|TID|QID|QHS|PRN|q\d+h)", re.I)


def _log(tool: str, args: dict, result) -> None:
    summary = result if isinstance(result, (str, int, float, bool)) else json.dumps(result)[:300]
    with open(RUN_DIR / "tool_calls.jsonl", "a") as f:
        f.write(json.dumps({"tool": tool, "args": args, "result": summary}) + "\n")


@server.tool()
def list_medication_lines() -> list[dict]:
    """List every line of the discharge document that looks like a medicine order (has a strength, dose form or
    frequency abbreviation). Use it to make sure no medicine in the document is missing from the plan."""
    out = [{"line": i + 1, "text": ln.strip()} for i, ln in enumerate(LINES) if _MED_LINE.search(ln) and len(ln.strip()) > 8]
    _log("list_medication_lines", {}, f"{len(out)} lines")
    return out[:80]


@server.tool()
def search_document(query: str, max_results: int = 6) -> list[dict]:
    """Find lines in the discharge document that contain all the words of the query (case-insensitive).
    Returns line numbers with the line and its neighbours, so wrapped sentences are visible."""
    words = [w for w in re.findall(r"[\w./-]+", query.lower()) if len(w) > 1]
    hits = []
    for i, ln in enumerate(LINES):
        window = " ".join(LINES[max(0, i - 1): i + 2]).lower()
        if words and all(w in window for w in words) and any(w in ln.lower() for w in words):
            hits.append({"line": i + 1, "text": " ".join(x.strip() for x in LINES[max(0, i - 1): i + 2])})
        if len(hits) >= max(1, min(max_results, 10)):
            break
    _log("search_document", {"query": query}, f"{len(hits)} hits")
    return hits


@server.tool()
def check_quote(quote: str) -> dict:
    """Check whether this exact text appears in the discharge document. Returns found (true/false) and a 0-1 match score.
    Call it on every document_quote before you report an issue: claims without a real quote are discarded."""
    found, score = grounding.quote_in_document(quote, DOC_NORM)
    result = {"found": found, "match": score}
    _log("check_quote", {"quote": quote[:120]}, result)
    return result


@server.tool()
def verify_item(item_id: str) -> dict:
    """Return the code-based grounding check for one plan item (e.g. m3, a1, w2): whether its quote is in the document
    and whether its strength, dose, frequency, dates and phone numbers match that quote, with any problems listed."""
    global _VERIFIED
    if PLAN is None:
        return {"error": "no plan loaded"}
    if _VERIFIED is None:
        _VERIFIED = grounding.verify_extraction(PLAN, DOC)
    result = _VERIFIED.get(item_id, {"error": f"unknown item id {item_id}"})
    _log("verify_item", {"item_id": item_id}, result)
    return result


if __name__ == "__main__":
    server.run()
