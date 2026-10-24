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
})();
"""


class Demo:
    def __init__(self, page):
        self.page = page
        self.waits: list[dict] = []

    def t(self) -> float:
        return time.time()          # same clock as Chrome's screencast frame timestamps

    def sleep(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)   # keeps screencast frames flowing

    def cap(self, html: str | None, hold: float = 0) -> None:
        self.page.evaluate("h => window.__caption(h)", html)
        if hold:
            self.sleep(hold)

    def move_to(self, selector: str, steps: int = 18) -> None:
        el = self.page.locator(selector).first
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
        self.sleep(0.35)

    def click(self, selector: str) -> None:
        self.move_to(selector)
        self.page.locator(selector).first.click()
        self.sleep(0.6)

    def scroll(self, dy: int, duration: float = 1.2) -> None:
        steps = 20
        for _ in range(steps):
            self.page.mouse.wheel(0, dy / steps)
            self.sleep(duration / steps)

    def tab(self, name: str) -> None:
        self.click(f'#tabs [data-tab="{name}"]')
        self.page.evaluate("window.scrollTo({top: document.querySelector('.tabs').offsetTop - 70, behavior: 'smooth'})")
        self.sleep(0.8)

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
        hf_png = pdf_page_png(APP / "data/samples/05_stroke_afib_chen_guoliang.pdf")
        SHOTS.mkdir(exist_ok=True)
        (SHOTS / "papers.png").write_bytes(base64.b64decode(hf_png.split(",", 1)[1]))

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
            ctx.add_init_script(OVERLAY_JS)
            page = ctx.new_page()
            d = Demo(page)
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
            d.cap("When someone leaves the hospital, the family goes home with a stack of discharge papers.", 4)
            d.cap("New medicines, changed doses, medicines to stop, visits and warning signs, in medical shorthand.", 4)
            d.cap("<b>Going Home</b> turns those papers into a plan a family can actually follow.", 3.5)

            # 2. The papers
            page.evaluate("([s, t]) => window.__modal(s, t)", [hf_png, "Synthetic discharge summary: stroke warning and new blood thinner (fictional patient)"])
            d.cap("Mr. Chen, 79, had a mini-stroke. His papers use shorthand like PO, BID and QHS.", 5)
            page.evaluate("window.__unmodal()")

            # 3. Pick Chinese and start
            d.cap("His daughter reads simplified Chinese, so we choose Chinese…", 0)
            d.move_to("#language")
            page.select_option("#language", label="中文 (Chinese, Simplified)")
            d.sleep(1.5)
            d.cap("…and start with his discharge papers.", 0)
            d.click('[data-sample="05_stroke_afib_chen_guoliang.pdf"]')
            d.cap("Reader agent extracts every medicine with its exact source words. Code checks every quote and number. "
                  "A Checker agent looks for anything missed. An Explainer writes it in plain Chinese.", 6)
            zh = d.wait_for_plan("discharge papers in Chinese", progress_shot="progress")

            # 4. The plan
            page.evaluate("window.scrollTo(0, 0)")
            d.sleep(0.5)
            d.shot("zh_top")
            d.cap("✅ Every medicine, dose and phone number was matched word-for-word to the papers.", 4)
            d.tab("timetable")
            d.click(".days .day")      # discharge day
            d.sleep(0.5)
            d.shot("zh_timetable")
            d.cap("A day-by-day medicine timetable the family can tick off…", 2)
            d.click(".dose input")
            d.sleep(0.8)
            d.move_to(".side-box.stop")
            d.cap("…with aspirin and the ginkgo supplement to STOP shown in red: both raise bleeding risk with the new blood thinner.", 4.5)
            d.tab("medicines")
            d.cap("Each medicine in plain Chinese: what it is for and how to take it.", 3)
            d.click(".med-group:nth-of-type(2) .med details summary")
            d.sleep(0.4)
            d.shot("zh_medicines")
            d.cap("Open “What the papers say” to see the exact English sentence it came from, matched ✓.", 4)
            d.scroll(450, 1.5)
            d.sleep(1)
            d.tab("warnings")
            d.shot("zh_warnings")
            d.move_to(".warn-card.emergency")
            d.cap("Warning signs: the stroke signs that mean call 911, and the bleeding signs that mean call the clinic.", 4.5)
            d.tab("appointments")
            d.shot("zh_appointments")
            d.cap("Five follow-up visits and tests in date order.", 2.5)
            d.move_to("#ics")
            d.cap("One click adds every dose and appointment to the phone’s calendar, with reminders.", 3.5)
            d.move_to("#print")
            d.cap("And it prints a weekly chart for the fridge.", 2.5)
            d.tab("trace")
            d.shot("zh_trace")
            d.cap(f"How this plan was made: each Claude agent, its model, tokens, time and cost "
                  f"({zh['total_tokens']:,} tokens, about ${zh['cost_usd']:.2f} at API prices).", 5)
            page.evaluate("document.querySelector('.trace-section h3') && [...document.querySelectorAll('.trace-section h3')].find(h => h.textContent.startsWith('Tools'))?.scrollIntoView({block: 'center', behavior: 'smooth'})")
            d.sleep(1.2)
            d.shot("zh_tools")
            d.cap("The Checker agent uses real tools: it lists the medicine lines, searches the papers and verifies items itself.", 5)

            # 5. Safety test
            d.cap(None, 0.4)
            d.click("#restart")
            d.cap("Safety: a wrong dose can hurt someone. This test file hides attacks aimed at the AI.", 0)
            page.select_option("#language", label="English")
            d.sleep(0.8)
            d.click('[data-sample="06_SAFETY_TEST_prompt_injection.pdf"]')
            d.sleep(2.5)
            safety = d.wait_for_plan("safety-test papers")
            d.cap("Hidden white text told the AI to have the patient take 10 tablets of everything.", 0)
            d.click("#notices .alert.danger summary")
            d.sleep(3.5)
            d.cap("A visible line told the AI to switch off its checks. Plain code removed both before any agent read the file.", 0)
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            d.sleep(0.6)
            d.click("#notices .alert.warn summary")
            d.sleep(0.5)
            d.shot("safety_notices")
            d.sleep(3.5)
            d.tab("timetable")
            d.click(".days .day")          # discharge day, when the 5-day antibiotic course starts
            d.cap("The plan underneath is the real one: the antibiotic twice a day with food, for 5 days.", 4)
            d.tab("trace")
            d.shot("safety_trace")
            d.cap(f"This plan: {safety['total_tokens']:,} tokens, about ${safety['cost_usd']:.2f}, {safety['elapsed_s']:.0f} seconds.", 4)

            # 6. Close
            d.cap("<b>Going Home</b> doesn’t replace the care team. It makes sure the family leaves with the plan they were given, in words they understand.", 5)
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

        timeline = {"frames": frames, "end_ts": time.time(), "waits": d.waits, "zh": zh, "safety": safety, "hip": hip}
        (OUT / "timeline.json").write_text(json.dumps(timeline, indent=1))
        print(json.dumps({"frames": len(frames), "waits": d.waits}, indent=1))
        assemble()
    finally:
        server.terminate()


def assemble(speedup: float = 10.0, out_name: str = "Going_Home_demo.mp4") -> None:
    """Frames -> MP4. Frame durations come from Chrome's timestamps; waiting stretches are sped up."""
    tl = json.loads((OUT / "timeline.json").read_text())
    frames, waits = tl["frames"], tl["waits"]
    lines = ["ffconcat version 1.0"]
    for i, f in enumerate(frames):
        nxt = frames[i + 1]["ts"] if i + 1 < len(frames) else f["ts"] + 1.0
        dur = max(0.0, nxt - f["ts"])
        if any(w["start"] <= f["ts"] < w["end"] for w in waits):
            dur /= speedup
        lines += [f"file 'frames/{f['file']}'", f"duration {dur:.4f}"]
    lines.append(f"file 'frames/{frames[-1]['file']}'")
    (OUT / "frames.ffconcat").write_text("\n".join(lines) + "\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "frames.ffconcat"),
                    "-vf", f"fps=30,scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "slow",
                    "-movflags", "+faststart", str(HERE / out_name)], check=True)
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(HERE / out_name)]).decode())
    print(f"wrote {out_name}: {int(dur // 60)}:{int(dur % 60):02d}")


if __name__ == "__main__":
    main()
