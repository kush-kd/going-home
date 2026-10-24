"""Plain Python, no AI: verified medicines -> a day-by-day timetable and a calendar file.

Doses on the timetable come straight from the verified extraction, never from model prose,
so a translation slip cannot change what the family sees on the clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .models import Appointment, Extraction, Medication

SLOT_TIME = {"morning": "08:00", "midday": "12:00", "afternoon": "14:00", "evening": "18:00", "bedtime": "21:00"}
DEFAULT_HORIZON = 14
MAX_HORIZON = 45


def _parse(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def dose_times(med: Medication) -> list[str]:
    """Clock times (HH:MM) for one day of a fixed-schedule medicine."""
    meals = med.meal_relation
    if med.interval_hours and not med.times_of_day:
        return {
            24: ["08:00"], 12: ["08:00", "20:00"], 8: ["06:00", "14:00", "22:00"],
            6: ["06:00", "12:00", "18:00", "00:00"], 4: ["06:00", "10:00", "14:00", "18:00", "22:00"],
        }.get(med.interval_hours, ["08:00"])
    n = med.times_per_day or len(med.times_of_day) or 1
    if med.times_of_day and len(med.times_of_day) == n:
        times = [SLOT_TIME[t] for t in med.times_of_day]
        if meals == "before_breakfast":
            times = ["07:00" if t == "08:00" else t for t in times]
        return sorted(times)
    if n == 1:
        if meals == "before_breakfast" or meals == "empty_stomach":
            return ["07:00"]
        return ["08:00"]
    if n == 2:
        return ["08:00", "18:00"] if meals in ("with_food", "after_meals") else ["08:00", "20:00"]
    if n == 3:
        return ["08:00", "12:30", "18:00"] if meals in ("with_food", "after_meals") else ["08:00", "14:00", "20:00"]
    if n == 4:
        return ["08:00", "12:00", "16:00", "20:00"]
    step = max(1, 16 // n)
    return [f"{6 + i * step:02d}:00" for i in range(n)]


def _active_range(med: Medication, start: date) -> tuple[date, date | None]:
    first = _parse(med.start_date) or start
    last = _parse(med.end_date)
    if last is None and med.duration_days:
        last = first + timedelta(days=med.duration_days - 1)
    return first, last


def slot_label(hhmm: str) -> str:
    h = int(hhmm[:2])
    if h < 5:
        return "night"
    if h < 11:
        return "morning"
    if h < 13:
        return "midday"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "bedtime"


def _dose_text(med: Medication) -> str:
    return med.dose or med.strength or "see instructions"


def build(ex: Extraction, verified: dict[str, dict]) -> dict:
    start = _parse(ex.discharge_date) or date.today()
    scheduled, as_needed, stopped, unscheduled = [], [], [], []
    for med in ex.medications:
        ok = verified.get(med.id, {}).get("verified", False)
        if med.status == "stop":
            stopped.append(med.id)
        elif not ok:
            unscheduled.append({"id": med.id, "reason": "needs_check"})
        elif med.schedule_kind == "as_needed":
            as_needed.append(med.id)
        elif med.schedule_kind == "taper" and med.taper:
            scheduled.append(med)
        elif med.schedule_kind == "fixed" and (med.times_per_day or med.interval_hours or med.times_of_day):
            scheduled.append(med)
        else:
            unscheduled.append({"id": med.id, "reason": "unclear_schedule"})

    # Horizon: long enough to show every course ending, at least two weeks.
    last_end = start
    for med in scheduled:
        if med.schedule_kind == "taper":
            ends = [_parse(s.end_date) for s in med.taper]
            last_end = max([last_end] + [e for e in ends if e])
        else:
            _, end = _active_range(med, start)
            if end:
                last_end = max(last_end, end)
    horizon = min(MAX_HORIZON, max(DEFAULT_HORIZON, (last_end - start).days + 2))

    days = []
    for offset in range(horizon):
        day = start + timedelta(days=offset)
        slots: dict[str, list] = {}
        for med in scheduled:
            if med.schedule_kind == "taper":
                for step in med.taper:
                    s, e = _parse(step.start_date), _parse(step.end_date)
                    if s and e and s <= day <= e:
                        for t in dose_times(med) if (med.times_of_day or med.times_per_day) else ["08:00"]:
                            slots.setdefault(t, []).append({"med_id": med.id, "dose": step.dose, "last_day": day == e and step is med.taper[-1]})
                continue
            first, last = _active_range(med, start)
            if day < first or (last and day > last):
                continue
            for t in dose_times(med):
                slots.setdefault(t, []).append({"med_id": med.id, "dose": _dose_text(med), "last_day": bool(last and day == last)})
        days.append({
            "date": day.isoformat(),
            "slots": [{"time": t, "label": slot_label(t), "items": slots[t]} for t in sorted(slots, key=lambda x: (x == "00:00", x))],
        })

    courses = []
    for med in scheduled:
        first, last = _active_range(med, start)
        if med.schedule_kind == "taper":
            first = min(_parse(s.start_date) for s in med.taper)
            last = max(_parse(s.end_date) for s in med.taper)
        courses.append({"med_id": med.id, "first_day": first.isoformat(), "last_day": last.isoformat() if last else None,
                        "times": dose_times(med) if med.schedule_kind != "taper" or med.times_per_day else ["08:00"]})

    return {
        "start_date": start.isoformat(),
        "horizon_days": horizon,
        "days": days,
        "courses": courses,
        "as_needed": as_needed,
        "stopped": stopped,
        "unscheduled": unscheduled,
    }


# ------------------------------------------------------------------ calendar (.ics)

def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, cur = [], line
    while len(cur.encode()) > 74:
        cut = 74
        while len(cur[:cut].encode()) > 74:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def to_ics(schedule: dict, ex: Extraction, med_names: dict[str, str], appt_titles: dict[str, str] | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Going Home//Discharge Plan//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:Going Home medicines"]
    n = 0
    for day in schedule["days"]:
        d = day["date"].replace("-", "")
        for slot in day["slots"]:
            n += 1
            t = slot["time"].replace(":", "") + "00"
            names = [f"{med_names.get(i['med_id'], i['med_id'])}: {i['dose']}" for i in slot["items"]]
            summary = "Medicines: " + ", ".join(med_names.get(i["med_id"], i["med_id"]).split(" (")[0] for i in slot["items"])
            lines += [
                "BEGIN:VEVENT", f"UID:gh-dose-{d}-{t}-{n}@going-home", f"DTSTAMP:{stamp}",
                f"DTSTART:{d}T{t}", f"DURATION:PT15M",
                _fold("SUMMARY:" + _ics_escape(summary)),
                _fold("DESCRIPTION:" + _ics_escape("\n".join(names) + "\nFrom your hospital discharge papers. Check with your care team if unsure.")),
                "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT0M", _fold("DESCRIPTION:" + _ics_escape(summary)), "END:VALARM",
                "END:VEVENT",
            ]
    for appt in ex.appointments:
        if not appt.date:
            continue
        title = (appt_titles or {}).get(appt.id) or appt.with_whom
        d = appt.date.replace("-", "")
        if appt.time:
            start = f"DTSTART:{d}T{appt.time.replace(':', '')}00"
            dur = "DURATION:PT1H"
        else:
            start = f"DTSTART;VALUE=DATE:{d}"
            dur = "DURATION:P1D"
        desc = "\n".join(x for x in (appt.purpose, appt.time_window, appt.what_to_bring and f"Bring: {appt.what_to_bring}", appt.phone and f"Phone: {appt.phone}") if x)
        lines += [
            "BEGIN:VEVENT", f"UID:gh-appt-{appt.id}-{d}@going-home", f"DTSTAMP:{stamp}", start, dur,
            _fold("SUMMARY:" + _ics_escape(title)),
            _fold("LOCATION:" + _ics_escape(appt.location or "")),
            _fold("DESCRIPTION:" + _ics_escape(desc)),
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-P1D", _fold("DESCRIPTION:" + _ics_escape("Tomorrow: " + title)), "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def appointment_sort_key(a: Appointment) -> tuple:
    return (a.date or "9999-99-99", a.time or "99:99")
