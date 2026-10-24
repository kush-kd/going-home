"""The deterministic parts: scheduler, calendar file, and the full pipeline on a correct read."""

import asyncio
from datetime import date

from app import agents, pipeline, scheduler
from app.models import Extraction, Medication
from fake_llm import FakeLLM, gold_01

HF = "01_heart_failure_ramesh_mehta.pdf"


def med(**kw) -> Medication:
    base = dict(id="m1", name="X", strength="10 mg", dose="1 tablet", route="by mouth", status="new", schedule_kind="fixed",
                times_per_day=1, interval_hours=None, times_of_day=[], meal_relation="none", duration_days=None, start_date=None,
                end_date=None, taper=[], max_per_day=None, purpose=None, special_instructions=[], source_quote="X")
    base.update(kw)
    return Medication(**base)


def test_dose_times():
    assert scheduler.dose_times(med(times_per_day=1)) == ["08:00"]
    assert scheduler.dose_times(med(times_per_day=1, times_of_day=["bedtime"])) == ["21:00"]
    assert scheduler.dose_times(med(times_per_day=1, meal_relation="before_breakfast")) == ["07:00"]
    assert scheduler.dose_times(med(times_per_day=2, meal_relation="with_food")) == ["08:00", "18:00"]
    assert scheduler.dose_times(med(times_per_day=3)) == ["08:00", "14:00", "20:00"]
    assert scheduler.dose_times(med(times_per_day=3, interval_hours=8)) == ["06:00", "14:00", "22:00"]


def _ex(meds, discharge="2026-10-17"):
    return Extraction(discharge_date=discharge, diagnosis=None, hospital_summary=None, medications=meds,
                      appointments=[], warnings=[], instructions=[], unclear_items=[])


def test_course_with_duration_ends_on_the_right_day():
    m = med(times_per_day=2, meal_relation="with_food", duration_days=5)
    s = scheduler.build(_ex([m]), {"m1": {"verified": True}})
    days_with_dose = [d["date"] for d in s["days"] if d["slots"]]
    assert days_with_dose == ["2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"]
    last = s["days"][4]["slots"][-1]["items"][0]
    assert last["last_day"] is True


def test_taper_gives_the_right_dose_each_day():
    steps = [
        {"start_date": "2026-10-17", "end_date": "2026-10-18", "dose": "4 tablets (40 mg)"},
        {"start_date": "2026-10-19", "end_date": "2026-10-20", "dose": "3 tablets (30 mg)"},
        {"start_date": "2026-10-21", "end_date": "2026-10-22", "dose": "2 tablets (20 mg)"},
        {"start_date": "2026-10-23", "end_date": "2026-10-24", "dose": "1 tablet (10 mg)"},
    ]
    m = med(schedule_kind="taper", times_per_day=1, meal_relation="with_food", taper=steps)
    s = scheduler.build(_ex([m], discharge="2026-10-16"), {"m1": {"verified": True}})
    by_day = {d["date"]: [i["dose"] for sl in d["slots"] for i in sl["items"]] for d in s["days"]}
    assert by_day["2026-10-16"] == []
    assert by_day["2026-10-18"] == ["4 tablets (40 mg)"]
    assert by_day["2026-10-20"] == ["3 tablets (30 mg)"]
    assert by_day["2026-10-24"] == ["1 tablet (10 mg)"]
    assert by_day["2026-10-25"] == []


def test_stopped_unverified_and_as_needed_meds_are_never_on_the_clock():
    meds = [med(id="m1", status="stop"), med(id="m2"), med(id="m3", schedule_kind="as_needed", times_per_day=None), med(id="m4", schedule_kind="unclear", times_per_day=None)]
    s = scheduler.build(_ex(meds), {"m1": {"verified": True}, "m2": {"verified": False}, "m3": {"verified": True}, "m4": {"verified": True}})
    on_clock = {i["med_id"] for d in s["days"] for sl in d["slots"] for i in sl["items"]}
    assert on_clock == set()
    assert s["stopped"] == ["m1"] and s["as_needed"] == ["m3"]
    assert {"id": "m2", "reason": "needs_check"} in s["unscheduled"]
    assert {"id": "m4", "reason": "unclear_schedule"} in s["unscheduled"]


def test_full_pipeline_on_a_correct_read(monkeypatch, sample_bytes):
    fake = FakeLLM()
    monkeypatch.setattr(agents, "call_structured", fake)
    run = pipeline.Run("English", "test")
    asyncio.run(pipeline.execute(run, HF, sample_bytes(HF)))
    assert run.status == "done", run.error
    r = run.result
    assert all(v["verified"] for v in r["verification"].values())
    assert fake.calls.count("reader") == 1 and fake.calls.count("checker") == 1 and fake.calls.count("explainer") == 1
    first = r["schedule"]["days"][0]
    assert first["date"] == "2026-10-16"
    times = {sl["time"]: [i["med_id"] for i in sl["items"]] for sl in first["slots"]}
    assert times == {"08:00": ["m1", "m2", "m3", "m4"], "21:00": ["m5"]}
    assert r["schedule"]["stopped"] == ["m6", "m7"]
    ics = r["ics"]
    assert ics.startswith("BEGIN:VCALENDAR") and "Cardiology clinic" in ics and "DTSTART:20261022T103000" in ics
    assert r["metrics"]["total_tokens"] > 0
    assert r["metrics"]["reading_grade_document"] is not None
