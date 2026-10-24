"""The orchestrator: runs the agents in order, applies every check, and records what each step cost.

    intake (safety filters) -> [Transcriber if photo] -> Reader -> grounding check
        -> Checker  ||  Explainer (started speculatively in parallel)
        -> [one repair round: Reader repair -> grounding -> Checker -> Explainer]
        -> Scheduler (plain Python) -> number check on the Explainer's text -> calendar file
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import date, datetime
from typing import Any, Callable

from pydantic import ValidationError

from . import agents, config, grounding, intake, scheduler
from .llm import LLMError, LLMResult, provider
from .models import CheckerReport, Explanation, Extraction, Medication

LABEL_KEYS = {
    "summary": "Why you were in the hospital", "today": "Today", "timetable": "Medicine timetable",
    "medicines": "Medicines", "warnings": "Warning signs", "appointments": "Appointments",
    "questions": "Questions for the doctor", "instructions": "Diet and activity", "as_needed": "Only if needed",
    "stop": "Stop taking these", "stop_short": "Stop", "new": "New", "changed": "Changed", "continue": "Keep taking",
    "emergency": "Call 911 or go to the emergency room", "call": "Call your care team if",
    "morning": "Morning", "midday": "Midday", "afternoon": "Afternoon", "evening": "Evening",
    "bedtime": "Bedtime", "night": "Night", "last_day": "Last day", "what_for": "What it is for",
    "how_to_take": "How to take it", "print_chart": "Print weekly chart", "calendar": "Add to my calendar",
    "check_with_team": "Check with your care team", "max": "Daily limit", "weekly_chart": "Weekly medicine chart",
    "given": "Given", "done": "Done", "new_papers": "New papers",
}


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Stops a run before it can spend more than its share of tokens or model calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.tokens = 0

    def before_call(self) -> None:
        if self.calls >= config.MAX_LLM_CALLS_PER_RUN:
            raise BudgetExceeded(f"Stopped: this run reached its limit of {config.MAX_LLM_CALLS_PER_RUN} model calls.")
        if self.tokens >= config.MAX_TOKENS_PER_RUN:
            raise BudgetExceeded(f"Stopped: this run reached its limit of {config.MAX_TOKENS_PER_RUN:,} tokens.")
        self.calls += 1

    def after_call(self, r: LLMResult) -> None:
        self.tokens += r.usage.billable


def _usage_file() -> Any:
    config.LOG_DIR.mkdir(exist_ok=True)
    return config.LOG_DIR / f"usage_{date.today().isoformat()}.json"


def check_daily_cap() -> int:
    f = _usage_file()
    runs = json.loads(f.read_text()).get("runs", 0) if f.exists() else 0
    if runs >= config.MAX_RUNS_PER_DAY:
        raise BudgetExceeded(f"Daily limit reached ({config.MAX_RUNS_PER_DAY} plans per day). Try again tomorrow.")
    f.write_text(json.dumps({"runs": runs + 1}))
    return runs + 1


class Run:
    def __init__(self, language: str, source: str) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.language = language
        self.source = source
        self.created = datetime.now().isoformat(timespec="seconds")
        self.status = "running"
        self.steps: list[dict] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.calls: list[dict] = []
        self.budget = Budget()
        self.started = time.monotonic()
        self.provider: str | None = None

    # Step events drive the live "how this plan is being made" panel.
    def step(self, key: str, title: str, status: str = "running", **detail) -> dict:
        for s in self.steps:
            if s["key"] == key:
                s.update(status=status, **detail)
                if status != "running":
                    s["elapsed_s"] = round(time.monotonic() - s["_t0"], 1)
                return s
        s = {"key": key, "title": title, "status": status, "_t0": time.monotonic(), **detail}
        self.steps.append(s)
        return s

    def public(self) -> dict:
        return {
            "id": self.id, "status": self.status, "error": self.error, "language": self.language,
            "steps": [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.steps],
            "result": self.result,
        }


RUNS: dict[str, Run] = {}


async def _call(run: Run, step_key: str, title: str, agent: str, coro_factory: Callable) -> LLMResult:
    run.budget.before_call()
    run.step(step_key, title, "running", agent=agent)
    try:
        r: LLMResult = await coro_factory()
    except LLMError as e:
        run.step(step_key, title, "error", message=str(e))
        raise
    run.budget.after_call(r)
    call = {"step": step_key, "agent": agent, "model": r.model, "provider": r.provider, "latency_s": r.latency_s,
            "cost_usd": r.cost_usd, **r.usage.as_dict()}
    run.calls.append(call)
    run.step(step_key, title, "done", agent=agent, model=r.model, tokens=r.usage.as_dict(), cost_usd=r.cost_usd, latency_s=r.latency_s)
    return r


def _parse_extraction(data: dict) -> Extraction:
    try:
        return Extraction.model_validate(data)
    except ValidationError as e:
        raise LLMError(f"The Reader returned data in the wrong shape: {e.errors()[0].get('msg')}")


def _checker_issues(report: CheckerReport, doc_norm: str) -> tuple[list[dict], list[dict]]:
    """Keep issues whose quote is really in the document; drop the rest as unsupported."""
    kept, dropped = [], []
    for issue in report.issues:
        d = issue.model_dump()
        if issue.issue_type == "not_in_document" or grounding.quote_in_document(issue.document_quote, doc_norm)[0]:
            kept.append(d)
        else:
            dropped.append(d)
    return kept, dropped


def _problems_for_repair(ex: Extraction, verified: dict, issues: list[dict]) -> list[str]:
    probs = []
    for item_id, v in verified.items():
        for p in v["problems"]:
            probs.append(f"{item_id}: {p}")
    for i in issues:
        if i["severity"] in ("high", "medium"):
            where = i["item_id"] or "missing item"
            probs.append(f"{where}: {i['problem']} Document says: \"{i['document_quote']}\". Suggested fix: {i['fix']}")
    return probs


def _fmt12(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:]
    return f"{(h % 12) or 12}:{m} {'AM' if h < 12 else 'PM'}"


def _plan_for_explainer(ex: Extraction, verified: dict, schedule: dict, questions: list[str]) -> dict:
    times = {c["med_id"]: c for c in schedule["courses"]}
    meds = []
    for m in ex.medications:
        c = times.get(m.id)
        meds.append({
            "id": m.id, "name": m.name, "strength": m.strength, "dose": m.dose, "route": m.route, "status": m.status,
            "schedule_kind": m.schedule_kind, "times_per_day": m.times_per_day, "interval_hours": m.interval_hours,
            "meal_relation": m.meal_relation, "duration_days": m.duration_days, "start_date": m.start_date,
            "end_date": m.end_date, "taper": [s.model_dump() for s in m.taper], "max_per_day": m.max_per_day,
            "purpose": m.purpose, "special_instructions": m.special_instructions,
            "clock_times_on_timetable": [_fmt12(t) for t in c["times"]] if c else [],
            "doctor_wrote": m.source_quote,
            "on_timetable": bool(c),
        })
    return {
        "diagnosis": ex.diagnosis, "hospital_summary": ex.hospital_summary, "discharge_date": ex.discharge_date,
        "medications": meds,
        "warnings": [w.model_dump(exclude={"source_quote"}) for w in ex.warnings],
        "instructions": [{"id": i.id, "text": i.text} for i in ex.instructions],
        "appointments": [a.model_dump(exclude={"source_quote"}) for a in ex.appointments],
        "questions_needed": questions,
    }


def _allowed_numbers_source(m: Medication, schedule: dict) -> str:
    c = next((c for c in schedule["courses"] if c["med_id"] == m.id), None)
    extra = " ".join(_fmt12(t) + " " + t for t in (c["times"] if c else []))
    # Frequency, interval and duration were checked against the quote by the grounding step,
    # so "twice daily" may be explained as "2 times a day".
    counts = " ".join(str(n) for n in (m.times_per_day, m.interval_hours, m.duration_days, len(c["times"]) if c else None) if n)
    return " ".join(filter(None, [m.source_quote, m.strength, m.dose, m.max_per_day, extra, counts, m.purpose, *m.special_instructions,
                                  m.start_date, m.end_date, " ".join(s.start_date + " " + s.end_date + " " + s.dose for s in m.taper)]))


def _validate_explanation(exp: Explanation, ex: Extraction, schedule: dict) -> tuple[dict, list[dict]]:
    """Throw out any explained text that introduces a number not in the verified data."""
    flags = []
    cards = {c.id: c for c in exp.medications}
    out_meds = {}
    for m in ex.medications:
        c = cards.get(m.id)
        allowed = _allowed_numbers_source(m, schedule)
        if c is None:
            flags.append({"id": m.id, "problem": "missing explanation; showing the doctor's words"})
            out_meds[m.id] = {"what_for": None, "how_to_take": m.source_quote, "tips": [], "fallback": True}
            continue
        text = " ".join([c.how_to_take, c.what_for or ""] + c.tips)
        bad = grounding.check_explanation_numbers(text, allowed)
        if bad:
            flags.append({"id": m.id, "problem": f"explanation had numbers not in the document ({', '.join(sorted(bad))}); showing the doctor's words"})
            out_meds[m.id] = {"what_for": None, "how_to_take": m.source_quote, "tips": [], "fallback": True}
        else:
            out_meds[m.id] = {"what_for": c.what_for, "how_to_take": c.how_to_take, "tips": c.tips, "fallback": False}

    def simple(items, src_items, kind):
        src = {s.id: s for s in src_items}
        res = {}
        given = {i.id: i for i in items}
        for sid, s in src.items():
            i = given.get(sid)
            # Allowed numbers: everything about this item that the grounding check verified against the document.
            fields = [s.source_quote, getattr(s, "text", None), getattr(s, "date", None), getattr(s, "phone", None),
                      getattr(s, "time", None) and (_fmt12(s.time) + " " + s.time)]
            fields += [getattr(s, k, None) for k in ("time_window", "location", "purpose", "what_to_bring")]
            fields += list(getattr(s, "signs", []) or [])
            source_text = " ".join(f for f in fields if f)
            if i is None:
                res[sid] = {"text": s.source_quote, "fallback": True}
                continue
            text = i.text if hasattr(i, "text") else " ".join(i.signs)
            bad = grounding.check_explanation_numbers(text, source_text)
            if bad:
                flags.append({"id": sid, "problem": f"{kind} explanation had new numbers ({', '.join(sorted(bad))}); showing the doctor's words"})
                res[sid] = {"text": s.source_quote, "fallback": True}
            else:
                res[sid] = {"text": i.text, "fallback": False} if hasattr(i, "text") else {"who_to_call": i.who_to_call, "signs": i.signs, "fallback": False}
        return res

    warnings = simple(exp.warnings, ex.warnings, "warning")
    for w in ex.warnings:   # fallback warnings keep the document's split signs
        if warnings[w.id].get("fallback"):
            warnings[w.id] = {"who_to_call": w.who_to_call, "signs": w.signs, "fallback": True}
    summary = exp.summary
    if summary and grounding.check_explanation_numbers(summary, f"{ex.diagnosis or ''} {ex.hospital_summary or ''}"):
        flags.append({"id": "summary", "problem": "summary had new numbers; showing the hospital's words"})
        summary = ex.hospital_summary or ex.diagnosis
    labels = {k: (exp.labels.get(k) or v) for k, v in LABEL_KEYS.items()}
    return {
        "summary": summary,
        "medications": out_meds,
        "warnings": warnings,
        "instructions": simple(exp.instructions, ex.instructions, "instruction"),
        "appointments": simple(exp.appointments, ex.appointments, "appointment"),
        "questions": [q.model_dump() for q in exp.questions],
        "labels": labels,
    }, flags


async def execute(run: Run, filename: str, data: bytes) -> None:
    try:
        await _execute(run, filename, data)
        run.status = "done"
    except (intake.IntakeError, BudgetExceeded, LLMError) as e:
        run.status, run.error = "error", str(e)
    except Exception as e:  # noqa: BLE001 - report anything unexpected instead of hanging the UI
        run.status, run.error = "error", f"Unexpected error: {e.__class__.__name__}: {e}"
    finally:
        _log(run)


async def _execute(run: Run, filename: str, data: bytes) -> None:
    run.step("intake", "Safety screen of the upload")
    run.provider = provider()
    check_daily_cap()
    doc = intake.load(filename, data)
    run.step("intake", "Safety screen of the upload", "done", pages=doc.pages, hidden_text=len(doc.hidden_text), removed_lines=len(doc.removed_lines))

    if doc.images:
        r = await _call(run, "transcriber", "Transcriber reads the photo", "transcriber", lambda: agents.transcriber(doc.images))
        intake.finalize_text(doc, r.data.get("text", ""))
        if not doc.text.strip() or not r.data.get("legible", True):
            raise intake.IntakeError("The photo could not be read. Try a sharper, well-lit photo of the whole page.")
    doc_norm = grounding.normalize(doc.text)

    r = await _call(run, "reader", "Reader extracts medicines, visits and warnings", "reader", lambda: agents.reader(doc.text))
    ex = _parse_extraction(r.data)

    run.step("grounding", "Program checks every quote and number against the document")
    verified = grounding.verify_extraction(ex, doc.text)
    n_bad = sum(not v["verified"] for v in verified.values())
    run.step("grounding", "Program checks every quote and number against the document", "done", items=len(verified), failed=n_bad)

    # Checker and Explainer in parallel: the Explainer usually does not need to wait.
    def explain_inputs(ex_: Extraction, ver_: dict, issues_: list[dict]):
        sched_ = scheduler.build(ex_, ver_)
        qs = [f"{u.text} ({u.reason})" for u in ex_.unclear_items]
        qs += [f"The plan could not confirm '{m.name}': {'; '.join(ver_[m.id]['problems'])}" for m in ex_.medications if not ver_.get(m.id, {}).get("verified")]
        qs += [f"{i['problem']} (document says: {i['document_quote']})" for i in issues_ if i["severity"] in ("high", "medium")]
        return sched_, _plan_for_explainer(ex_, ver_, sched_, qs), qs

    sched, plan_data, questions = explain_inputs(ex, verified, [])
    explained_questions = list(questions)
    lang = run.language
    check_task = asyncio.create_task(_call(run, "checker", "Checker compares the plan with the document", "checker", lambda: agents.checker(doc.text, ex)))
    explain_task = asyncio.create_task(_call(run, "explainer", f"Explainer writes it in plain {lang}", "explainer", lambda: agents.explainer(plan_data, lang, LABEL_KEYS)))
    try:
        cr = await check_task
    except Exception:
        explain_task.cancel()
        raise
    report = CheckerReport.model_validate(cr.data)
    issues, dropped = _checker_issues(report, doc_norm)
    initial_issues = issues
    run.step("checker", "Checker compares the plan with the document", "done", issues=len(issues), dropped=len(dropped))

    repaired = False
    rechecked = False
    initial_grounding = {k: v["problems"] for k, v in verified.items() if v["problems"]}
    problems = _problems_for_repair(ex, verified, issues)
    rounds = 0
    while problems and rounds < config.MAX_REPAIR_ROUNDS:
        rounds += 1
        repaired = True
        explain_task.cancel()
        run.step("explainer", f"Explainer writes it in plain {lang}", "restarted", message="restarted after the repair")
        r = await _call(run, "repair", "Reader fixes the problems found", "reader", lambda: agents.reader_repair(doc.text, ex, problems))
        ex = _parse_extraction(r.data)
        verified = grounding.verify_extraction(ex, doc.text)
        if any(i["severity"] in ("high", "medium") for i in issues):
            # The Checker raised the problems, so it reviews the fix. When only the quote check
            # flagged items, re-running that check (free, above) is enough.
            rechecked = True
            cr = await _call(run, "recheck", "Checker reviews the fixed plan", "checker", lambda: agents.checker(doc.text, ex))
            issues, more_dropped = _checker_issues(CheckerReport.model_validate(cr.data), doc_norm)
            dropped += more_dropped
        problems = []   # one repair round only; anything left is shown to the family
        sched, plan_data, questions = explain_inputs(ex, verified, issues)
        explained_questions = list(questions)
        explain_task = asyncio.create_task(_call(run, "explainer", f"Explainer writes it in plain {lang}", "explainer", lambda: agents.explainer(plan_data, lang, LABEL_KEYS)))

    if not repaired and issues:
        # Low-severity issues only: keep the speculative explanation, add the questions.
        sched, plan_data, questions = explain_inputs(ex, verified, issues)
    er = await explain_task

    run.step("schedule", "Scheduler builds the timetable and calendar (plain Python)")
    sched = scheduler.build(ex, verified)
    run.step("schedule", "Scheduler builds the timetable and calendar (plain Python)", "done", days=sched["horizon_days"])

    run.step("numbers", "Program checks every number the Explainer wrote")
    exp, exp_flags = _validate_explanation(Explanation.model_validate(er.data), ex, sched)
    # Questions that came up after the Explainer started still reach the family, in the checks' own words.
    for q in questions:
        if q not in explained_questions:
            exp["questions"].append({"question": q, "reason": "Flagged by the safety checks", "fallback": True})
    run.step("numbers", "Program checks every number the Explainer wrote", "done", rejected=len(exp_flags))

    med_names = {m.id: f"{m.name} {m.strength or ''}".strip() for m in ex.medications}
    ics = scheduler.to_ics(sched, ex, med_names, {a.id: a.with_whom for a in ex.appointments})

    english_out = ". ".join([exp["summary"] or ""] + [v["how_to_take"] for v in exp["medications"].values()] +
                           [s for w in exp["warnings"].values() for s in w.get("signs", [])] +
                           [v["text"] for v in exp["instructions"].values()]) if lang.lower().startswith("english") else ""
    totals = {k: sum(c[k] for c in run.calls) for k in ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")}
    costs = [c["cost_usd"] for c in run.calls if c["cost_usd"] is not None]
    run.result = {
        "language": lang,
        "document": {"pages": doc.pages, "source_kind": doc.source_kind, "hidden_text": doc.hidden_text,
                     "removed_lines": doc.removed_lines, "chars": len(doc.text)},
        "extraction": ex.model_dump(),
        "verification": verified,
        "checker": {"initial_issues": initial_issues, "remaining_issues": issues, "dropped_unsupported": dropped, "repaired": repaired,
                    "rechecked": rechecked, "initial_grounding_problems": initial_grounding},
        "explanation": exp,
        "explainer_flags": exp_flags,
        "schedule": sched,
        "ics": ics,
        "metrics": {
            "reading_grade_document": grounding.reading_grade(doc.text),
            "reading_grade_plan": grounding.reading_grade(english_out) if english_out else None,
            "calls": run.calls, "totals": totals, "total_tokens": sum(totals.values()),
            "cost_usd": round(sum(costs), 4) if costs else None,
            "elapsed_s": round(time.monotonic() - run.started, 1),
            "provider": run.provider,
        },
    }


def _log(run: Run) -> None:
    """Append a privacy-safe record (no document text, no names) for the tokenomics analysis."""
    config.LOG_DIR.mkdir(exist_ok=True)
    res = run.result or {}
    ex = res.get("extraction") or {}
    rec = {
        "run_id": run.id, "ts": run.created, "source": run.source, "language": run.language, "status": run.status,
        "error": run.error, "provider": getattr(run, "provider", None), "calls": run.calls,
        "elapsed_s": round(time.monotonic() - run.started, 1),
        "medications": len(ex.get("medications", [])),
        "unverified_items": sum(not v["verified"] for v in (res.get("verification") or {}).values()),
        "checker_initial_issues": len((res.get("checker") or {}).get("initial_issues", [])),
        "checker_remaining_issues": len((res.get("checker") or {}).get("remaining_issues", [])),
        "repaired": (res.get("checker") or {}).get("repaired"),
        "explainer_rejections": len(res.get("explainer_flags", [])),
        "hidden_text_blocks": len((res.get("document") or {}).get("hidden_text", [])),
        "injection_lines_removed": len((res.get("document") or {}).get("removed_lines", [])),
        "reading_grade_document": (res.get("metrics") or {}).get("reading_grade_document"),
        "reading_grade_plan": (res.get("metrics") or {}).get("reading_grade_plan"),
    }
    with open(config.LOG_DIR / "runs.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
