"""Simulated reader study (AI personas, NOT real people).

Each persona answers the questions a caregiver must get right, twice:
  A) from the original discharge papers (English, clinical shorthand), and
  B) from the Going Home plan for that case (in the persona's language).
A separate grader scores every answer against a hand-written answer key.
This is a proxy that gives directional evidence; it does not replace a real-user test.

    .venv/bin/python scripts/simulated_readers.py
Writes docs/results/simulated_readers.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, intake, llm  # noqa: E402

config.CLAUDE_MODELS["persona"] = "haiku"     # a smaller model plays a non-expert reader
config.CLAUDE_MODELS["grader"] = "sonnet"
REC = ROOT / "data" / "recorded"

CASES = [
    {
        "sample": "05_stroke_afib_chen_guoliang.pdf", "recording": "05_stroke_afib_chen_guoliang__Chinese",
        "persona": "You are Mei, 52, Mr. Chen's daughter and caregiver. You read simplified Chinese fluently. You read English "
                   "slowly, only simple everyday words, and you do not know medical abbreviations such as PO, BID, QHS or PRN.",
        "questions": [
            ("Which medicines should your father take in the evening, and how much of each?", "Apixaban 5 mg (1 tablet), metoprolol 25 mg (1 tablet), metformin 500 mg (1 tablet, with dinner); atorvastatin 80 mg at bedtime"),
            ("Which medicines must he stop taking?", "Aspirin 81 mg and ginkgo biloba"),
            ("Name three warning signs that mean you should call 911.", "Any three of: face drooping, arm or leg weakness, trouble speaking, sudden vision loss, loss of balance, sudden severe headache, vomiting blood or black stools"),
            ("When and where is the neurology appointment?", "Wednesday November 4, 2026 at 9:00 AM, Maplewood Neuroscience Center, 4th floor"),
            ("What is the most acetaminophen (Tylenol) he can take in one day?", "3,000 mg per day"),
            ("What changed about his atorvastatin?", "Dose increased from 20 mg to 80 mg, taken at bedtime"),
        ],
    },
    {
        "sample": "02_pneumonia_diabetes_maria_garcia.pdf", "recording": "02_pneumonia_diabetes_maria_garcia__Spanish",
        "persona": "You are Rosa, 60, Maria Garcia's sister, helping her at home. You read Spanish fluently and only basic English. "
                   "You have no medical training.",
        "questions": [
            ("How many days does Maria take the antibiotic, and when is the last dose?", "5 days; last dose the evening of 10/21/2026"),
            ("How much insulin does she inject, and when?", "18 units, once daily at bedtime"),
            ("What should she do if her blood sugar is below 70?", "Eat or drink 15 grams of fast sugar (4 oz juice or 4 glucose tablets), wait 15 minutes, check again"),
            ("When should you call 911?", "Trouble breathing; lips or face blue or gray; confused or very sleepy; blood sugar below 54 that does not come up, or passing out"),
            ("When is her follow-up appointment?", "Friday 10/23/2026 at 9:15 AM with Dr. Laura Chen, Maplewood Primary Care, Suite 110"),
            ("How often can she take acetaminophen, and what is the daily limit?", "2 tablets (1,000 mg) every 8 hours as needed; no more than 3,000 mg in 24 hours"),
        ],
    },
    {
        "sample": "03_hip_replacement_dorothy_thompson.pdf", "recording": "03_hip_replacement_dorothy_thompson__English",
        "persona": "You are Tom, 76, Dorothy's husband. You speak English but have no medical background, you are tired, "
                   "and medical shorthand (PO, BID, q8h, PRN, SL) means nothing to you.",
        "questions": [
            ("How often does Dorothy take the blood thinner, and for how long?", "Apixaban 2.5 mg, 1 tablet twice daily for 35 days (last dose 11/20/2026)"),
            ("How often does she take acetaminophen, and is it only when in pain?", "2 tablets every 8 hours on a schedule (not only when in pain); max 3,000 mg per day"),
            ("What is the most oxycodone she can take in a day?", "4 tablets per day"),
            ("Which medicine must she not take right now?", "Aspirin 81 mg (hold while on apixaban unless the surgeon says to restart)"),
            ("Name two signs that mean call 911.", "Any two of: chest pain or sudden shortness of breath; swollen, red or painful calf; black or bloody stools or vomiting blood; hip feels popped out or cannot bear weight"),
            ("What should you do about her other home medicines?", "The papers do not say which ones; ask the doctor (they say only 'resume your other home medications as before')"),
        ],
    },
]

ANSWER_SCHEMA = {"type": "object", "properties": {"answers": {"type": "array", "items": {"type": "string"}}}, "required": ["answers"]}
GRADE_SCHEMA = {"type": "object", "properties": {"grades": {"type": "array", "items": {
    "type": "object", "properties": {"score": {"type": "number", "enum": [0, 0.5, 1]}, "why": {"type": "string"}}, "required": ["score", "why"]}}},
    "required": ["grades"]}


def plan_as_text(rec: dict) -> str:
    """What the family sees in the app, flattened to text (in the plan's language)."""
    r = rec["result"]
    ex, exp, sched = r["extraction"], r["explanation"], r["schedule"]
    meds = {m["id"]: m for m in ex["medications"]}
    L = exp["labels"]
    out = [f"{L.get('summary', 'Why you were in the hospital')}: {exp.get('summary') or ''}", ""]
    day = next((d for d in sched["days"] if d["slots"]), sched["days"][0])
    out.append(f"{L.get('timetable', 'Medicine timetable')} ({day['date']}):")
    for slot in day["slots"]:
        items = "; ".join(f"{meds[i['med_id']]['name']} {meds[i['med_id']].get('strength') or ''}: {i['dose']}" for i in slot["items"])
        out.append(f"  {slot['time']} ({L.get(slot['label'], slot['label'])}): {items}")
    if sched["stopped"]:
        out.append(f"{L.get('stop', 'Stop taking these')}: " + ", ".join(meds[i]["name"] for i in sched["stopped"]))
    if sched["as_needed"]:
        out.append(f"{L.get('as_needed', 'Only if needed')}: " + "; ".join(
            f"{meds[i]['name']} {meds[i].get('dose') or ''} ({meds[i].get('max_per_day') or ''})" for i in sched["as_needed"]))
    out.append("")
    out.append(f"{L.get('medicines', 'Medicines')}:")
    for mid, card in exp["medications"].items():
        m = meds[mid]
        out.append(f"- {m['name']} {m.get('strength') or ''} [{L.get(m['status'] if m['status'] != 'stop' else 'stop_short', m['status'])}]: "
                   f"{card.get('what_for') or ''} {card['how_to_take']} {' '.join(card.get('tips') or [])}")
    out.append("")
    out.append(f"{L.get('warnings', 'Warning signs')}:")
    for w in ex["warnings"]:
        e = exp["warnings"].get(w["id"], w)
        head = L.get("emergency" if w["level"] == "emergency" else "call", w["level"])
        out.append(f"- {head} ({w.get('phone') or '911'}): " + "; ".join(e["signs"]))
    out.append("")
    out.append(f"{L.get('appointments', 'Appointments')}:")
    for a in ex["appointments"]:
        out.append(f"- {a['with_whom']} {a.get('date') or ''} {a.get('time') or ''}: {exp['appointments'].get(a['id'], {}).get('text', '')}")
    out.append(f"{L.get('instructions', 'Diet and activity')}:")
    for i in ex["instructions"]:
        out.append(f"- {exp['instructions'].get(i['id'], {}).get('text', i['text'])}")
    if exp["questions"]:
        out.append(f"{L.get('questions', 'Questions for the doctor')}: " + " | ".join(q["question"] for q in exp["questions"]))
    return "\n".join(out)


async def ask(persona: str, material: str, questions: list[str]) -> tuple[list[str], llm.LLMResult]:
    system = (persona + "\n\nAnswer each question using ONLY the material you are given, the way this person would understand it. "
              "If the material is hard for you to understand, or you are not sure, say so instead of guessing. "
              "Answer in English, briefly, one answer per question, in order.")
    user = "<material>\n" + material + "\n</material>\n\nQuestions:\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    r = await llm.call_structured("persona", system, user, ANSWER_SCHEMA)
    return r.data["answers"], r


async def grade(questions, gold, answers) -> list[dict]:
    system = ("You grade a caregiver's answers against an answer key. Score 1 if the answer is correct and complete enough to act on safely, "
              "0.5 if partly right, 0 if wrong, missing, or 'I don't know'. Be strict about doses, dates and medicines to stop.")
    rows = "\n".join(f"Q{i + 1}: {q}\nKEY: {g}\nANSWER: {a}" for i, (q, g, a) in enumerate(zip(questions, gold, answers)))
    r = await llm.call_structured("grader", system, rows, GRADE_SCHEMA)
    return r.data["grades"]


async def main() -> None:
    results = []
    for case in CASES:
        rec = json.loads((REC / f"{case['recording']}.json").read_text())
        doc = intake.load(case["sample"], (config.SAMPLES_DIR / case["sample"]).read_bytes()).text
        qs = [q for q, _ in case["questions"]]
        gold = [g for _, g in case["questions"]]
        for condition, material in (("original papers", doc), ("Going Home plan", plan_as_text(rec))):
            answers, _ = await ask(case["persona"], material, qs)
            grades = await grade(qs, gold, answers)
            score = sum(g["score"] for g in grades)
            results.append({"case": case["sample"], "persona": case["persona"].split(",")[0].replace("You are ", ""),
                            "plan_language": rec["language"], "condition": condition, "score": score, "max": len(qs),
                            "questions": qs, "answers": answers, "grades": grades})
            print(f"{case['sample'][:28]:30} {condition:16} {score}/{len(qs)}")
    out = ROOT / "docs" / "results" / "simulated_readers.json"
    out.write_text(json.dumps({"note": "AI personas (claude haiku) graded by claude sonnet against a hand-written key. Not real users.",
                               "results": results}, indent=1, ensure_ascii=False))
    print("saved", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main())
