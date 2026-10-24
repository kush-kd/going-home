"""Record a real demo of Going Home (real Claude agents, no test stand-in).

Starts the app on port 8020, drives Chrome with Playwright, records the browser,
and writes the raw video plus a timeline of the waiting parts (to speed up later).

    .venv-rec/bin/python record_demo.py
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parents[1] / "docs"
APP = Path(__file__).resolve().parents[1]
OUT = HERE / "video_raw"
SHOTS = HERE / "shots"
PORT = 8020
URL = f"http://127.0.0.1:{PORT}"
W, H = 1280, 800

# Captions and a visible pointer, added to every page load. Annotations only: the app is not changed.
OVERLAY_JS = r"""
(() => {
  if (window.__ghOverlay) return; window.__ghOverlay = true;
  const add = () => {
    const cap = document.createElement('div');
    cap.id = '__cap';
    cap.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);max-width:1060px;z-index:99999;'
      + 'background:rgba(16,42,67,.93);color:#fff;font:600 19px/1.4 -apple-system,Segoe UI,sans-serif;padding:12px 20px;'
      + 'border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.25);text-align:center;transition:opacity .25s;opacity:0';
    document.body.appendChild(cap);
    const dot = document.createElement('div');
    dot.id = '__dot';
    dot.style.cssText = 'position:fixed;width:22px;height:22px;border-radius:50%;background:rgba(245,158,11,.55);'
      + 'border:2px solid #b45309;z-index:100000;pointer-events:none;transform:translate(-50%,-50%);left:-40px;top:-40px;transition:left .05s,top .05s';
    document.body.appendChild(dot);
    document.addEventListener('mousemove', e => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
    document.addEventListener('mousedown', () => { dot.style.background = 'rgba(245,158,11,.95)'; setTimeout(() => dot.style.background = 'rgba(245,158,11,.55)', 250); }, true);
  };
  if (document.body) add(); else document.addEventListener('DOMContentLoaded', add);
  window.__caption = (html) => { const c = document.getElementById('__cap'); if (!c) return; if (!html) { c.style.opacity = 0; return; } c.innerHTML = html; c.style.opacity = 1; };
  window.__modal = (src, title) => {
    const m = document.createElement('div'); m.id = '__modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(16,42,67,.6);z-index:99998;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px';
    m.innerHTML = '<div style="color:#fff;font:700 18px -apple-system,sans-serif">' + title + '</div><img src="' + src + '" style="max-height:640px;border-radius:6px;box-shadow:0 10px 40px rgba(0,0,0,.4)">';
    document.body.appendChild(m);
  };
  window.__unmodal = () => { const m = document.getElementById('__modal'); if (m) m.remove(); };
  window.__card = (html) => {
    const c = document.createElement('div'); c.id = '__card';
    c.style.cssText = 'position:fixed;inset:0;z-index:99999;background:linear-gradient(135deg,#0b5952,#102a43);color:#fff;display:flex;flex-direction:column;justify-content:center;padding:70px 110px;font:400 25px/1.45 -apple-system,Segoe UI,sans-serif;opacity:0;transition:opacity .35s';
    c.innerHTML = html; document.body.appendChild(c); requestAnimationFrame(() => c.style.opacity = 1);
  };
  window.__uncard = () => { const c = document.getElementById('__card'); if (c) { c.style.opacity = 0; setTimeout(() => c.remove(), 350); } };
})();
"""


VOICE = "am_michael"
VOICE_DIR = HERE / "voice" / "lines"
NARRATION = {
    "s1": "When someone leaves the hospital, the family gets papers full of medical shorthand. I built Going Home to turn them into a plan a family can actually follow.",
    "s2": "It only restates what the hospital wrote. It never diagnoses, and never changes a dose.",
    "s3": "Mister Chen, a synthetic patient, is seventy-nine and just had a mini stroke. His papers say things like P O, B I D, and Q H S.",
    "s4": "Why his daughter's language matters: for patients with limited English, hospital errors cause physical harm about half the time, versus thirty percent for English speakers. And family members who translate can drop a dose.",
    "s5": "So we choose Chinese.",
    "s6": "Four Claude agents read the papers, check every quote and number in code, look for anything missed, and write it in plain Chinese.",
    "s7": "Every medicine was found in the papers, and every number matches exactly.",
    "s8": "The timetable turns shorthand like B I D and Q H S into clock times for each day, marks when each course ends, and shows in red what to stop. Most adults can't organize this on their own.",
    "s9": "Times are suggestions, and on day one it reminds the family to ask which doses were already given.",
    "s10": "Aspirin and ginkgo are flagged to stop, because both raise bleeding risk with his new blood thinner.",
    "s11": "Each medicine is in plain Chinese, with the hospital's English sentence one tap away.",
    "s12": "Stroke signs mean call nine one one. Bleeding signs mean call the clinic.",
    "s13": "Follow-up visits, phone reminders, and a chart for the fridge.",
    "s14": "This tab shows each agent, its model, its tokens and its cost. About twenty cents for this plan.",
    "s15": "And every tool the Checker called.",
    "s16": "Now a safety test, with attacks hidden in the file.",
    "s18": "Hidden text told the A I to give ten tablets of everything.",
    "s19": "Plain code removed it, and a second attack, before any agent read the file.",
    "s20": "The plan underneath is the real one.",
    "s21": "About nine cents.",
    "s22": "Could this save lives? Two thirds of drug-harm hospitalizations in older adults involve blood thinners, antiplatelets, insulin, or diabetes pills. A hospital program that gave patients this kind of plan cut hospital returns by about thirty percent. Going Home builds that plan automatically.",
    "s23": "Going Home doesn't replace the care team. It makes sure the family leaves with the plan they were given.",
}


def generate_voice() -> dict[str, float]:
    """Make one WAV per narration line with Kokoro (Apache-2.0); return durations in seconds."""
    import hashlib
    import soundfile as sf
    from kokoro_onnx import Kokoro
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    kokoro = None
    durs = {}
    for key, text in NARRATION.items():
        f = VOICE_DIR / f"{key}_{hashlib.md5((VOICE + "1.1" + text).encode()).hexdigest()[:8]}.wav"
        if not f.exists():
            kokoro = kokoro or Kokoro(str(HERE / "tts_models/kokoro-v1.0.onnx"), str(HERE / "tts_models/voices-v1.0.bin"))
            samples, sr = kokoro.create(text, voice=VOICE, speed=1.1, lang="en-us")
            sf.write(f, samples, sr)
        info = sf.info(f)
        durs[key] = info.frames / info.samplerate
        (VOICE_DIR / f"{key}.path").write_text(str(f))
    return durs


class Demo:
    def __init__(self, page):
        self.page = page
        self.waits: list[dict] = []
        self.events: list[dict] = []
        self.voice: dict[str, float] = {}
        self.voice_free_at = 0.0

    def t(self) -> float:
        return time.time()          # same clock as Chrome's screencast frame timestamps

    def sleep(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)   # keeps screencast frames flowing

    def say(self, key: str) -> None:
        """Start a narration line once the previous one has finished; scenes then wait for it."""
        now = self.t()
        if now < self.voice_free_at:
            self.sleep(self.voice_free_at - now)
        start = self.t()
        self.events.append({"key": key, "ts": start, "dur": self.voice.get(key, 0)})
        self.voice_free_at = start + self.voice.get(key, 0) + 0.3

    def remaining_voice(self) -> float:
        return max(0.0, self.voice_free_at - self.t())

    def cap(self, html: str | None, hold: float = 0, say: str | None = None, wait_voice: bool = True) -> None:
        if say:
            now = self.t()
            if now < self.voice_free_at:          # let the previous line finish before the caption changes
                self.sleep(self.voice_free_at - now)
        self.page.evaluate("h => window.__caption(h)", html)
        if say:
            self.say(say)
            if wait_voice:
                hold = max(hold, self.remaining_voice())
        if hold:
            self.sleep(hold)

    def move_to(self, selector: str, steps: int = 10) -> None:
        el = self.page.locator(selector).first
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
        self.sleep(0.2)

    def click(self, selector: str) -> None:
        self.move_to(selector)
        self.page.locator(selector).first.click()
        self.sleep(0.3)

    def scroll(self, dy: int, duration: float = 1.2) -> None:
        steps = 20
        for _ in range(steps):
            self.page.mouse.wheel(0, dy / steps)
            self.sleep(duration / steps)

    def tab(self, name: str) -> None:
        self.click(f'#tabs [data-tab="{name}"]')
        self.page.evaluate("window.scrollTo({top: document.querySelector('.tabs').offsetTop - 70, behavior: 'smooth'})")
        self.sleep(0.45)

    def card(self, title: str, lines: list[str], source: str, hold: float, say: str | None = None) -> None:
        html_ = (f'<div style="font-size:15px;letter-spacing:.12em;text-transform:uppercase;color:#99f6e4;font-weight:700">Why it matters</div>'
                 f'<div style="font-size:40px;font-weight:800;margin:6px 0 22px">{title}</div>'
                 + "".join(f'<div style="margin-bottom:14px">{ln}</div>' for ln in lines)
                 + f'<div style="font-size:14px;color:#c7d2fe;margin-top:18px">{source}</div>')
        if say and self.t() < self.voice_free_at:
            self.sleep(self.voice_free_at - self.t())
        self.page.evaluate("h => { window.__caption(null); window.__card(h); }", html_)
        if say:
            self.say(say)
            hold = max(hold, self.remaining_voice())
        self.sleep(hold)
        self.page.evaluate("window.__uncard()")
        self.sleep(0.4)

    def shot(self, name: str) -> None:
        """Save a clean screenshot (no caption or pointer) for the report and slides."""
        SHOTS.mkdir(exist_ok=True)
        self.page.evaluate("document.getElementById('__cap').style.visibility='hidden'; document.getElementById('__dot').style.visibility='hidden'")
        self.page.screenshot(path=str(SHOTS / f"{name}.png"))
        self.page.evaluate("document.getElementById('__cap').style.visibility='visible'; document.getElementById('__dot').style.visibility='visible'")

    def wait_for_plan(self, label: str, progress_shot: str | None = None) -> dict:
        start = self.t()
        self.page.evaluate("""label => {
            const t0 = Date.now();
            window.__waitTimer = setInterval(() => {
              const s = Math.round((Date.now() - t0) / 1000);
              window.__caption('⏩ <b>Sped up</b> · the four Claude agents are working on the ' + label + ' · real time ' + Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'));
            }, 500);
        }""", label)
        if progress_shot:
            self.sleep(22)
            self.shot(progress_shot)
        self.page.wait_for_function("!document.querySelector('#result').hidden || !document.querySelector('#run-error').hidden", timeout=420_000, polling=500)
        self.page.evaluate("clearInterval(window.__waitTimer)")
        end = self.t()
        self.waits.append({"start": start, "end": end, "label": label})
        error = self.page.locator("#run-error").inner_text() if self.page.locator("#run-error").is_visible() else ""
        if error:
            raise RuntimeError(f"Run failed on camera: {error}")
        return self.page.evaluate("state.run.result.metrics")


def pdf_page_png(pdf: Path) -> str:
    sys.path.insert(0, str(APP))
    import pdfplumber  # from the app's venv site-packages (added below)
    with pdfplumber.open(pdf) as doc:
        img = doc.pages[0].to_image(resolution=90).original
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.webm"):
        f.unlink()
    import os
    env = os.environ | {"MAX_RUNS_PER_DAY": "1000", "GOING_HOME_MODE": "live"}   # the app's daily cap is for real users
    server = subprocess.Popen([str(APP / ".venv/bin/uvicorn"), "app.main:app", "--app-dir", str(APP), "--port", str(PORT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    try:
        time.sleep(2.5)
        # pdfplumber lives in the app's venv; borrow it just to render the sample page image.
        site = next((APP / ".venv/lib").glob("python*/site-packages"))
        sys.path.insert(0, str(site))
        voice_durs = generate_voice()
        print("narration:", round(sum(voice_durs.values()), 1), "seconds of voice")
        hf_png = pdf_page_png(APP / "data/samples/05_stroke_afib_chen_guoliang.pdf")
        SHOTS.mkdir(exist_ok=True)
        (SHOTS / "papers.png").write_bytes(base64.b64decode(hf_png.split(",", 1)[1]))

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
            ctx.add_init_script(OVERLAY_JS)
            page = ctx.new_page()
            d = Demo(page)
            d.voice = voice_durs
            frames_dir = OUT / "frames"
            frames_dir.mkdir(exist_ok=True)
            for f in frames_dir.glob("*.jpg"):
                f.unlink()
            frames: list[dict] = []
            cdp = ctx.new_cdp_session(page)

            def on_frame(e):
                path = frames_dir / f"{len(frames):06d}.jpg"
                path.write_bytes(base64.b64decode(e["data"]))
                frames.append({"file": path.name, "ts": e["metadata"]["timestamp"]})
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]})
                except Exception:  # noqa: BLE001 - the page closed after the last frame
                    pass

            cdp.on("Page.screencastFrame", on_frame)
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})
            page.goto(URL)
            page.wait_for_selector("[data-sample]")
            d.sleep(0.8)
            d.shot("start")

            # 1. The problem
            d.cap("When someone leaves the hospital, the family goes home with a stack of discharge papers.", 3, say="s1", wait_voice=False)
            d.cap("New medicines, changed doses, medicines to stop, visits and warning signs, in medical shorthand.", 3)
            d.cap("<b>Going Home</b> turns those papers into a plan a family can actually follow.", 2)
            page.evaluate("document.querySelector('.scope').scrollIntoView({block: 'center', behavior: 'smooth'})")
            d.sleep(1)
            d.move_to(".scope-col.doesnt")
            d.cap("It only restates the hospital's plan. It never diagnoses, changes a dose, adds a medicine or gives medical advice.", 2, say="s2")
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            d.sleep(0.8)

            # 2. The papers
            page.evaluate("([s, t]) => window.__modal(s, t)", [hf_png, "Synthetic discharge summary: stroke warning and new blood thinner (fictional patient)"])
            d.cap("Mr. Chen, 79, had a mini-stroke. His papers use shorthand like PO, BID and QHS.", 2, say="s3")
            page.evaluate("window.__unmodal()")
            d.card("Why her own language?", [
                "In US hospitals, <b>49%</b> of adverse events among patients with limited English caused physical harm, vs <b>30%</b> for English speakers.",
                "Chinese-speaking patients had <b>1.7×</b> the odds of being readmitted within 30 days.",
                "At discharge, family members often interpret. In one study, <b>77%</b> of errors by untrained interpreters, such as family, could have clinical consequences, like a dropped dose or duration.",
            ], "Divi 2007, Int J Qual Health Care · Karliner 2010, J Hosp Med · Flores 2003, Pediatrics", 3, say="s4")

            # 3. Pick Chinese and start
            d.cap("His daughter reads simplified Chinese, so we choose Chinese…", 0, say="s5", wait_voice=False)
            d.move_to("#language")
            page.select_option("#language", label="中文 (Chinese, Simplified)")
            d.sleep(1.5)
            d.cap("…and start with his discharge papers.", 0)
            d.click('[data-sample="05_stroke_afib_chen_guoliang.pdf"]')
            d.cap("Reader agent extracts every medicine with its exact source words. Code checks every quote and number. "
                  "A Checker agent looks for anything missed. An Explainer writes it in plain Chinese.", 3, say="s6", wait_voice=False)
            zh = d.wait_for_plan("discharge papers in Chinese", progress_shot="progress")

            # 4. The plan
            page.evaluate("window.scrollTo(0, 0)")
            d.sleep(0.5)
            d.shot("zh_top")
            d.cap("✅ Every medicine was found in the papers, and every dose, date and phone number matches them exactly.", 1.5, say="s7")
            d.card("What the timetable does", [
                "The papers say <i>“1 tab PO BID”</i>, <i>“QHS”</i>, <i>“1–2 tabs q6h PRN”</i>. Families have to work out what that means for tonight.",
                "Going Home turns it into clock times for each day, marks the last day of each course, keeps “only if needed” medicines off the clock, and shows in red what to <b>stop</b>.",
                "Without help, only <b>15%</b> of adults could organize a 7-medicine regimen into 4 daily times. Patients didn’t understand <b>82%</b> of their stopped medicines.",
            ], "Wolf 2011, Arch Intern Med · Ziaeian 2012, J Gen Intern Med", 3, say="s8")
            d.tab("timetable")
            d.click(".days .day")      # discharge day
            d.sleep(0.5)
            d.shot("zh_timetable")
            d.move_to(".tt-note")
            d.cap("Clock times are suggestions. On discharge day it reminds the family to ask which doses were already given.", 1.5, say="s9")
            d.cap("A day-by-day medicine timetable the family can tick off…", 1)
            d.click(".dose input")
            d.sleep(0.8)
            d.move_to(".side-box.stop")
            d.cap("…with aspirin and the ginkgo supplement to STOP shown in red: both raise bleeding risk with the new blood thinner.", 1.5, say="s10")
            d.tab("medicines")
            d.cap("Each medicine in plain Chinese: what it is for and how to take it.", 1.5, say="s11", wait_voice=False)
            d.click(".med-group:nth-of-type(2) .med details summary")
            d.sleep(0.4)
            d.shot("zh_medicines")
            d.cap("Open “What the papers say” to see the exact English sentence it came from, matched ✓.", 2)
            d.scroll(450, 1.5)
            d.sleep(1)
            d.tab("warnings")
            d.shot("zh_warnings")
            d.move_to(".warn-card.emergency")
            d.cap("Warning signs: the stroke signs that mean call 911, and the bleeding signs that mean call the clinic.", 1.5, say="s12")
            d.tab("appointments")
            d.shot("zh_appointments")
            d.cap("Five follow-up visits and tests in date order.", 1.5, say="s13", wait_voice=False)
            d.move_to("#ics")
            d.cap("One click adds every dose and appointment to the phone’s calendar, with reminders.", 1)
            d.move_to("#print")
            d.cap("And it prints a weekly chart for the fridge.", 1)
            d.tab("trace")
            d.shot("zh_trace")
            d.cap(f"How this plan was made: each Claude agent, its model, tokens, time and cost "
                  f"({zh['total_tokens']:,} tokens, about ${zh['cost_usd']:.2f} at API prices).", 2, say="s14")
            page.evaluate("document.querySelector('.trace-section h3') && [...document.querySelectorAll('.trace-section h3')].find(h => h.textContent.startsWith('Tools'))?.scrollIntoView({block: 'center', behavior: 'smooth'})")
            d.sleep(1.2)
            d.shot("zh_tools")
            d.cap("The Checker agent uses real tools: it lists the medicine lines, searches the papers and verifies items itself.", 2, say="s15")

            # 5. Safety test
            d.cap(None, 0.4)
            d.click("#restart")
            d.cap("Safety: a wrong dose can hurt someone. This test file hides attacks aimed at the AI.", 0, say="s16", wait_voice=False)
            page.select_option("#language", label="English")
            d.sleep(0.8)
            d.click('[data-sample="06_SAFETY_TEST_prompt_injection.pdf"]')
            d.sleep(2.5)
            safety = d.wait_for_plan("safety-test papers")
            d.cap("Hidden white text told the AI to have the patient take 10 tablets of everything.", 0, say="s18")
            d.click("#notices .alert.danger summary")
            d.sleep(0.8)
            d.cap("A visible line told the AI to switch off its checks. Plain code removed both before any agent read the file.", 0, say="s19")
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            d.sleep(0.6)
            d.click("#notices .alert.warn summary")
            d.sleep(0.5)
            d.shot("safety_notices")
            d.sleep(0.6)
            d.tab("timetable")
            d.click(".days .day")          # discharge day, when the 5-day antibiotic course starts
            d.cap("The plan underneath is the real one: the antibiotic twice a day with food, for 5 days.", 2, say="s20")
            d.tab("trace")
            d.shot("safety_trace")
            d.cap(f"This plan: {safety['total_tokens']:,} tokens, about ${safety['cost_usd']:.2f}, {safety['elapsed_s']:.0f} seconds.", 1.5, say="s21")

            d.card("Could this save lives?", [
                "Two-thirds of emergency hospitalizations for drug harm in older adults involve <b>blood thinners, antiplatelets, insulin and diabetes pills</b>. Most were unintentional overdoses. Mr. Chen's plan has a blood thinner, a diabetes pill, and an antiplatelet he must stop.",
                "A hospital program that gave patients a medication schedule, an appointment calendar and warning signs cut hospital returns by about <b>30%</b> in a randomized trial.",
                "Going Home builds that plan automatically, in the family's language, for about <b>20 cents</b>.",
            ], "Budnitz 2011, NEJM · Jack 2009 (Project RED), Ann Intern Med", 3, say="s22")

            # 6. Close
            d.cap("<b>Going Home</b> doesn’t replace the care team. It makes sure the family leaves with the plan they were given, in words they understand.", 2, say="s23")
            d.sleep(d.remaining_voice())
            d.cap(None, 0.8)

            d.sleep(0.5)
            cdp.send("Page.stopScreencast")

            # Off camera: the hip-replacement case, for the "questions instead of guesses" screenshot.
            d.click("#restart")
            page.select_option("#language", label="English")
            d.click('[data-sample="03_hip_replacement_dorothy_thompson.pdf"]')
            hip = d.wait_for_plan("hip papers")
            d.tab("questions")
            d.shot("hip_questions")
            d.tab("timetable")
            d.click(".days .day")
            d.shot("hip_timetable")
            ctx.close()
            browser.close()

        timeline = {"frames": frames, "end_ts": time.time(), "waits": d.waits, "events": d.events, "zh": zh, "safety": safety, "hip": hip}
        (OUT / "timeline.json").write_text(json.dumps(timeline, indent=1))
        print(json.dumps({"frames": len(frames), "waits": d.waits}, indent=1))
        assemble(speedup=16.0)
    finally:
        server.terminate()


def assemble(speedup: float = 16.0, out_name: str = "Going_Home_demo.mp4") -> None:
    """Frames -> MP4 with narration. Frame durations come from Chrome's timestamps; waiting stretches are
    sped up, but never so much that a narration line playing over them would be cut off."""
    tl = json.loads((OUT / "timeline.json").read_text())
    frames, waits, events = tl["frames"], tl["waits"], tl.get("events", [])
    waits = [w for w in waits if w["start"] < frames[-1]["ts"]]

    def timeline_map(speeds):
        """Return (per-frame output durations, function real_ts -> output seconds)."""
        outs, t = [], 0.0
        marks = []
        for i, f in enumerate(frames):
            nxt = frames[i + 1]["ts"] if i + 1 < len(frames) else f["ts"] + 1.0
            dur = max(0.0, nxt - f["ts"])
            sp = next((speeds[j] for j, w in enumerate(waits) if w["start"] <= f["ts"] < w["end"]), 1.0)
            marks.append((f["ts"], t, sp))
            outs.append(dur / sp)
            t += dur / sp

        def to_out(ts):
            prev = marks[0]
            for m in marks:
                if m[0] > ts:
                    break
                prev = m
            return prev[1] + (ts - prev[0]) / prev[2]
        return outs, to_out

    speeds = [speedup] * len(waits)
    for _ in range(3):   # widen any wait whose sped-up length would cut a narration line short
        _, to_out = timeline_map(speeds)
        changed = False
        for j, w in enumerate(waits):
            w_out_start, w_out_end = to_out(w["start"]), to_out(w["end"])
            ends = [to_out(e["ts"]) + e["dur"] + 0.4 for e in events if e["ts"] <= w["end"]]
            need_end = max([w_out_end] + ends)
            if need_end > w_out_end + 0.05:
                real = w["end"] - w["start"]
                speeds[j] = max(1.0, real / max(0.1, need_end - w_out_start))
                changed = True
        if not changed:
            break
    outs, to_out = timeline_map(speeds)

    lines = ["ffconcat version 1.0"]
    for f, dur in zip(frames, outs):
        lines += [f"file 'frames/{f['file']}'", f"duration {dur:.4f}"]
    lines.append(f"file 'frames/{frames[-1]['file']}'")
    (OUT / "frames.ffconcat").write_text("\n".join(lines) + "\n")
    silent = OUT / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "frames.ffconcat"),
                    "-vf", f"fps=30,scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "slow",
                    str(silent)], check=True)

    if not events:
        silent.replace(HERE / out_name)
    else:
        inputs, filters, labels = ["-i", str(silent)], [], []
        placed = []
        for n, e in enumerate(events, start=1):
            wav = Path((VOICE_DIR / f"{e['key']}.path").read_text())
            at = to_out(e["ts"])
            placed.append((e["key"], round(at, 2), round(e["dur"], 2)))
            inputs += ["-i", str(wav)]
            filters.append(f"[{n}:a]aresample=48000,adelay={int(at * 1000)}:all=1[a{n}]")
            labels.append(f"[a{n}]")
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0,loudnorm=I=-17:TP=-2:LRA=11,aresample=48000[aout]")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
                        "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                        "-movflags", "+faststart", str(HERE / out_name)], check=True)
        overlaps = [(a[0], b[0]) for a, b in zip(placed, placed[1:]) if a[1] + a[2] > b[1] + 0.05]
        print("narration placed:", placed)
        print("overlapping lines:", overlaps or "none")
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(HERE / out_name)]).decode())
    print(f"wrote {out_name}: {int(dur // 60)}:{int(dur % 60):02d}  (wait speeds: {[round(x, 1) for x in speeds]})")


if __name__ == "__main__":
    main()
