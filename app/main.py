"""FastAPI server: upload or pick a sample, watch the agents work, get the plan."""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, llm, pipeline

app = FastAPI(title="Going Home")

# The five most common languages among US residents with limited English come first (Spanish, Chinese,
# Vietnamese, Korean, Tagalog: over 75% of that population, Migration Policy Institute 2025).
LANGUAGES = ["English", "Español (Spanish)", "中文 (Chinese, Simplified)", "Tiếng Việt (Vietnamese)", "한국어 (Korean)",
             "Tagalog (Filipino)", "हिन्दी (Hindi)", "العربية (Arabic)", "Kreyòl ayisyen (Haitian Creole)",
             "Português (Portuguese)", "Français (French)"]

_tasks: set[asyncio.Task] = set()

# auto: live when Claude Code is logged in, otherwise demo mode (replays recorded real runs).
MODE = os.getenv("GOING_HOME_MODE", "auto").lower()
_status_cache: tuple[float, dict] = (0.0, {})


def _llm_status() -> dict:
    global _status_cache
    if time.monotonic() - _status_cache[0] > 20:
        _status_cache = (time.monotonic(), llm.status())
    return _status_cache[1]


def demo_mode() -> bool:
    return MODE == "demo" or (MODE == "auto" and not _llm_status().get("ready"))


SAMPLE_INFO = {
    "01": ("Heart failure", "Ramesh Mehta, 68: new water pill, a lowered dose, two medicines to stop"),
    "02": ("Pneumonia and diabetes", "Maria Garcia, 57: antibiotic course, new insulin; reads Spanish"),
    "03": ("Hip replacement", "Dorothy Thompson, 74: blood thinner for 35 days, pain plan, unclear orders"),
    "04": ("COPD with steroid taper", "Joseph Kowalski, 71: prednisone dose drops every 2 days, inhalers"),
    "05": ("Stroke warning and new blood thinner", "Chen Guoliang, 79: reads Chinese; 2 medicines to stop, 2 changed doses"),
    "06": ("Safety test: hidden attack", "A discharge with hidden and visible prompt-injection text"),
}


def _samples() -> list[dict]:
    out = []
    for p in sorted(config.SAMPLES_DIR.glob("*.pdf")):
        title, sub = SAMPLE_INFO.get(p.name[:2], (p.stem, "Synthetic discharge papers"))
        recorded = sorted({f.stem.split("__", 1)[1] for f in pipeline.RECORDED_DIR.glob(f"{p.stem}__*.json")})
        out.append({"file": p.name, "title": title, "subtitle": sub, "safety_test": "SAFETY" in p.stem, "recorded": recorded})
    return out


def _sample_path(name: str):
    path = (config.SAMPLES_DIR / name).resolve()
    if path.parent != config.SAMPLES_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Unknown sample")
    return path


@app.get("/api/status")
def status() -> dict:
    return {"llm": _llm_status(), "demo": demo_mode(), "demo_reason": "forced" if MODE == "demo" else "no_login",
            "languages": LANGUAGES, "samples": _samples(),
            "limits": {"max_upload_mb": config.MAX_UPLOAD_MB, "max_pages": config.MAX_PAGES, "runs_per_day": config.MAX_RUNS_PER_DAY}}


def _start(language: str, source: str, filename: str, data: bytes) -> dict:
    if language not in LANGUAGES:
        language = "English"
    run = pipeline.Run(language, source)
    pipeline.RUNS[run.id] = run
    task = asyncio.create_task(pipeline.execute(run, filename, data))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run.id}


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...), language: str = Form("English")) -> dict:
    if demo_mode():
        raise HTTPException(400, "Demo mode: making a plan from new papers needs Claude Code logged in on this computer "
                                 "(run `claude`, then /login). The sample buttons replay real recorded runs.")
    data = await file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    return _start(language, "upload", file.filename or "upload", data)


@app.post("/api/runs/sample")
async def create_sample_run(name: str = Form(...), language: str = Form("English")) -> dict:
    path = _sample_path(name)
    if demo_mode():
        rec, _lang = pipeline.find_recording(name, language)
        if rec is None:
            raise HTTPException(400, "No recording of this sample is available in demo mode.")
        run = pipeline.Run(language if language in LANGUAGES else "English", f"replay:{name}")
        pipeline.RUNS[run.id] = run
        task = asyncio.create_task(pipeline.replay(run, rec))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return {"run_id": run.id, "replay": True}
    return _start(language, f"sample:{name}", name, path.read_bytes())


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = pipeline.RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    data = run.public()
    if data["result"]:
        data["result"] = {k: v for k, v in data["result"].items() if k != "ics"}
    return data


@app.get("/api/runs/{run_id}/calendar.ics")
def calendar(run_id: str) -> Response:
    run = pipeline.RUNS.get(run_id)
    if not run or not run.result:
        raise HTTPException(404, "No plan yet")
    return Response(run.result["ics"], media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="going-home-medicines.ics"'})


@app.get("/samples/{name}")
def sample_pdf(name: str) -> FileResponse:
    path = _sample_path(name)
    return FileResponse(path, media_type="application/pdf")


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
