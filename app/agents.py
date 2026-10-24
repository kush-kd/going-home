"""The four agents. Each one is a single structured call with its own prompt and schema."""

from __future__ import annotations

import json

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


async def checker(doc_text: str, plan: Extraction) -> LLMResult:
    user = (
        _doc_block(doc_text)
        + "\n\n<plan>\n" + plan.model_dump_json(indent=1, exclude={"hospital_summary"}) + "\n</plan>"
        + "\n\nList every place where the plan does not match the document."
    )
    return await call_structured("checker", _prompt("checker"), user, json_schema(CheckerReport))


async def explainer(plan_data: dict, language: str, label_keys: dict[str, str]) -> LLMResult:
    # The Explainer never sees the raw document: only verified, structured data.
    user = (
        f"Target language: {language}\n\n"
        "<plan>\n" + json.dumps(plan_data, ensure_ascii=False, indent=1) + "\n</plan>\n\n"
        "<label_keys>\n" + json.dumps(label_keys, ensure_ascii=False, indent=1) + "\n</label_keys>"
    )
    return await call_structured("explainer", _prompt("explainer"), user, json_schema(Explanation))
