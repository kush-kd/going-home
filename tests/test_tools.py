"""The Checker's MCP tools are plain code: test them directly."""

import importlib
import json

from app import intake
from fake_llm import gold_01

HF = "01_heart_failure_ramesh_mehta.pdf"


def load_tools(tmp_path, monkeypatch, sample_bytes, plan=None):
    doc = intake.load(HF, sample_bytes(HF))
    (tmp_path / "document.txt").write_text(doc.text)
    (tmp_path / "plan.json").write_text(json.dumps(plan or gold_01()))
    monkeypatch.setenv("GH_RUN_DIR", str(tmp_path))
    import app.mcp_tools as tools
    return importlib.reload(tools)


def test_list_medication_lines_finds_every_medicine(tmp_path, monkeypatch, sample_bytes):
    tools = load_tools(tmp_path, monkeypatch, sample_bytes)
    text = " ".join(x["text"] for x in tools.list_medication_lines())
    for name in ("Furosemide", "Spironolactone", "Metoprolol", "Lisinopril", "Atorvastatin", "Amlodipine"):
        assert name in text


def test_search_and_check_quote(tmp_path, monkeypatch, sample_bytes):
    tools = load_tools(tmp_path, monkeypatch, sample_bytes)
    hits = tools.search_document("nurse line")
    assert hits and "(203) 555-0187" in hits[0]["text"]
    assert tools.check_quote("Take 1 tablet by mouth at bedtime.")["found"] is True
    assert tools.check_quote("Take 4 tablets of furosemide hourly.")["found"] is False


def test_verify_item_reports_a_wrong_dose(tmp_path, monkeypatch, sample_bytes):
    bad = gold_01()
    bad["medications"][0]["strength"] = "80 mg"
    tools = load_tools(tmp_path, monkeypatch, sample_bytes, plan=bad)
    assert tools.verify_item("m1")["verified"] is False
    assert tools.verify_item("m2")["verified"] is True
    log = (tmp_path / "tool_calls.jsonl").read_text().splitlines()
    assert [json.loads(x)["tool"] for x in log] == ["verify_item", "verify_item"]
