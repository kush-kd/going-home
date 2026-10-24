"""Run one sample through the real pipeline and save the full result.

    .venv/bin/python scripts/run_one.py 05_SAFETY_TEST_prompt_injection.pdf [language]
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, pipeline  # noqa: E402


async def main(name: str, language: str) -> None:
    run = pipeline.Run(language, f"script:{name}")
    await pipeline.execute(run, name, (config.SAMPLES_DIR / name).read_bytes())
    out = config.LOG_DIR / f"result_{Path(name).stem}_{language.split()[0].lower()}.json"
    out.write_text(json.dumps(run.public(), indent=1, ensure_ascii=False))
    print(run.status, run.error or "", "->", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "English"))
