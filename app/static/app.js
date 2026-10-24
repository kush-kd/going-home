/* Going Home front end: upload -> poll the run -> render the plan. No framework, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// Everything that came from a model or a document is escaped before it touches the page.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { file: null, runId: null, run: null, day: null, labels: {} };

const L = (key, fallback) => state.labels[key] || fallback;
const STATUS_TEXT = { new: "New", changed: "Changed", continue: "Keep taking", stop: "Stop" };
const statusLabel = (s) => L(s === "stop" ? "stop_short" : s, STATUS_TEXT[s] || s);

// ------------------------------------------------------------ dates & times
const parseISO = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const LOCALES = { "Español": "es", "हिन्दी": "hi-IN", "中文": "zh-CN", "Tiếng": "vi", "Português": "pt", "Français": "fr", "Kreyòl": "fr-HT", "العربية": "ar" };
const fmtDate = (iso, opts) => parseISO(iso).toLocaleDateString(state.locale, opts);
const fmtTime = (hhmm) => { let [h, m] = hhmm.split(":").map(Number); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12; return `${h}:${String(m).padStart(2, "0")} ${ap}`; };

// ------------------------------------------------------------ start screen
async function init() {
  let status;
  try {
    status = await (await fetch("/api/status")).json();
  } catch {
    $("#provider").textContent = "Server not reachable";
    $("#provider").className = "pill bad";
    return;
  }
  const llm = status.llm;
  state.demo = status.demo;
  if (status.demo) {
    $("#provider").textContent = "Demo mode: replaying recorded real runs";
    $("#provider").className = "pill warn";
    $("#provider").title = llm.message;
    $("#demo-banner").hidden = false;
    if (status.demo_reason === "forced") {
      $("#demo-why").textContent = "This copy is set to demo mode (GOING_HOME_MODE=demo), so";
      $("#demo-reason").textContent = "";
    } else {
      $("#demo-reason").textContent = `(${llm.message})`;
    }
    $("#upload-form").classList.add("disabled");
    $("#file").disabled = true;
  } else {
    $("#provider").textContent = llm.ready ? `AI: ${llm.message}` : "AI backend not ready";
    $("#provider").className = "pill " + (llm.ready ? "ok" : "bad");
    $("#provider").title = llm.message;
  }

  $("#language").innerHTML = status.languages.map((l) => `<option>${esc(l)}</option>`).join("");

  $("#samples").innerHTML = status.samples.map((s) => `
    <div class="sample ${s.safety_test ? "safety" : ""}">
      <div><strong>${esc(s.title)}</strong><small>${esc(s.subtitle)}</small>${status.demo && s.recorded.length ? `<small class="rec">Recorded runs: ${esc(s.recorded.join(", "))}</small>` : ""}</div>
      <div class="btns">
        <a class="btn ghost" href="/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener">View</a>
        <button class="btn primary" data-sample="${esc(s.file)}">Use this</button>
      </div>
    </div>`).join("");
  $$("[data-sample]").forEach((b) => b.addEventListener("click", () => startSample(b.dataset.sample)));
}

function setFile(file) {
  state.file = file;
  const dz = $("#dropzone");
  dz.classList.toggle("has-file", !!file);
  $("#drop-title").textContent = file ? file.name : "Drop a PDF or photo here";
  $("#drop-sub").textContent = file ? `${(file.size / 1024).toFixed(0)} KB. Click to choose a different file.` : "or click to choose a file (PDF, JPG, PNG; up to 10 MB)";
  $("#go").disabled = !file;
}

function wireStart() {
  const dz = $("#dropzone");
  $("#file").addEventListener("change", (e) => setFile(e.target.files[0]));
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file").click(); } });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
  $("#upload-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!state.file) return;
    const fd = new FormData();
    fd.append("file", state.file);
    fd.append("language", $("#language").value);
    startRun("/api/runs", fd);
  });
  $("#restart").addEventListener("click", reset);
  $("#retry").addEventListener("click", reset);
  $("#print").addEventListener("click", () => { renderPrint(); window.print(); });
  $$("#tabs [role=tab]").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
}

function startSample(name) {
  const fd = new FormData();
  fd.append("name", name);
  fd.append("language", $("#language").value);
  startRun("/api/runs/sample", fd);
}

async function startRun(url, fd) {
  show("progress");
  $("#steps").innerHTML = "";
  $("#run-error").hidden = true;
  $("#retry").hidden = true;
  $(".spinner").classList.remove("stopped");
  $("#progress-title").textContent = state.demo ? "Replaying a recorded real run…" : "Making your plan…";
  $("#progress-sub").textContent = state.demo ? "Demo mode: these are the real steps, models and token counts from a run recorded earlier, shown faster than real time." : "Four AI agents and three safety checks are working through the papers. This usually takes 1 to 3 minutes.";
  try {
    const res = await fetch(url, { method: "POST", body: fd });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Could not start");
    state.runId = body.run_id;
    poll();
  } catch (err) {
    fail(err.message);
  }
}

async function poll() {
  const res = await fetch(`/api/runs/${state.runId}`);
  const run = await res.json();
  renderSteps(run.steps);
  if (run.status === "running") return setTimeout(poll, 1000);
  if (run.status === "error") return fail(run.error);
  state.run = run;
  renderResult(run);
}

function fail(msg) {
  $(".spinner").classList.add("stopped");
  $("#progress-title").textContent = "We could not make the plan";
  $("#run-error").innerHTML = `<span class="ico">⚠️</span><div>${esc(msg)}</div>`;
  $("#run-error").hidden = false;
  $("#retry").hidden = false;
}

function renderSteps(steps) {
  $("#steps").innerHTML = steps.map((s) => {
    const t = s.tokens ? s.tokens.input_tokens + s.tokens.cache_write_tokens + s.tokens.cache_read_tokens + s.tokens.output_tokens : null;
    const bits = [];
    if (s.model) bits.push(esc(s.model));
    if (t) bits.push(`${t.toLocaleString()} tokens`);
    if (s.elapsed_s != null) bits.push(`${s.elapsed_s}s`);
    if (s.failed != null) bits.push(s.failed ? `${s.failed} item(s) flagged` : "all items matched");
    if (s.tools_used) bits.push(`${s.tools_used} tool calls`);
    if (s.issues != null) bits.push(`${s.issues} issue(s)` + (s.dropped ? `, ${s.dropped} unsupported dropped` : ""));
    if (s.rejected != null) bits.push(s.rejected ? `${s.rejected} rejected` : "all numbers matched");
    if (s.hidden_text) bits.push(`${s.hidden_text} hidden text block(s) removed`);
    if (s.removed_lines) bits.push(`${s.removed_lines} suspicious line(s) removed`);
    if (s.message) bits.push(esc(s.message));
    const icon = s.status === "done" ? "✓" : s.status === "error" ? "!" : s.status === "restarted" ? "↻" : "";
    return `<li class="${s.status}"><span class="icon">${icon}</span>
      <div>${s.agent ? `<div class="agent">${esc(s.agent)} agent</div>` : `<div class="agent" style="color:var(--muted)">program</div>`}${esc(s.title)}</div>
      <div class="meta">${bits.join(" · ")}</div></li>`;
  }).join("");
}

function show(id) {
  ["start", "progress", "result"].forEach((s) => ($("#" + s).hidden = s !== id));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function reset() {
  state.runId = null; state.run = null; state.day = null;
  setFile(null);
  $("#file").value = "";
  show("start");
}

function showTab(name) {
  $$("#tabs [role=tab]").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === name));
  $$(".panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
}

// ------------------------------------------------------------ the plan
function medMap(r) { return Object.fromEntries(r.extraction.medications.map((m) => [m.id, m])); }

function renderResult(run) {
  const r = run.result;
  state.labels = r.explanation.labels || {};
  state.locale = LOCALES[r.language.split(" ")[0]] || undefined;
  const rtl = /arab/i.test(r.language);
  $("#result").dir = rtl ? "rtl" : "ltr";
  $$("[data-label]").forEach((el) => (el.textContent = L(el.dataset.label, el.textContent)));

  const ex = r.extraction;
  $("#plan-eyebrow").textContent = ex.discharge_date ? `Discharged ${fmtDate(ex.discharge_date, { weekday: "long", month: "long", day: "numeric", year: "numeric" })} · ${r.language}` : r.language;
  $("#plan-title").textContent = ex.diagnosis ? ex.diagnosis.split(/[.;]/)[0] : "Your plan for going home";
  $("#ics").href = `/api/runs/${run.id}/calendar.ics`;

  if (r.explanation.summary) {
    $("#summary").innerHTML = `<h2>${esc(L("summary", "Why you were in the hospital"))}</h2><p>${esc(r.explanation.summary)}</p>`;
    $("#summary").hidden = false;
  } else $("#summary").hidden = true;

  renderNotices(r);
  const s = r.schedule;
  const t = todayISO();
  state.day = s.days.some((d) => d.date === t) ? t : s.days[0]?.date;
  renderTimetable(r);
  renderMedicines(r);
  renderWarnings(r);
  renderAppointments(r);
  renderQuestions(r);
  renderInstructions(r);
  renderTrace(run);
  showTab("timetable");
  show("result");
}

function renderNotices(r) {
  const out = [];
  if (r.replay) {
    const when = r.replay.recorded_at ? new Date(r.replay.recorded_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "earlier";
    const other = r.replay.requested_language && r.replay.requested_language !== r.replay.language
      ? ` You chose ${esc(r.replay.requested_language)}, but this sample was recorded in ${esc(r.replay.language)}; other languages need a live run.` : "";
    out.push(`<div class="alert info"><span class="ico">▶️</span><div><strong>Demo mode: this is a replay of a real run recorded ${esc(when)}</strong> with ${esc(r.replay.models.join(" and "))}. Nothing was generated just now.${other} To make plans live, log in to Claude Code on this computer (see the README).</div></div>`);
  }
  const d = r.document;
  if (d.hidden_text.length) {
    out.push(`<div class="alert danger"><span class="ico">🛡️</span><div><strong>Hidden text was found in this document and ignored.</strong> It was invisible on the page (white or tiny text), which is a common way to sneak instructions to an AI.
      <details><summary>Show what was hidden</summary><code>${esc(d.hidden_text.join("\n"))}</code></details></div></div>`);
  }
  if (d.removed_lines.length) {
    out.push(`<div class="alert warn"><span class="ico">🛡️</span><div><strong>${d.removed_lines.length} line(s) that tried to give orders to an AI were removed</strong> before any agent read the document.
      <details><summary>Show removed lines</summary><code>${esc(d.removed_lines.join("\n"))}</code></details></div></div>`);
  }
  const unver = r.schedule.unscheduled.filter((u) => u.reason === "needs_check").length;
  const remaining = r.checker.remaining_issues.filter((i) => i.severity !== "low");
  if (unver || remaining.length) {
    out.push(`<div class="alert warn"><span class="ico">⚠️</span><div><strong>${esc(L("check_with_team", "Check with your care team"))}:</strong> ${unver ? `${unver} medicine(s) could not be confirmed word-for-word against your papers, so they are not on the timetable.` : ""} ${remaining.length ? `The checker still has ${remaining.length} concern(s); see Questions for the doctor.` : ""}</div></div>`);
  }
  if (r.explainer_flags.length) {
    out.push(`<div class="alert info"><span class="ico">🔢</span><div>${r.explainer_flags.length} explanation(s) contained a number that is not in your papers, so we show the doctor's exact words for those instead.</div></div>`);
  }
  if (!out.length) {
    out.push(`<div class="alert ok"><span class="ico">✅</span><div><strong>Every medicine, dose and phone number on this plan was matched word-for-word to your discharge papers.</strong> Still, follow your care team if anything differs.</div></div>`);
  }
  $("#notices").innerHTML = out.join("");
}

function medTitle(m) { return `${esc(m.name)}${m.strength && !m.name.includes(m.strength) ? ` <span class="muted">${esc(m.strength)}</span>` : ""}`; }

function renderTimetable(r) {
  const panel = $('[data-panel="timetable"]');
  const meds = medMap(r);
  const s = r.schedule;
  const t = todayISO();
  const dayButtons = s.days.map((d) => `<button class="day ${d.date === t ? "today" : ""}" data-day="${d.date}" aria-pressed="${d.date === state.day}">
      <small>${esc(fmtDate(d.date, { weekday: "short" }))}</small><b>${parseISO(d.date).getDate()}</b><small>${esc(fmtDate(d.date, { month: "short" }))}</small></button>`).join("");
  const day = s.days.find((d) => d.date === state.day);
  const slots = day && day.slots.length ? day.slots.map((slot) => `
      <div class="slot">
        <div class="slot-time"><b>${fmtTime(slot.time)}</b><span>${esc(L(slot.label, slot.label))}</span></div>
        <div>${slot.items.map((it) => {
          const m = meds[it.med_id];
          const key = `gh:${state.runId}:${day.date}:${slot.time}:${it.med_id}`;
          const taken = localStorage.getItem(key) === "1";
          const notes = [];
          if (m.meal_relation === "with_food") notes.push("With food");
          if (m.meal_relation === "before_breakfast") notes.push("Empty stomach, before breakfast");
          if (m.meal_relation === "empty_stomach") notes.push("On an empty stomach");
          if (m.meal_relation === "after_meals") notes.push("After meals");
          if (m.route && !/mouth|^\s*p\.?o\.?\s*$|oral/i.test(m.route)) notes.push(m.route);   // "PO" means by mouth
          return `<label class="dose ${taken ? "taken" : ""}">
            <input type="checkbox" data-key="${esc(key)}" ${taken ? "checked" : ""} aria-label="Mark ${esc(m.name)} as given">
            <div><div class="name">${medTitle(m)}<span class="badge ${m.status}">${esc(statusLabel(m.status))}</span>${it.last_day ? `<span class="badge last">${esc(L("last_day", "Last day"))}</span>` : ""}</div>
            <div class="amt">${esc(it.dose)}</div>${notes.length ? `<div class="note">${esc(notes.join(" · "))}</div>` : ""}</div>
          </label>`;
        }).join("")}</div>
      </div>`).join("") : `<p class="empty">No scheduled medicines on this day.</p>`;

  const prn = s.as_needed.map((id) => meds[id]).map((m) => `<li><strong>${esc(m.name)}</strong> ${esc(m.dose || "")}${m.interval_hours ? `, no more often than every ${m.interval_hours} hours` : ""}${m.max_per_day ? `<br><span class="note">${esc(L("max", "Daily limit"))}: ${esc(m.max_per_day)}</span>` : ""}</li>`).join("");
  const stop = s.stopped.map((id) => meds[id]).map((m) => `<li><strong>${esc(m.name)}</strong></li>`).join("");
  const check = s.unscheduled.map((u) => `<li><strong>${esc(meds[u.id].name)}</strong><br><span class="note">${u.reason === "needs_check" ? "Could not be confirmed against your papers" : "The papers do not give a clear schedule"}</span></li>`).join("");

  panel.innerHTML = `<div class="days" role="group" aria-label="Choose a day">${dayButtons}</div>
    <div class="tt-grid">
      <div class="card">${slots}</div>
      <aside>
        ${stop ? `<div class="side-box stop"><h3>⛔ ${esc(L("stop", "Stop taking these"))}</h3><ul>${stop}</ul></div>` : ""}
        ${prn ? `<div class="side-box prn"><h3>💊 ${esc(L("as_needed", "Only if needed"))}</h3><ul>${prn}</ul></div>` : ""}
        ${check ? `<div class="side-box check"><h3>❓ ${esc(L("check_with_team", "Check with your care team"))}</h3><ul>${check}</ul></div>` : ""}
      </aside>
    </div>`;
  $$("[data-day]", panel).forEach((b) => b.addEventListener("click", () => { state.day = b.dataset.day; renderTimetable(r); }));
  $$("input[data-key]", panel).forEach((cb) => cb.addEventListener("change", () => {
    localStorage.setItem(cb.dataset.key, cb.checked ? "1" : "0");
    cb.closest(".dose").classList.toggle("taken", cb.checked);
  }));
  const sel = $(`[data-day="${state.day}"]`, panel);
  if (sel) sel.scrollIntoView({ block: "nearest", inline: "center" });
}

function renderMedicines(r) {
  const panel = $('[data-panel="medicines"]');
  const courses = Object.fromEntries(r.schedule.courses.map((c) => [c.med_id, c]));
  const card = (m) => {
    const e = r.explanation.medications[m.id] || {};
    const v = r.verification[m.id] || {};
    const c = courses[m.id];
    const range = c ? (c.last_day ? `${fmtDate(c.first_day, { month: "short", day: "numeric" })} to ${fmtDate(c.last_day, { month: "short", day: "numeric" })}` : `From ${fmtDate(c.first_day, { month: "short", day: "numeric" })}, keep taking until told to stop`) : "";
    return `<article class="med ${m.status === "stop" ? "stop" : ""}">
      <h3>${esc(m.name)} <span class="badge ${m.status}">${esc(statusLabel(m.status))}</span></h3>
      <div class="strength">${esc([m.strength, m.route].filter(Boolean).join(" · "))}</div>
      ${e.what_for ? `<div class="label">${esc(L("what_for", "What it is for"))}</div><div>${esc(e.what_for)}</div>` : ""}
      <div class="label">${esc(L("how_to_take", "How to take it"))}</div><div>${esc(e.how_to_take || m.source_quote)}</div>
      ${e.tips && e.tips.length ? `<ul class="tips">${e.tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      ${c && m.schedule_kind !== "taper" ? `<div class="times">${c.times.map((t) => `<span>${fmtTime(t)}</span>`).join("")}</div>` : ""}
      ${m.taper.length ? `<div class="times">${m.taper.map((s) => `<span>${esc(fmtDate(s.start_date, { month: "short", day: "numeric" }))}–${esc(fmtDate(s.end_date, { month: "short", day: "numeric" }))}: ${esc(s.dose)}</span>`).join("")}</div>` : ""}
      ${range && m.status !== "stop" ? `<div class="note muted" style="margin-top:.4rem">${esc(range)}</div>` : ""}
      <details><summary>What the papers say ${v.verified ? '<span class="badge ok">✓ matched</span>' : '<span class="badge warn">needs check</span>'}</summary>
        <blockquote>${esc(m.source_quote)}</blockquote>${v.problems && v.problems.length ? `<p class="note">${esc(v.problems.join("; "))}</p>` : ""}${e.fallback ? `<p class="note">Showing the doctor's exact words for safety.</p>` : ""}</details>
    </article>`;
  };
  const group = (status, title) => {
    const ms = r.extraction.medications.filter((m) => m.status === status);
    return ms.length ? `<section class="med-group"><h2>${esc(title)}</h2><div class="med-cards">${ms.map(card).join("")}</div></section>` : "";
  };
  panel.innerHTML = group("stop", "⛔ " + L("stop", "Stop taking these")) + group("new", L("new", "New")) + group("changed", L("changed", "Changed")) + group("continue", L("continue", "Keep taking"));
}

function telHref(p) { return "tel:" + String(p).replace(/[^\d+]/g, ""); }

function renderWarnings(r) {
  const panel = $('[data-panel="warnings"]');
  const ws = [...r.extraction.warnings].sort((a, b) => (a.level === "emergency" ? -1 : 1) - (b.level === "emergency" ? -1 : 1));
  panel.innerHTML = ws.length ? `<div class="warn-grid">${ws.map((w) => {
    const e = r.explanation.warnings[w.id] || { who_to_call: w.who_to_call, signs: w.signs };
    const emergency = w.level === "emergency";
    const phone = emergency && !w.phone ? "911" : w.phone;
    return `<div class="warn-card ${w.level}">
      <h2>${emergency ? "🚑" : "📞"} ${esc(emergency ? L("emergency", "Call 911 or go to the emergency room") : L("call", "Call your care team if"))}</h2>
      <ul>${e.signs.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="muted" style="margin-bottom:.5rem">${esc(e.who_to_call)}</div>
      ${phone ? `<a class="phone" href="${telHref(phone)}">📞 ${esc(phone)}</a>` : ""}
    </div>`;
  }).join("")}</div>` : `<p class="empty">No warning signs were listed in these papers. Ask your care team when to call.</p>`;
}

function renderAppointments(r) {
  const panel = $('[data-panel="appointments"]');
  const as = [...r.extraction.appointments].sort((a, b) => (a.date || "9999").localeCompare(b.date || "9999") || (a.time || "").localeCompare(b.time || ""));
  panel.innerHTML = as.length ? as.map((a) => {
    const e = r.explanation.appointments[a.id];
    const kind = { visit: "Visit", test: "Test", call_to_schedule: "You need to call", they_will_call: "They will call you" }[a.kind];
    return `<div class="appt">
      ${a.date ? `<div class="date"><small>${esc(fmtDate(a.date, { month: "short" }))}</small><b>${parseISO(a.date).getDate()}</b><small>${esc(fmtDate(a.date, { weekday: "short" }))}</small></div>` : `<div class="date none">${esc(kind)}</div>`}
      <div><h3>${esc(a.with_whom)}</h3>
        <div class="meta">${esc([a.time && fmtTime(a.time), a.time_window, a.location].filter(Boolean).join(" · "))}</div>
        <p style="margin:.4rem 0 0">${esc(e ? e.text : a.source_quote)}</p>
        ${a.phone ? `<a class="btn" style="margin-top:.5rem" href="${telHref(a.phone)}">📞 ${esc(a.phone)}</a>` : ""}
      </div></div>`;
  }).join("") : `<p class="empty">No appointments were listed.</p>`;
}

function renderQuestions(r) {
  const panel = $('[data-panel="questions"]');
  const qs = r.explanation.questions;
  $("#q-count").textContent = qs.length || "";
  panel.innerHTML = qs.length ? `<p class="muted">Bring these to your next visit or call. Tick them off as you get answers.</p>
    <ul class="qlist">${qs.map((q, i) => `<li><input type="checkbox" id="q${i}"><label for="q${i}"><strong>${esc(q.question)}</strong><div class="why">${esc(q.reason)}</div></label></li>`).join("")}</ul>`
    : `<div class="alert ok"><span class="ico">✅</span><div>Nothing in the papers was unclear. You can still ask your care team anything.</div></div>`;
}

function renderInstructions(r) {
  const panel = $('[data-panel="instructions"]');
  const icons = { diet: "🥗", activity: "🚶", monitoring: "📋", wound_care: "🩹", equipment: "🫁", other: "📌" };
  const is = r.extraction.instructions;
  panel.innerHTML = is.length ? `<ul class="ilist">${is.map((i) => `<li><span class="cat">${icons[i.category] || "📌"}</span><div>${esc((r.explanation.instructions[i.id] || {}).text || i.text)}</div></li>`).join("")}</ul>` : `<p class="empty">No diet or activity instructions were listed.</p>`;
}

function renderTrace(run) {
  const r = run.result;
  const m = r.metrics;
  const panel = $('[data-panel="trace"]');
  const verified = Object.values(r.verification);
  const ok = verified.filter((v) => v.verified).length;
  const rows = m.calls.map((c) => `<tr><td>${esc(c.agent)}</td><td>${esc(c.step)}</td><td>${esc(c.model)}</td>
      <td class="num">${c.input_tokens.toLocaleString()}</td><td class="num">${c.cache_write_tokens.toLocaleString()}</td><td class="num">${c.cache_read_tokens.toLocaleString()}</td><td class="num">${c.output_tokens.toLocaleString()}</td>
      <td class="num">${c.latency_s}s</td><td class="num">${c.cost_usd != null ? "$" + c.cost_usd.toFixed(4) : "–"}</td></tr>`).join("");
  const T = m.totals;
  const toolCalls = m.calls.flatMap((c) => (c.tool_calls || []).map((t) => ({ ...t, agent: c.agent })));
  const toolTable = toolCalls.length ? `<div class="trace-section"><h3>Tools the Checker called (${toolCalls.length})</h3><table class="trace"><tr><th>#</th><th>Tool</th><th>Input</th><th>Result</th></tr>${toolCalls.map((t, i) => `<tr><td>${i + 1}</td><td>${esc(t.tool)}</td><td>${esc(Object.values(t.args || {}).join(", "))}</td><td>${esc(typeof t.result === "string" ? t.result : JSON.stringify(t.result)).slice(0, 160)}</td></tr>`).join("")}</table></div>` : "";
  const issues = (list) => list.length ? `<table class="trace"><tr><th>Severity</th><th>Problem</th><th>Document says</th></tr>${list.map((i) => `<tr><td>${esc(i.severity)}</td><td>${esc(i.problem)}</td><td>${esc(i.document_quote)}</td></tr>`).join("")}</table>` : `<p class="muted">None.</p>`;
  panel.innerHTML = `
    <div class="trace-cards">
      <div class="stat"><b>${m.total_tokens.toLocaleString()}</b><span>tokens used (all agents)</span></div>
      <div class="stat"><b>${m.cost_usd != null ? "$" + m.cost_usd.toFixed(3) : "–"}</b><span>cost at API list prices</span></div>
      <div class="stat"><b>${m.elapsed_s}s</b><span>time to make the plan</span></div>
      <div class="stat"><b>${ok}/${verified.length}</b><span>items matched word-for-word</span></div>
    </div>
    ${m.reading_grade_document != null ? `<div class="alert info"><span class="ico">📖</span><div>${m.reading_grade_plan != null
      ? `Reading level went from <strong>grade ${m.reading_grade_document}</strong> (hospital papers) to <strong>grade ${m.reading_grade_plan}</strong> (this plan), by the Flesch-Kincaid formula.`
      : `The hospital papers read at <strong>grade ${m.reading_grade_document}</strong> (Flesch-Kincaid). That formula only works for English, so this plan's level is not scored.`}</div></div>` : ""}
    <h3>How the agents worked together</h3>
    <div class="flow"><span>Safety screen</span><i>→</i>${r.document.source_kind === "photo" || r.document.source_kind === "scanned_pdf" ? `<span class="ai">Transcriber</span><i>→</i>` : ""}<span class="ai">Reader</span><i>→</i><span>Quote and number check</span><i>→</i><span class="ai">Checker</span><i>+</i><span class="ai">Explainer</span>${r.checker.repaired ? `<i>→</i><span class="ai">Reader repair</span>${r.checker.rechecked ? `<i>→</i><span class="ai">Checker again</span>` : `<i>→</i><span>Quote check again</span>`}` : ""}<i>→</i><span>Scheduler</span><i>→</i><span>Number check</span></div>
    <table class="trace"><tr><th>Agent</th><th>Step</th><th>Model</th><th>Input</th><th>Cache write</th><th>Cache read</th><th>Output</th><th>Time</th><th>Cost</th></tr>${rows}
      <tr><td colspan="3"><strong>Total</strong></td><td class="num"><strong>${T.input_tokens.toLocaleString()}</strong></td><td class="num"><strong>${T.cache_write_tokens.toLocaleString()}</strong></td><td class="num"><strong>${T.cache_read_tokens.toLocaleString()}</strong></td><td class="num"><strong>${T.output_tokens.toLocaleString()}</strong></td><td class="num"><strong>${m.elapsed_s}s</strong></td><td class="num"><strong>${m.cost_usd != null ? "$" + m.cost_usd.toFixed(4) : "–"}</strong></td></tr></table>
    ${Object.keys(r.checker.initial_grounding_problems || {}).length ? `<div class="trace-section"><h3>Quote and number check: caught on the first read, sent back to the Reader</h3><table class="trace"><tr><th>Item</th><th>Problem</th></tr>${Object.entries(r.checker.initial_grounding_problems).map(([id, ps]) => `<tr><td>${esc(id)}</td><td>${esc(ps.join("; "))}</td></tr>`).join("")}</table></div>` : ""}
    ${toolTable}
    <div class="trace-section"><h3>Checker: first review</h3>${issues(r.checker.initial_issues)}</div>
    ${r.checker.rechecked ? `<div class="trace-section"><h3>Checker: after the repair round</h3>${issues(r.checker.remaining_issues)}</div>` : ""}
    ${r.checker.dropped_unsupported.length ? `<div class="trace-section"><h3>Checker claims thrown out (quote not in document)</h3>${issues(r.checker.dropped_unsupported)}</div>` : ""}
    <div class="trace-section"><h3>Quote and number check</h3><table class="trace"><tr><th>Item</th><th>Result</th><th>Match</th><th>Problems</th></tr>${Object.entries(r.verification).map(([id, v]) => `<tr><td>${esc(id)}</td><td>${v.verified ? "✓ matched" : "⚠ flagged"}</td><td class="num">${Math.round(v.score * 100)}%</td><td>${esc(v.problems.join("; "))}</td></tr>`).join("")}</table></div>
    <div class="trace-section"><h3>Safety screen</h3><p>Pages: ${r.document.pages} · Hidden text blocks removed: ${r.document.hidden_text.length} · Suspicious lines removed: ${r.document.removed_lines.length} · Explainer texts rejected by the number check: ${r.explainer_flags.length}</p></div>`;
}

// ------------------------------------------------------------ print: a weekly chart to put on the fridge
function renderPrint() {
  const r = state.run.result;
  const meds = medMap(r);
  const s = r.schedule;
  const start = s.days.findIndex((d) => d.date === state.day);
  const weekDays = s.days.slice(Math.max(0, start), Math.max(0, start) + 7);
  const rows = [];
  const seen = new Set();
  s.days.forEach((d) => d.slots.forEach((sl) => sl.items.forEach((it) => {
    const k = sl.time + "|" + it.med_id;
    if (!seen.has(k)) { seen.add(k); rows.push({ time: sl.time, med: it.med_id }); }
  })));
  rows.sort((a, b) => a.time.localeCompare(b.time) || a.med.localeCompare(b.med));
  const cell = (d, row) => {
    const sl = d.slots.find((x) => x.time === row.time);
    const it = sl && sl.items.find((x) => x.med_id === row.med);
    return it ? `<td class="box">☐ <small>${esc(it.dose)}</small></td>` : `<td class="off"></td>`;
  };
  const activeRows = rows.filter((row) => weekDays.some((d) => d.slots.some((sl) => sl.time === row.time && sl.items.some((it) => it.med_id === row.med))));
  const warn = r.extraction.warnings;
  $("#print-area").innerHTML = `
    <div class="print-page">
      <h2>${esc(L("weekly_chart", "Weekly medicine chart"))}: ${esc(fmtDate(weekDays[0].date, { month: "long", day: "numeric" }))} to ${esc(fmtDate(weekDays[weekDays.length - 1].date, { month: "long", day: "numeric" }))}</h2>
      <table class="chart"><tr><th>Time</th><th>Medicine</th>${weekDays.map((d) => `<th>${esc(fmtDate(d.date, { weekday: "short", month: "numeric", day: "numeric" }))}</th>`).join("")}</tr>
      ${activeRows.map((row) => `<tr><td><strong>${fmtTime(row.time)}</strong></td><td>${esc(meds[row.med].name)} ${esc(meds[row.med].strength || "")}</td>${weekDays.map((d) => cell(d, row)).join("")}</tr>`).join("")}</table>
      <div class="print-row">
        ${s.as_needed.length ? `<div class="print-box"><h3>${esc(L("as_needed", "Only if needed"))}</h3><ul>${s.as_needed.map((id) => `<li>${esc(meds[id].name)}: ${esc(meds[id].dose || "")}${meds[id].max_per_day ? ` (${esc(meds[id].max_per_day)})` : ""}</li>`).join("")}</ul></div>` : ""}
        ${s.stopped.length ? `<div class="print-box red"><h3>${esc(L("stop", "Stop taking these"))}</h3><ul>${s.stopped.map((id) => `<li>${esc(meds[id].name)}</li>`).join("")}</ul></div>` : ""}
        ${warn.length ? `<div class="print-box red"><h3>${esc(L("warnings", "Warning signs"))}</h3><ul>${warn.map((w) => `<li><strong>${w.level === "emergency" ? "911 / ER" : esc(w.phone || w.who_to_call)}</strong>: ${esc((r.explanation.warnings[w.id] || w).signs.slice(0, 4).join("; "))}</li>`).join("")}</ul></div>` : ""}
      </div>
      <p style="font-size:.8rem;margin-top:.4rem">Made with Going Home from the hospital discharge papers. Follow your care team if anything differs.</p>
    </div>`;
}

wireStart();
init();
