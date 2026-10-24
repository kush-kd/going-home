"""A stand-in for the model, used by the tests (and by tests/fake_server.py for UI checks).

It answers each agent with fixed data so the pipeline, checks and UI can be exercised
without any model access. It is never used by the real app.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.llm import LLMResult, Usage

FIXTURES = Path(__file__).parent / "fixtures"


def gold_01() -> dict:
    return json.loads((FIXTURES / "extraction_01.json").read_text())


def echo_explanation(plan: dict, labels: dict) -> dict:
    """A faithful 'explanation': restates the data without adding numbers."""
    return {
        "summary": plan.get("hospital_summary"),
        "medications": [
            {"id": m["id"], "what_for": m["purpose"],
             "how_to_take": ("Do not take this medicine." if m["status"] == "stop" else m["doctor_wrote"]),
             "tips": m["special_instructions"]}
            for m in plan["medications"]
        ],
        "warnings": [{"id": w["id"], "who_to_call": w["who_to_call"], "signs": w["signs"]} for w in plan["warnings"]],
        "instructions": [{"id": i["id"], "text": i["text"]} for i in plan["instructions"]],
        "appointments": [{"id": a["id"], "text": f"{a['with_whom']}."} for a in plan["appointments"]],
        "questions": [{"question": q, "reason": "Unclear in the papers"} for q in plan["questions_needed"]],
        "labels": labels,
    }


class FakeLLM:
    def __init__(self, reader=None, repair=None, checker=None, explainer=None, tokens=1000):
        self.reader = reader or gold_01()
        self.repair = repair
        self.checker = checker or [{"issues": [], "summary": "The plan matches the document."}]
        self.explainer = explainer
        self.tokens = tokens
        self.calls: list[str] = []

    async def __call__(self, agent, system, user, schema, images=None):
        self.calls.append(agent)
        if agent == "reader":
            data = copy.deepcopy(self.repair if ("<problems>" in user and self.repair) else self.reader)
        elif agent == "checker":
            data = self.checker[min(self.calls.count("checker") - 1, len(self.checker) - 1)]
        elif agent == "explainer":
            plan = json.loads(user.split("<plan>\n", 1)[1].split("\n</plan>", 1)[0])
            labels = json.loads(user.split("<label_keys>\n", 1)[1].split("\n</label_keys>", 1)[0])
            data = self.explainer(plan, labels) if self.explainer else echo_explanation(plan, labels)
        elif agent == "transcriber":
            data = {"text": "", "legible": False}
        else:
            raise AssertionError(agent)
        return LLMResult(data=data, usage=Usage(input_tokens=self.tokens, cache_read_tokens=self.tokens // 2, output_tokens=self.tokens // 4),
                         model=f"fake-{agent}", provider="fake", latency_s=0.01, cost_usd=0.001)
