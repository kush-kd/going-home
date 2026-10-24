"""The four agents. Each has its own prompt and output schema; the Checker also calls real tools (app/mcp_tools.py)."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from . import config
from .llm import Image, LLMResult, call_structured
from .models import CheckerReport, Explanation, Extraction, json_schema


def _prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text()


def _doc_block(text: str) -> str:
    return f"<discharge_document>\n{text}\n</discharge_document>"


TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "legible": {"type": "boolean"}},
    "required": ["text", "legible"],
}


async def transcriber(images: list[Image]) -> LLMResult:
    return await call_structured(
        "transcriber", _prompt("transcriber"),
        "Transcribe every line of text in these discharge papers.", TRANSCRIPT_SCHEMA, images=images,
    )


async def reader(doc_text: str) -> LLMResult:
    user = _doc_block(doc_text) + "\n\nExtract the discharge plan from the document above."
    return await call_structured("reader", _prompt("reader"), user, json_schema(Extraction))


async def reader_repair(doc_text: str, previous: Extraction, problems: list[str]) -> LLMResult:
    system = _prompt("reader") + "\n\n" + _prompt("reader_repair")
    user = (
        _doc_block(doc_text)
        + "\n\n<previous_extraction>\n" + previous.model_dump_json(indent=1) + "\n</previous_extraction>"
        + "\n\n<problems>\n" + "\n".join(f"- {p}" for p in problems) + "\n</problems>"
    )
    return await call_structured("reader", system, user, json_schema(Extraction))


CHECKER_TOOLS = ["list_medication_lines", "search_document", "check_quote", "verify_item"]


async def checker(doc_text: str, plan: Extraction) -> LLMResult:
    """The Checker reviews the plan with real tools: it searches the document and verifies items itself."""
    run_dir = tempfile.mkdtemp(prefix="gh-check-")
    Path(run_dir, "document.txt").write_text(doc_text)
    Path(run_dir, "plan.json").write_text(plan.model_dump_json())
    user = (
        _doc_block(doc_text)
        + "\n\n<plan>\n" + plan.model_dump_json(indent=1, exclude={"hospital_summary"}) + "\n</plan>"
        + "\n\nList every place where the plan does not match the document. Use your tools as described."
    )
    try:
        return await call_structured("checker", _prompt("checker"), user, json_schema(CheckerReport),
                                     tools=CHECKER_TOOLS, run_dir=run_dir)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


async def explainer(plan_data: dict, language: str, label_keys: dict[str, str]) -> LLMResult:
    # The Explainer never sees the full document: only verified items and their verified source sentences.
    user = (
        f"Target language: {language}\n\n"
        "<plan>\n" + json.dumps(plan_data, ensure_ascii=False, indent=1) + "\n</plan>\n\n"
        "<label_keys>\n" + json.dumps(label_keys, ensure_ascii=False, indent=1) + "\n</label_keys>"
    )
    return await call_structured("explainer", _prompt("explainer"), user, json_schema(Explanation))
