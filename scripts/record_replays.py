"""Record real runs of the sample papers for demo mode (used when no Claude login is available).

    .venv/bin/python scripts/record_replays.py 01_heart_failure_ramesh_mehta.pdf English

Each recording is the complete result of a real run (agents, models, tokens, cost, timings)
saved to data/recorded/. Demo mode replays it and labels it clearly as a recording.
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, pipeline  # noqa: E402

OUT = ROOT / "data" / "recorded"


async def main(name: str, language: str) -> None:
    config.MAX_RUNS_PER_DAY = 10_000
    run = pipeline.Run(language, f"recording:{name}")
    await pipeline.execute(run, name, (config.SAMPLES_DIR / name).read_bytes())
    if run.status != "done":
        raise SystemExit(f"{name} {language}: {run.error}")
    data = run.public()
    data["recorded_at"] = datetime.now().isoformat(timespec="minutes")
    data["ics"] = run.result["ics"]
    OUT.mkdir(parents=True, exist_ok=True)
    key = pipeline.recording_key(name, language)
    (OUT / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False))
    m = run.result["metrics"]
    print(f"recorded {key}: {m['total_tokens']:,} tokens, ${m['cost_usd']:.3f}, {m['elapsed_s']}s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
