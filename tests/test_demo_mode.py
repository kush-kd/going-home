"""Demo mode: with no Claude login, sample buttons replay recorded real runs, clearly labeled."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from app import agents, main, pipeline
from fake_llm import FakeLLM

HF = "01_heart_failure_ramesh_mehta.pdf"


def make_recording(tmp_path, monkeypatch, sample_bytes):
    monkeypatch.setattr(agents, "call_structured", FakeLLM())
    run = pipeline.Run("English", "test")
    asyncio.run(pipeline.execute(run, HF, sample_bytes(HF)))
    rec = run.public() | {"ics": run.result["ics"], "recorded_at": "2026-10-24T12:00"}
    monkeypatch.setattr(pipeline, "RECORDED_DIR", tmp_path)
    (tmp_path / f"{pipeline.recording_key(HF, 'English')}.json").write_text(json.dumps(rec))


def test_demo_mode_replays_and_labels_a_recorded_run(tmp_path, monkeypatch, sample_bytes):
    make_recording(tmp_path, monkeypatch, sample_bytes)
    monkeypatch.setattr(main, "MODE", "demo")
    monkeypatch.setattr(pipeline, "REPLAY_SPEED", 0)
    with TestClient(main.app) as client:
        status = client.get("/api/status").json()
        assert status["demo"] is True
        assert next(s for s in status["samples"] if s["file"] == HF)["recorded"] == ["English"]
        run_id = client.post("/api/runs/sample", data={"name": HF, "language": "中文 (Chinese, Simplified)"}).json()["run_id"]
        for _ in range(50):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "done"
        replay = run["result"]["replay"]
        assert replay["language"] == "English" and replay["requested_language"].startswith("中文")
        assert client.get(f"/api/runs/{run_id}/calendar.ics").text.startswith("BEGIN:VCALENDAR")
        upload = client.post("/api/runs", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}, data={"language": "English"})
        assert upload.status_code == 400 and "Demo mode" in upload.json()["detail"]
