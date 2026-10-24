"""Every structured type the agents produce or consume."""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["new", "changed", "continue", "stop"]
ScheduleKind = Literal["fixed", "as_needed", "taper", "unclear"]
TimeOfDay = Literal["morning", "midday", "afternoon", "evening", "bedtime"]
MealRelation = Literal["with_food", "empty_stomach", "before_breakfast", "after_meals", "none"]


# ---------------------------------------------------------------- Reader output

class TaperStep(BaseModel):
    start_date: str = Field(description="YYYY-MM-DD, first day of this step")
    end_date: str = Field(description="YYYY-MM-DD, last day of this step")
    dose: str = Field(description="Dose for these days exactly as written, e.g. '4 tablets (40 mg)'")


class Medication(BaseModel):
    id: str = Field(description="m1, m2, ... in document order")
    name: str = Field(description="Medicine name as written, including brand in brackets if given")
    strength: str | None = Field(description="Strength per unit as written, e.g. '40 mg', '100 units/mL'")
    dose: str | None = Field(description="Amount per dose as written, e.g. '1 tablet', '18 units', '2 puffs', '1/2 tablet (12.5 mg)'")
    route: str | None = Field(description="e.g. 'by mouth', 'under the skin', 'inhaled', 'on the skin'")
    status: Status = Field(description="new = START list, changed = CHANGE list, continue = CONTINUE list, stop = STOP / do not take")
    schedule_kind: ScheduleKind = Field(description="fixed = regular schedule, as_needed = only when needed, taper = dose changes by date, unclear = the document does not say clearly")
    times_per_day: int | None = Field(description="Regular doses per day (once=1, twice=2, three times=3). Null for as_needed/taper/unclear unless stated")
    interval_hours: int | None = Field(description="If written as 'every N hours', N (use the shortest N for ranges like 4 to 6)")
    times_of_day: list[TimeOfDay] = Field(description="Only times of day the document states (e.g. 'in the morning', 'at bedtime'); empty if not stated")
    meal_relation: MealRelation = Field(description="with_food, empty_stomach, before_breakfast, after_meals, or none if not stated")
    duration_days: int | None = Field(description="Number of days if stated")
    start_date: str | None = Field(description="YYYY-MM-DD if the document gives a start date, else null")
    end_date: str | None = Field(description="YYYY-MM-DD of the last dose if stated, else null")
    taper: list[TaperStep] = Field(description="Steps for a taper; empty otherwise")
    max_per_day: str | None = Field(description="Daily maximum as written, e.g. 'No more than 4 tablets per day'")
    purpose: str | None = Field(description="Purpose ONLY if the document states it, in the document's words; else null")
    special_instructions: list[str] = Field(description="Other instructions as written, e.g. 'Do not crush or chew', 'Swallow whole'")
    source_quote: str = Field(description="Exact, contiguous text copied from the document that contains this medicine's name and instructions")


class Appointment(BaseModel):
    id: str = Field(description="a1, a2, ...")
    kind: Literal["visit", "test", "call_to_schedule", "they_will_call"]
    with_whom: str = Field(description="Clinic, doctor or test name as written")
    date: str | None = Field(description="YYYY-MM-DD if a date is given")
    time: str | None = Field(description="24-hour HH:MM if a time is given")
    time_window: str | None = Field(description="e.g. 'between 8:00 AM and 4:00 PM', 'within 2 weeks'")
    location: str | None
    purpose: str | None
    what_to_bring: str | None
    phone: str | None = Field(description="Phone number as written, if any")
    source_quote: str


class WarningGroup(BaseModel):
    id: str = Field(description="w1, w2, ...")
    level: Literal["emergency", "call"] = Field(description="emergency = call 911 / go to ER; call = call the clinic or nurse line")
    who_to_call: str = Field(description="As written, e.g. 'Heart Failure Nurse Line', '911 or the nearest emergency room'")
    phone: str | None = Field(description="Phone number as written, if any")
    signs: list[str] = Field(description="Each warning sign as its own short item, in the document's words")
    source_quote: str


class Instruction(BaseModel):
    id: str = Field(description="i1, i2, ...")
    category: Literal["diet", "activity", "monitoring", "wound_care", "equipment", "other"]
    text: str = Field(description="The instruction as written")
    source_quote: str


class UnclearItem(BaseModel):
    id: str = Field(description="u1, u2, ...")
    text: str = Field(description="What the document says")
    reason: str = Field(description="Why a family could not act on it, e.g. 'does not say which medicines or doses'")
    source_quote: str


class Extraction(BaseModel):
    discharge_date: str | None = Field(description="YYYY-MM-DD")
    diagnosis: str | None = Field(description="Main diagnosis or procedure as written")
    hospital_summary: str | None = Field(description="What happened in hospital, as written (1-3 sentences)")
    medications: list[Medication]
    appointments: list[Appointment]
    warnings: list[WarningGroup]
    instructions: list[Instruction]
    unclear_items: list[UnclearItem]


# ---------------------------------------------------------------- Checker output

class CheckerIssue(BaseModel):
    severity: Literal["high", "medium", "low"] = Field(description="high = could cause a wrong dose, a missed medicine, or a missed danger sign")
    issue_type: Literal["missing_item", "wrong_value", "wrong_status", "not_in_document", "other"]
    item_id: str | None = Field(description="id of the plan item this is about, or null if the item is missing")
    document_quote: str = Field(description="Exact text from the document that shows the problem (empty only for not_in_document)")
    problem: str
    fix: str


class CheckerReport(BaseModel):
    issues: list[CheckerIssue]
    summary: str = Field(description="One sentence on how well the plan matches the document")


# ---------------------------------------------------------------- Explainer output

class MedCard(BaseModel):
    id: str
    what_for: str | None = Field(description="Plain-language purpose, only from the given purpose; null if none given")
    how_to_take: str = Field(description="Plain-language instructions using ONLY the numbers in the given data")
    tips: list[str]


class WarningCard(BaseModel):
    id: str
    who_to_call: str
    signs: list[str]


class PlainItem(BaseModel):
    id: str
    text: str


class Question(BaseModel):
    question: str = Field(description="A question the family can ask the doctor or pharmacist, first person")
    reason: str


class Explanation(BaseModel):
    summary: str | None = Field(description="2-3 plain sentences on why the patient was in hospital; null if no diagnosis given")
    medications: list[MedCard]
    warnings: list[WarningCard]
    instructions: list[PlainItem]
    appointments: list[PlainItem]
    questions: list[Question]
    labels: dict[str, str] = Field(description="Every requested UI label key translated into the target language")


# ---------------------------------------------------------------- JSON Schema helper

def json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema with every $ref inlined, so any structured-output backend accepts it."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return inline(copy.deepcopy(defs[name]))
            return {k: inline(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(raw)
