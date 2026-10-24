"""Measure accuracy, tokens and cost of the full pipeline on the four synthetic samples.

Used for the report's tokenomics section: run it once per model setup and compare.

    .venv/bin/python scripts/eval_models.py                         # default models
    READER_MODEL=sonnet .venv/bin/python scripts/eval_models.py      # cheaper Reader
    .venv/bin/python scripts/eval_models.py --runs 2 --label opus-reader

Writes logs/eval_<label>.json and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, grounding, pipeline  # noqa: E402

GOLD = json.loads((ROOT / "data" / "eval" / "gold.json").read_text())


def _num_in(expected: str, value: str | None) -> bool:
    return expected in grounding.numbers(value or "")


def score(sample: str, result: dict) -> dict:
    gold = GOLD[sample]
    meds = result["extraction"]["medications"]
    found, fields_ok, fields_total, missed = 0, 0, 0, []
    used = set()
    counted = [g for g in gold["medications"] if not g.get("optional")]
    for g in gold["medications"]:
        hit = next((m for m in meds if g["match"] in m["name"].lower() and m["id"] not in used), None)
        if hit is None:
            if not g.get("optional"):
                missed.append(g["match"])
            continue
        used.add(hit["id"])
        if g.get("optional"):
            continue
        found += 1
        for key in ("status", "schedule_kind", "times_per_day", "duration_days", "end_date"):
            if key in g:
                fields_total += 1
                fields_ok += hit.get(key) == g[key]
        if "strength" in g:
            fields_total += 1
            fields_ok += _num_in(g["strength"], hit.get("strength"))
        if "dose" in g:
            fields_total += 1
            fields_ok += _num_in(g["dose"], hit.get("dose"))
        if "taper_steps" in g:
            fields_total += 1
            fields_ok += len(hit.get("taper", [])) == g["taper_steps"]
    optional = {g["match"] for g in gold["medications"] if g.get("optional")}
    extra = [m["name"] for m in meds if m["id"] not in used and not any(o in m["name"].lower() for o in optional)]
    ver = result["verification"]
    return {
        "med_recall": round(found / len(counted), 3),
        "field_accuracy": round(fields_ok / fields_total, 3) if fields_total else None,
        "missed": missed,
        "extra": extra,
        "warnings_ok": len(result["extraction"]["warnings"]) == gold["warnings"],
        "appointments_ok": len(result["extraction"]["appointments"]) == gold["appointments"],
        "verified_rate": round(sum(v["verified"] for v in ver.values()) / max(1, len(ver)), 3),
        "repaired": result["checker"]["repaired"],
        "grounding_flags": result["checker"].get("initial_grounding_problems", {}),
        "checker_issues": [f"[{i['severity']}] {i['problem']}" for i in result["checker"]["initial_issues"]],
        "checker_remaining": [f"[{i['severity']}] {i['problem']}" for i in result["checker"]["remaining_issues"]],
        "explainer_rejections": len(result["explainer_flags"]),
    }


async def run_one(sample: str, language: str) -> dict:
    run = pipeline.Run(language, f"eval:{sample}")
    await pipeline.execute(run, sample, (config.SAMPLES_DIR / sample).read_bytes())
    if run.status != "done":
        return {"sample": sample, "error": run.error}
    m = run.result["metrics"]
    return {"sample": sample, **score(sample, run.result), "tokens": m["totals"], "total_tokens": m["total_tokens"],
            "cost_usd": m["cost_usd"], "elapsed_s": m["elapsed_s"],
            "models": sorted({c["model"] for c in m["calls"]})}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="_".join(f"{k}-{v}" for k, v in config.CLAUDE_MODELS.items() if k != "transcriber"))
    ap.add_argument("--language", default="English")
    args = ap.parse_args()
    config.MAX_RUNS_PER_DAY = 10_000
    rows = []
    for _ in range(args.runs):
        for sample in [k for k in GOLD if not k.startswith("_")]:
            r = await run_one(sample, args.language)
            print(json.dumps(r))
            rows.append(r)
    ok = [r for r in rows if "error" not in r]
    summary = {
        "label": args.label, "models": config.CLAUDE_MODELS, "runs": len(rows), "errors": len(rows) - len(ok),
        "med_recall": round(sum(r["med_recall"] for r in ok) / max(1, len(ok)), 3),
        "field_accuracy": round(sum(r["field_accuracy"] for r in ok) / max(1, len(ok)), 3),
        "verified_rate": round(sum(r["verified_rate"] for r in ok) / max(1, len(ok)), 3),
        "avg_tokens": round(sum(r["total_tokens"] for r in ok) / max(1, len(ok))),
        "avg_cost_usd": round(sum(r["cost_usd"] or 0 for r in ok) / max(1, len(ok)), 4),
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in ok) / max(1, len(ok)), 1),
    }
    config.LOG_DIR.mkdir(exist_ok=True)
    out = config.LOG_DIR / f"eval_{args.label}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print("\nSUMMARY", json.dumps(summary, indent=1))
    print("saved", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main())
