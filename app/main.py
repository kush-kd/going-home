"""FastAPI server: upload or pick a sample, watch the agents work, get the plan."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, llm, pipeline

app = FastAPI(title="Going Home")

LANGUAGES = ["English", "Español (Spanish)", "हिन्दी (Hindi)", "中文 (Chinese, Simplified)", "Tiếng Việt (Vietnamese)",
             "Português (Portuguese)", "Français (French)", "Kreyòl ayisyen (Haitian Creole)", "العربية (Arabic)"]

_tasks: set[asyncio.Task] = set()


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
        out.append({"file": p.name, "title": title, "subtitle": sub, "safety_test": "SAFETY" in p.stem})
    return out


def _sample_path(name: str):
    path = (config.SAMPLES_DIR / name).resolve()
    if path.parent != config.SAMPLES_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Unknown sample")
    return path


@app.get("/api/status")
def status() -> dict:
    return {"llm": llm.status(), "languages": LANGUAGES, "samples": _samples(),
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
    data = await file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    return _start(language, "upload", file.filename or "upload", data)


@app.post("/api/runs/sample")
async def create_sample_run(name: str = Form(...), language: str = Form("English")) -> dict:
    path = _sample_path(name)
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
