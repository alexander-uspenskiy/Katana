"use strict";
/* Katana plan graph: draws the plan (phases left to right, feedback loops underneath, artifacts on every step)
   and, in edit mode, changes it. index.html calls pgView() from renderPlan() and the app's globals (ui, esc, icon,
   flat, isGate, gated, post, ...) are used at call time. The editor works on ui.pg.draft and saves the whole plan
   (pipeline, artifacts, custom agents) with one POST to /api/settings; /api/validate checks every change. */

const PG = { W: 216, H: 142, GH: 142, GX: 88, GY: 18, PAD: 18, TOP: 16, SW: 118, SH: 52, EW: 92, ADD: 26, STEP: 28 };
const VERDICT_HINT = { reviewer: "APPROVE", security: "APPROVE", tester: "PASS", architect: "READY", ba: "READY", developer: "DONE", general: "DONE" };
const pgTab = { step: "Step", artifacts: "Artifacts", agents: "Agents" };
/* ---------- role icons, and the model + effort every step shows ---------- */
const PG_ROLES = {  // agent → [icon paths (16×16, stroked), colour]
  ba: ['<path d="M5.5 3H4a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1h-1.5"/><rect x="5.5" y="1.8" width="5" height="2.6" rx=".8"/><path d="M5.5 8h5M5.5 10.8h3"/>', "#8a5cd6"],
  architect: ['<path d="M8 2l6 3-6 3-6-3z"/><path d="M2 8l6 3 6-3M2 11l6 3 6-3"/>', "#14a3a0"],
  security: ['<path d="M8 1.8l5 1.8v4c0 3-2.2 5-5 6.4C5.2 12.6 3 10.6 3 7.6v-4z"/><path d="M5.8 8l1.6 1.6 3-3.2"/>', "#d03b3b"],
  developer: ['<path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5M9 3l-2 10"/>', "#2a78d6"],
  tester: ['<path d="M6 2h4M6.8 2v4.2L3 12.6a1 1 0 0 0 .9 1.4h8.2a1 1 0 0 0 .9-1.4L9.2 6.2V2"/><path d="M4.8 10h6.4"/>', "#1baf7a"],
  reviewer: ['<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2L14 14M5.2 7l1.4 1.4L9 5.8"/>', "#d98a00"],
  orchestrator: ['<circle cx="8" cy="8" r="1.8"/><circle cx="3" cy="3.5" r="1.3"/><circle cx="13" cy="3.5" r="1.3"/><circle cx="8" cy="13.2" r="1.3"/><path d="M6.7 6.9L4 4.6M9.3 6.9L12 4.6M8 9.8v2.1"/>', "#8a5cd6"],
  general: ['<path d="M2.5 6h11v7.5h-11z"/><path d="M5.8 6V4.3c0-.5.4-.8.8-.8h2.8c.4 0 .8.3.8.8V6M2.5 9.2h11M7.2 9.2v1.4h1.6V9.2"/>', "#c2562d"],
  custom: ['<path d="M8 2l1.4 4.6L14 8l-4.6 1.4L8 14l-1.4-4.6L2 8l4.6-1.4z"/>', "#e0568a"],
  gate: ['<rect x="2.5" y="2.5" width="11" height="11" rx="2"/><path d="M5.5 8.2l1.7 1.7 3.3-3.6"/>', "var(--ink-2)"],
};
const pgRole = (agent) => (!agent ? "gate" : PG_ROLES[agent] ? agent : "custom");
function pgBadge(agent, size = 28, style = "") {  // the coloured icon tile that tells step kinds apart at a glance
  const k = pgRole(agent), [paths, colour] = PG_ROLES[k], i = Math.round(size * 0.62);
  return `<span class="mono-badge rb" style="--rc:${colour};width:${size}px;height:${size}px;border-radius:${size > 24 ? 8 : 6}px;${style}" aria-hidden="true"><svg width="${i}" height="${i}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${paths}</svg></span>`;
}
const PG_MODELS = { fable: "Fable 5.1", opus: "Opus 5.5", sonnet: "Sonnet 5.5", haiku: "Haiku 5.5" };  // alias → the current model it points at
const pgModel = (m) => (/^claude-/.test(m || "") ? modelName(m) : PG_MODELS[m] || modelName(m));
const pgWrites = (a) => a.writes_code  // what a step produces: changes to the code, or only reports (documents)
  ? `<span class="chip wk code" title="Changes the project's code"><svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5"/></svg>changes code</span>`
  : `<span class="chip wk rep" title="Only creates or updates reports and documents; the code is not touched"><svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2h5.5L12.5 5v9H4z"/><path d="M6.5 8h4M6.5 10.8h4"/></svg>writes reports</span>`;
const pgRunWith = (a) => `<span class="chip" title="Model">${esc(pgModel(a.model))}</span>${effortMeter(a.effort)}`;
const pgSlug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30);

/* ---------- data the graph draws from ---------- */
function pgAgents() {
  const out = {};
  ((ui.data && ui.data.agents) || []).forEach((a) => (out[a.name] = a));
  if (ui.cfg) Object.entries(ui.cfg.catalog).forEach(([n, a]) => (out[n] = Object.assign({}, out[n], a, { name: n })));
  const d = ui.pg && ui.pg.draft;
  if (!d) return out;
  Object.keys(out).forEach((n) => { if (out[n].custom && !d.custom_agents[n]) delete out[n]; });
  Object.entries(d.custom_agents).forEach(([n, a]) => {
    const c = d.agents[n] || {};
    out[n] = { name: n, title: a.title, role: a.role, prompt: a.prompt, tools: a.tools, writes_code: !!a.writes_code, custom: true, model: c.model || "sonnet", effort: c.effort || "medium" };
  });
  Object.entries(d.agents).forEach(([n, c]) => { if (out[n] && !out[n].custom) out[n] = Object.assign({}, out[n], { model: c.model || out[n].model, effort: c.effort || out[n].effort, tools: c.tools || out[n].tools, enabled: c.enabled }); });
  return out;
}
function pgTable() {
  const out = {};
  const src = ui.pg ? [].concat(((ui.cfg && ui.cfg.artifact_table) || []).filter((a) => a.builtin), ui.pg.draft.artifacts) : ((ui.data && ui.data.artifacts) || []);
  src.forEach((a) => (out[a.id] = a));
  return out;
}
const pgProduces = (s, table, agents) => [...new Set([].concat(s.outputs || [],
  Object.values(table).filter((a) => a.path && a.path === s.output).map((a) => a.id),
  !isGate(s) && agents[s.agent] && agents[s.agent].writes_code ? ["code"] : []))];
const pgReads = (s) => s.inputs || [];
function pgReadable(phases, s) {  // what a step can read: the artifacts of earlier phases (`before`), plus (`loop`) what the phases
  // from a feedback loop's target up to its gateway wrote on a previous pass, when that loop covers the step's phase
  const table = pgTable(), agents = pgAgents(), stages = flat(phases);
  const at = (id) => phases.findIndex((ph) => ph.some((o) => o.id === id));
  const made = (ph) => ph.filter((o) => !(isGate(s) && o === s)).flatMap((o) => pgProduces(o, table, agents));
  const i = at(s.id), before = new Set(["requirements"]), loop = new Set();
  phases.slice(0, i).forEach((ph) => made(ph).forEach((a) => before.add(a)));
  phases.forEach((ph, g) => ph.filter(gated).forEach((gate) => {
    const t = at(pgBackOf(stages, gate));
    if (t < 0 || t > i || g < i) return;
    phases.slice(t, g + 1).forEach((p) => made(p).forEach((a) => { if (!before.has(a)) loop.add(a); }));
  }));
  return { before, loop };
}
function pgBackOf(stages, s) {  // where a failed gateway sends the work: its 'on fail', else the nearest earlier build stage
  if (s.on_fail) return s.on_fail;
  const i = stages.indexOf(s), build = stages.slice(0, i).reverse().find((o) => o.agent === "developer");
  return build ? build.id : i > 0 ? stages[i - 1].id : null;
}
const pgGateway = (s) => (gated(s) ? (s.gateway === "manual" ? "manual" : "autonomous") : null);

/* ---------- layout + drawing ---------- */
function pgLayout(phases, o) {
  const heights = phases.map((ph) => ph.reduce((a, s) => a + (isGate(s) ? PG.GH : PG.H) + PG.GY, -PG.GY));
  const tallest = Math.max(PG.H, ...heights);
  const bodyH = tallest + (o.editable ? PG.ADD + PG.GY : 0);
  const pos = {}, cols = [];
  const start = { x: PG.PAD, y: PG.TOP + (tallest - PG.SH) / 2, w: PG.SW, h: PG.SH };
  let x = start.x + PG.SW + PG.GX;
  phases.forEach((ph, i) => {
    const top = PG.TOP + (tallest - heights[i]) / 2;
    let y = top;
    ph.forEach((s, k) => {
      const h = isGate(s) ? PG.GH : PG.H;
      pos[s.id] = { x, y, w: PG.W, h, i, k, cy: y + h / 2, r: x + PG.W, b: y + h };
      y += h + PG.GY;
    });
    cols.push({ x, i, top, bottom: y - PG.GY, gate: ph.every(isGate) });
    x += PG.W + PG.GX;
  });
  const end = o.mode === "deterministic" ? { x, y: start.y, w: PG.EW, h: PG.SH } : null;
  return { pos, cols, start, end, tallest, bodyH, width: (end ? end.x + end.w : x - PG.GX) + PG.PAD };
}
function pgRound(points, r = 9) {  // a polyline with rounded corners, as an SVG path
  const pts = points.filter((p, i) => !i || p[0] !== points[i - 1][0] || p[1] !== points[i - 1][1]);
  let d = `M${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1], [cx, cy] = pts[i], [nx, ny] = pts[i + 1];
    const l1 = Math.hypot(cx - px, cy - py), l2 = Math.hypot(nx - cx, ny - cy), rr = Math.min(r, l1 / 2, l2 / 2);
    d += ` L${cx + ((px - cx) / l1) * rr} ${cy + ((py - cy) / l1) * rr} Q${cx} ${cy} ${cx + ((nx - cx) / l2) * rr} ${cy + ((ny - cy) / l2) * rr}`;
  }
  return d + ` L${pts[pts.length - 1][0]} ${pts[pts.length - 1][1]}`;
}
function pgChips(ids, table, flow, max = 3) {
  if (!ids.length) return `<span class="ach none">none</span>`;
  const chip = (id) => `<span class="ach ${flow === id ? "on" : ""}" data-pg-art="${esc(id)}" role="button" tabindex="0" title="${esc((table[id] || {}).title || id)}${table[id] && table[id].path ? " · " + esc(table[id].path) : ""}">${esc(id)}</span>`;
  const shown = ids.slice(0, max);
  if (flow && ids.includes(flow) && !shown.includes(flow)) shown[max - 1] = flow;
  return shown.map(chip).join("") + (ids.length > shown.length ? `<span class="ach none" title="${esc(ids.filter((i) => !shown.includes(i)).join(", "))}">+${ids.length - shown.length}</span>` : "");
}

/* Everything in o: phases, mode, states (run progress by stage id), run, flow (artifact id), sel, connect, editable,
   issues (stage id → {errors, warnings}), menu. Returns the HTML of the scrolling canvas. */
function pgGraph(o) {
  const phases = o.phases, stages = flat(phases), L = pgLayout(phases, o), pos = L.pos, posn = stagePos(phases);
  const agents = pgAgents(), table = pgTable(), st = o.states || {}, run = o.run;
  const title = (id) => (stages.find((s) => s.id === id) || {}).title || id;
  const approval = run && run.status === "approval" && run.approval ? run.approval.stage : null;

  const loops = stages.filter(gated).map((s) => ({ s, t: pgBackOf(stages, s) })).filter((l) => l.t && pos[l.t])
    .sort((a, b) => (pos[a.s.id].i - pos[a.t].i) - (pos[b.s.id].i - pos[b.t].i) || pos[a.s.id].i - pos[b.s.id].i);
  const out = {}, into = {};
  const yTop = PG.TOP + L.bodyH + 16, height = yTop + (loops.length ? loops.length * PG.STEP + 6 : 0) + 8;
  const svg = [], pills = [], nodes = [], adds = [];

  // sequence edges, with a shared vertical bus in each gap so parallel stages fan out and back in
  const layers = [[{ r: L.start.x + L.start.w, cy: L.start.y + L.start.h / 2 }], ...phases.map((ph) => ph.map((s) => ({ x: pos[s.id].x, r: pos[s.id].r, cy: pos[s.id].cy })))];
  if (L.end) layers.push([{ x: L.end.x, cy: L.end.y + L.end.h / 2 }]);
  for (let g = 0; g + 1 < layers.length; g++) {
    layers[g].forEach((a) => layers[g + 1].forEach((b) => {
      const mid = a.r + PG.GX / 2, pts = a.cy === b.cy ? [[a.r, a.cy], [b.x, b.cy]] : [[a.r, a.cy], [mid, a.cy], [mid, b.cy], [b.x, b.cy]];
      svg.push(`<path class="pg-edge ${o.mode === "auto" ? "hint" : ""}" d="${pgRound(pts)}" marker-end="url(#pgA)"/>`);
    }));
  }

  // feedback loops: leave the right side, run down a gap, along the bottom, up the gap before the target, and back in
  loops.forEach((l, level) => {
    const a = pos[l.s.id], b = pos[l.t];
    const lo = (out[a.i] = (out[a.i] || 0) + 1) - 1, li = (into[b.i] = (into[b.i] || 0) + 1) - 1;
    const xo = a.r + 12 + lo * 6, xi = b.x - 12 - li * 6, yL = yTop + level * PG.STEP, sy = a.cy + 12, ty = b.cy + 12 + li * 5;
    const bad = b.i > a.i || (l.t === l.s.id && isGate(l.s)), mode = pgGateway(l.s), used = run ? (run.loops || {})[l.s.id] || 0 : 0;
    const max = l.s.max_loops || o.maxIter, sel = o.sel === l.s.id;
    svg.push(`<path class="pg-loopline ${mode} ${bad ? "bad" : ""} ${used ? "used" : ""} ${sel ? "sel" : ""}" d="${pgRound([[a.r, sy], [xo, sy], [xo, yL], [xi, yL], [xi, ty], [b.x, ty]])}" marker-end="url(#${bad ? "pgB" : "pgL"})"/>`);
    const label = mode === "manual" ? `${icon("warn", 12)}<span>you decide · back to ${esc(posn[l.t])}. ${esc(title(l.t))}</span>` : `${icon("loop", 12)}<span>else back to ${esc(posn[l.t])}. ${esc(title(l.t))}</span>`;
    pills.push(`<button class="loop-pill ${mode} ${bad ? "bad" : ""} ${used ? "used" : ""} ${sel ? "sel" : ""}" data-pg-loop="${esc(l.s.id)}" style="left:${(xo + xi) / 2}px;top:${yL}px" ${tipAttr(`${l.s.title} ${mode === "manual" ? "asks you what to do when it fails" : "sends the work back by itself when it fails"}\n→ ${title(l.t)}${bad ? "\nThis loop goes forward: it must go back." : ""}\n${run ? `${used} of ${max} loops used` : `up to ${max} loops${l.s.max_loops ? "" : " (default)"}`}`)}>${label}<b class="num" style="font-weight:600">${run ? `${used}/${max}` : `${max}×`}</b></button>`);
  });

  // nodes
  const flowOf = (s) => {
    if (!o.flow) return "";
    const make = pgProduces(s, table, agents).includes(o.flow), read = pgReads(s).includes(o.flow);
    return make ? "produces" : read ? "reads" : "dim";
  };
  const flag = (s) => {
    const i = (o.issues || {})[s.id];
    return i && (i.errors.length || i.warnings.length) ? `<span class="gn-flag ${i.errors.length ? "" : "warn"}" ${tipAttr([...i.errors, ...i.warnings].join("\n"))}>${i.errors.length ? "!" : "▲"}</span>` : "";
  };
  const klass = (s) => {
    const x = st[s.id] || { state: "pending" }, state = approval === s.id ? "approval" : x.state;
    const target = o.connect && pgCanTarget(o, s);
    return [state, o.sel === s.id ? "sel" : "", flowOf(s), target ? "can-target" : "", gated(s) ? "gated" : "", pgGateway(s) === "manual" ? "manual" : ""].filter(Boolean).join(" ");
  };
  stages.forEach((s) => {
    const p = pos[s.id], a = agents[s.agent] || { title: s.agent || "Katana" }, x = st[s.id] || { state: "pending", runs: 0 };
    const box = `left:${p.x}px;top:${p.y}px;width:${p.w}px;height:${p.h}px`;
    const tip = `${posn[s.id]}. ${s.title}\n${isGate(s) ? "Gateway measured by Katana, no LLM call" : s.task}\n${approval === s.id ? "Waiting for you" : (x.state === "pending" ? "" : STATE_LABEL[x.state] || x.state) + (x.runs ? " · " + x.runs + " runs" : "")}`.trim();
    if (isGate(s)) {
      const mode = pgGateway(s), n = (s.conditions || []).length;
      nodes.push(`<div class="gn gn-stage gn-gate ${klass(s)}" data-pg-node="${esc(s.id)}" role="button" tabindex="0" style="${box}" ${tipAttr(tip)}><span class="gn-tag" aria-hidden="true">Gateway</span>
        <div class="gn-top">${pgBadge(null)}<div class="gn-t"><b>${esc(s.title)}</b><span class="muted">Gateway</span></div><span class="gn-pos">${esc(posn[s.id])}</span></div>
        <div class="gn-model"><span class="chip" title="Katana measures gateways itself">Katana</span><span class="chip" title="No LLM call, so no model or effort">no model</span></div>
        <div class="gn-io"><div class="io-row" title="Reads"><i>↓</i>${pgChips(pgReads(s), table, o.flow)}</div><div class="io-row" title="Writes"><i>↑</i>${pgChips(pgProduces(s, table, agents), table, o.flow)}</div></div>
        <div class="gn-meta"><span class="chip">${n} criteri${n === 1 ? "on" : "a"}</span><span class="chip ${mode === "manual" ? "accent" : ""}">${icon("gate", 11)}${mode === "manual" ? "manual" : "auto"}</span>${s.required ? `<span class="chip">required</span>` : ""}</div>${flag(s)}</div>`);
      return;
    }
    const mode = pgGateway(s);
    nodes.push(`<div class="gn gn-stage ${a.writes_code ? "wcode" : "wrep"} ${klass(s)}" data-pg-node="${esc(s.id)}" role="button" tabindex="0" style="${box}" ${tipAttr(tip)}>${mode ? `<span class="gn-tag" aria-hidden="true">Gateway</span>` : ""}
      <div class="gn-top">${pgBadge(s.agent)}<div class="gn-t"><b>${esc(s.title)}</b><span class="muted">${esc(a.title)}</span></div><span class="gn-pos">${esc(posn[s.id])}</span></div>
      <div class="gn-model">${pgRunWith(a)}</div>
      <div class="gn-io"><div class="io-row" title="Reads"><i>↓</i>${pgChips(pgReads(s), table, o.flow)}</div><div class="io-row" title="Writes"><i>↑</i>${pgChips(pgProduces(s, table, agents), table, o.flow)}</div></div>
      <div class="gn-meta">${pgWrites(a)}${mode ? `<span class="chip ${mode === "manual" ? "accent" : ""}" title="Passes on ${esc(s.pass.join(", "))}">${icon("gate", 11)}${mode === "manual" ? "manual" : "auto"}</span>` : ""}${a.custom ? `<span class="chip">custom</span>` : ""}</div>${flag(s)}</div>`);
  });
  const sel0 = o.sel === "$start";
  nodes.push(`<div class="gn-start ${sel0 ? "sel" : ""}" data-pg-node="$start" role="button" tabindex="0" style="left:${L.start.x}px;top:${L.start.y}px;width:${L.start.w}px;height:${L.start.h}px"><div style="display:grid;gap:3px;justify-items:center">Requirement<span class="ach ${o.flow === "requirements" ? "on" : ""}" data-pg-art="requirements" role="button" tabindex="0" title="The requirement Katana took from the trigger · docs/00-requirements.md">requirements</span></div></div>`);
  if (L.end) {
    const k = run ? ({ done: "done", failed: "failed", blocked: "failed" }[run.status] || "") : "";
    nodes.push(`<div class="gn-end ${k}" style="left:${L.end.x}px;top:${L.end.y}px;width:${L.end.w}px;height:${L.end.h}px">${run && ["failed", "blocked"].includes(run.status) ? icon("x", 13) + STATUS[run.status].label : icon("check", 13) + "Done"}</div>`);
  }

  // edit controls: insert points in every gap, and a parallel-step button under each column
  if (o.editable) {
    const mid = layers.map((ph) => ph[0].r + PG.GX / 2), cy = PG.TOP + L.tallest / 2;
    for (let g = 0; g <= phases.length; g++) {
      adds.push(`<button class="gap-add" data-pg-gap="${g}" style="left:${mid[g]}px;top:${cy}px" title="Insert a step or gateway here" aria-label="Insert a step or gateway before phase ${g + 1}">+</button>`);
    }
    L.cols.forEach((c) => { if (!c.gate) adds.push(`<button class="col-add" data-pg-col="${c.i}" style="left:${c.x}px;top:${c.bottom + PG.GY}px;width:${PG.W}px">+ step in parallel</button>`); });
    if (o.menu) adds.push(pgMenu(o.menu, mid, cy, L));
  }
  const arrow = (id, cls) => `<marker id="${id}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0L9 4.5L0 9z" class="pg-arrow ${cls}"/></marker>`;
  return `<div class="pg-stage" style="width:${L.width}px;height:${height}px"><svg class="pg-svg" width="${L.width}" height="${height}" aria-hidden="true"><defs>${arrow("pgA", "")}${arrow("pgL", "loop")}${arrow("pgB", "bad")}</defs>${svg.join("")}</svg>${nodes.join("")}${pills.join("")}${adds.join("")}</div>`;
}
function pgCanTarget(o, s) {  // while picking a loop target on the graph: is `s` a valid place to go back to?
  const stages = flat(o.phases), from = stages.find((x) => x.id === o.connect), pos = stagePos(o.phases);
  if (!from) return false;
  const idx = (x) => parseInt(pos[x.id], 10);
  return idx(s) <= idx(from) && !(s.id === from.id && isGate(from));
}
function pgMenuItems(kind) {  // kind: step (agents), gateway, or both
  const agents = Object.values(pgAgents()).filter((a) => a.name !== ui.cfg.options.router), std = agents.filter((a) => !a.custom), mine = agents.filter((a) => a.custom);
  const row = (a) => `<button data-pg-add="agent" data-agent="${esc(a.name)}">${pgBadge(a.name, 22)}${esc(a.title)}<small class="muted" style="margin-left:auto">${esc(pgModel(a.model))} · ${esc(a.effort)}</small></button>`;
  const steps = `<div class="h">Standard agents</div>${std.map(row).join("")}${mine.length ? `<div class="h">Your agents</div>${mine.map(row).join("")}` : ""}
    <button data-pg-add="new-agent">＋ New agent…</button>`;
  const gateways = `<div class="h">Gateways</div>
    <button data-pg-add="gateway" data-mode="autonomous">${icon("gate", 14)}<span>Autonomous gateway<br><small class="muted">sends failed work back by itself</small></span></button>
    <button data-pg-add="gateway" data-mode="manual">${icon("warn", 14)}<span>Manual gateway<br><small class="muted">stops and waits for you if criteria fail</small></span></button>`;
  return kind === "step" ? steps : kind === "gateway" ? gateways : `${steps}<hr>${gateways}`;
}
function pgMenu(m, mid, cy, L) {
  const insert = m.at.insert !== undefined;
  const x = insert ? mid[m.at.insert] + 16 : L.cols[m.at.phase].x, y = insert ? cy + 4 : L.cols[m.at.phase].bottom + PG.GY + 30;
  return `<div class="pg-menu at" data-pg-menu style="left:${x}px;top:${y}px">${pgMenuItems(insert ? "both" : "step")}</div>`;
}

/* ---------- view mode (the Plan tab as it was, now as a graph) ---------- */
function pgView(d, chosen, st, mode, phases) {
  return `<div class="pg"><div class="pg-bar"><span class="muted" style="font-size:12.5px">${mode === "auto" ? "Stage catalogue: the phases are a hint, not a rule" : "Phases run left to right; stages in a phase run at the same time"}</span>
    <span class="sp"></span>${chosen ? `<span class="muted" style="font-size:12.5px">A run’s plan is frozen. Choose <b>Current plan</b> to edit it.</span>` : ""}
    <button class="btn primary" data-pg="open" ${chosen ? "disabled" : ""}>Edit plan</button></div>
    ${ui.pgFlow ? pgFlowBar(ui.pgFlow, phases) : ""}
    <div class="pg-main"><div class="pg-canvas" id="pgViewCanvas">${pgGraph({ phases, mode, states: st, run: chosen, flow: ui.pgFlow, maxIter: d.settings.max_iterations })}</div></div></div>`;
}
function pgFlowBar(id, phases) {
  const table = pgTable(), agents = pgAgents(), a = table[id] || { title: id }, stages = flat(phases);
  const w = stages.filter((s) => pgProduces(s, table, agents).includes(id)).map((s) => s.title), r = stages.filter((s) => pgReads(s).includes(id)).map((s) => s.title);
  return `<div class="pg-flowbar">${icon("info", 14)}<b>${esc(a.title)}</b>${a.path ? `<span class="mono">${esc(a.path)}</span>` : ""}
    <span>written by <b>${w.length ? esc(w.join(", ")) : "nobody yet"}</b> (green)</span><span>read by <b>${r.length ? esc(r.join(", ")) : "nobody"}</b> (blue)</span>
    <button class="btn" data-pg="flow-off" style="margin-left:auto">Clear</button></div>`;
}

/* ---------- edit mode ---------- */
const pgPick = (s) => ({ pipeline: phasesOf(s.pipeline), artifacts: s.artifacts || [], custom_agents: s.custom_agents || {}, agents: s.agents || {} });
const pgDirty = () => ui.pg && JSON.stringify(pgPick(ui.pg.draft)) !== ui.pg.base;
const pgBody = () => Object.assign(clone(ui.cfg.settings), clone(pgPick(ui.pg.draft)));
const pgFind = (id) => flat(ui.pg.draft.pipeline).find((s) => s.id === id);
const pgPhaseOf = (id) => ui.pg.draft.pipeline.findIndex((ph) => ph.some((s) => s.id === id));
function pgOpen() {
  if (!ui.cfg) { toast("Settings are still loading. Try again in a moment."); return; }
  ui.planRun = "none"; ui.pgFlow = null;
  const s = ui.cfg.settings;
  ui.pg = { draft: clone(pgPick(s)), base: JSON.stringify(pgPick(s)), sel: null, tab: "step", undo: [], redo: [], issues: { errors: [], warnings: [] },
    flow: null, connect: null, menu: null, form: null, saving: false, seq: 0 };
  pgCheck(); render();
}
async function pgClose(force) {
  if (!force && pgDirty() && !confirm("Discard the changes you made to the plan?")) return;
  ui.pg = null; render();
}
function pgCommit(fn, key) {
  const pg = ui.pg, now = Date.now();
  if (!(key && pg.lastKey === key && now - pg.lastAt < 1500)) { pg.undo.push(JSON.stringify(pg.draft)); if (pg.undo.length > 60) pg.undo.shift(); }
  pg.redo = []; pg.lastKey = key || null; pg.lastAt = now;
  fn(pg.draft);
  pg.draft.pipeline = pg.draft.pipeline.filter((ph) => ph.length);
  if (pg.sel && pg.sel !== "$start" && !pgFind(pg.sel)) pg.sel = null;
  pgCheck();
}
function pgStep(from, to) {
  const pg = ui.pg, list = pg[from];
  if (!list.length) return;
  pg[to].push(JSON.stringify(pg.draft)); pg.draft = JSON.parse(list.pop()); pg.lastKey = null; pg.menu = null;
  if (pg.sel && pg.sel !== "$start" && !pgFind(pg.sel)) pg.sel = null;
  pgCheck(); pgPaint();
}
let pgTimer = null;
function pgCheck() {  // asks the server what is wrong with the draft, once the user pauses
  clearTimeout(pgTimer);
  pgTimer = setTimeout(async () => {
    const pg = ui.pg; if (!pg) return;
    const n = ++pg.seq, out = await post("/api/validate", pgBody());
    if (!ui.pg || n !== ui.pg.seq || !out.ok) return;
    ui.pg.issues = { errors: out.errors, warnings: out.warnings };
    pgPaint(["bar", "checks", "canvas"]);
  }, 300);
}
function pgIssuesBy() {
  const by = {};
  ["errors", "warnings"].forEach((k) => ui.pg.issues[k].forEach((i) => { if (i.stage) (by[i.stage] = by[i.stage] || { errors: [], warnings: [] })[k].push(i.message); }));
  return by;
}
function pgOptions() {
  const pg = ui.pg;
  return { phases: pg.draft.pipeline, mode: ui.cfg.settings.mode, editable: true, sel: pg.sel, connect: pg.connect, flow: pg.flow, issues: pgIssuesBy(), maxIter: ui.cfg.settings.max_iterations, menu: pg.menu };
}
function pgPaint(parts) {
  const pg = ui.pg, host = $("planHead");
  if (!pg) return;
  if (!document.querySelector("#planHead .pg.editing") || !parts) {
    host.innerHTML = `<div class="pg editing"><div class="pg-bar" id="pgBar"></div><div id="pgChecks"></div><div id="pgFlow"></div><div class="pg-main"><div class="pg-canvas" id="pgCanvas"></div><aside class="pg-side" aria-label="Inspector"><div class="pg-tabs" id="pgTabs" role="tablist"></div><div class="pg-pane" id="pgSide"></div></aside></div></div>`;
    parts = ["bar", "checks", "canvas", "side"];
  }
  if (parts.includes("bar")) $("pgBar").innerHTML = pgBar();
  if (parts.includes("checks")) $("pgChecks").innerHTML = pgChecks();
  if (parts.includes("canvas")) {
    const c = $("pgCanvas"), l = c.scrollLeft, t = c.scrollTop;
    c.innerHTML = (pg.connect ? `<div class="pg-hint">${icon("loop", 14)}Click the step the work should go back to <button class="btn" data-pg="connect-off">Cancel</button></div>` : "") + pgGraph(pgOptions());
    c.scrollLeft = l; c.scrollTop = t;
    $("pgFlow").innerHTML = pg.flow ? pgFlowBar(pg.flow, pg.draft.pipeline) : "";
  }
  if (parts.includes("side")) {
    $("pgTabs").innerHTML = Object.entries(pgTab).map(([k, l]) => `<button role="tab" aria-selected="${pg.tab === k}" data-pg-tab="${k}">${l}</button>`).join("");
    $("pgSide").innerHTML = pg.tab === "artifacts" ? pgArtifactsPane() : pg.tab === "agents" ? pgAgentsPane() : pgInspector();
  }
}
function pgBar() {
  const pg = ui.pg, dirty = pgDirty(), errs = pg.issues.errors.length;
  const menu = (label, kind) => `<details class="pg-pop" data-pg-pop><summary class="btn">${label}</summary><div class="pg-menu">${pgMenuItems(kind)}</div></details>`;
  return `${menu("+ Step", "step")}${menu("+ Gateway", "gateway")}
    <span class="sep"></span><button class="btn" data-pg="undo" ${pg.undo.length ? "" : "disabled"} title="Undo (Ctrl/Cmd+Z)">Undo</button><button class="btn" data-pg="redo" ${pg.redo.length ? "" : "disabled"}>Redo</button>
    <span class="msg">${errs ? `${icon("err", 14)} ${errs} problem${errs > 1 ? "s" : ""} to fix` : dirty ? `${icon("info", 14)} Unsaved plan changes` : `${icon("check", 14)} Saved`}</span><span class="sp"></span>
    <button class="btn" data-pg="close">${dirty ? "Discard &amp; close" : "Close"}</button><button class="btn primary" data-pg="save" ${!dirty || errs || pg.saving ? "disabled" : ""}>${pg.saving ? "Saving…" : "Save plan"}</button>`;
}
function pgChecks() {
  const { errors, warnings } = ui.pg.issues, n = errors.length + warnings.length;
  if (!n) return `<div class="pg-checks">${icon("check", 14)} The plan checks out: every feedback loop goes back to an earlier step, and every artifact a step reads is written before it.</div>`;
  const row = (i, k) => `<li>${icon(k === "e" ? "err" : "warn", 14)}${i.stage ? `<button data-pg-select="${esc(i.stage)}">${esc(i.message)}</button>` : `<span>${esc(i.message)}</span>`}</li>`;
  return `<details class="pg-checks ${errors.length ? "bad" : ""}" ${errors.length ? "open" : ""}><summary>${errors.length ? icon("err", 14) : icon("warn", 14)}<b>${errors.length ? `${errors.length} problem${errors.length > 1 ? "s" : ""}` : "No problems"}</b>${warnings.length ? ` · ${warnings.length} warning${warnings.length > 1 ? "s" : ""}` : ""}<span class="muted">· click one to jump to its step</span></summary>
    <ul>${errors.map((i) => row(i, "e")).join("")}${warnings.map((i) => row(i, "w")).join("")}</ul></details>`;
}

/* ---------- inspector ---------- */
const pgInput = (s, f, label, extra = "") => `<label class="field"><span>${label}</span><input type="text" data-pgf="${f}" value="${esc(s[f])}" ${extra}></label>`;
function pgInspector() {
  const pg = ui.pg, id = pg.sel;
  if (id === "$start") return `<h3>Requirement</h3><p class="muted" style="margin:0;font-size:12.5px">Katana writes the requirement it took from the trigger to <span class="mono">docs/00-requirements.md</span> in each run’s workspace. Steps that list <span class="mono">requirements</span> under Reads are told to read it first.</p>`;
  const s = id && pgFind(id);
  if (!s) return `<div class="pg-empty">Select a step or gateway on the graph to edit it.<br><br>Use the <b>+</b> between phases to insert one, or <b>+ Step</b> above.<br><br>Click a <b>feedback loop</b> label to edit where failed work goes back to.</div>`;
  const agents = pgAgents(), table = pgTable(), P = pg.draft.pipeline, i = pgPhaseOf(id), k = P[i].indexOf(s), stages = flat(P), pos = stagePos(P);
  const gate = isGate(s), mode = pgGateway(s), gatedNow = gated(s);
  const agentOpts = (list) => list.map((a) => `<option value="${esc(a.name)}" ${s.agent === a.name ? "selected" : ""}>${esc(a.title)}</option>`).join("");
  const real = Object.values(agents).filter((a) => a.name !== ui.cfg.options.router), std = real.filter((a) => !a.custom), mine = real.filter((a) => a.custom);
  const avail = pgReadable(P, s);
  const note = (key, a) => key !== "inputs" ? "" : avail.before.has(a) ? "" : avail.loop.has(a) ? "\nWritten later in the plan: available once a feedback loop sends work back." : "\nNothing before this step writes it.";
  const picks = (key, ids) => `<div class="picks">${ids.map((a) => `<label class="${key === "inputs" && !avail.before.has(a) ? (avail.loop.has(a) ? "viaLoop" : "unwritten") : ""}" title="${esc(table[a].title)}${table[a].path ? " · " + esc(table[a].path) : ""}${esc(note(key, a))}"><input type="checkbox" data-pgf="${key}" value="${esc(a)}" ${(s[key] || []).includes(a) ? "checked" : ""} ${key === "outputs" && table[a].path && table[a].path === s.output ? "disabled" : ""}>${esc(a)}${key === "inputs" && avail.loop.has(a) && !avail.before.has(a) ? icon("loop", 11) : ""}</label>`).join("")}</div>`;
  const readIds = Object.keys(table).filter((a) => avail.before.has(a) || avail.loop.has(a) || (s.inputs || []).includes(a)), writeIds = Object.keys(table).filter((a) => a !== "requirements");
  const targets = stages.filter((o) => pos[o.id] && parseInt(pos[o.id], 10) <= parseInt(pos[id], 10) && !(gate && o.id === id));
  const loop = gatedNow ? `<div class="sec loopsec"><h3>${icon("loop", 14)} Feedback loop</h3>
      <div class="seg2" role="radiogroup" aria-label="Gateway type">
        <label><input type="radio" name="pgw" data-pgf="gateway" value="autonomous" ${mode === "autonomous" ? "checked" : ""}><b>Autonomous</b><span class="muted">sends failed work back by itself, up to the loop limit</span></label>
        <label><input type="radio" name="pgw" data-pgf="gateway" value="manual" ${mode === "manual" ? "checked" : ""}><b>Manual</b><span class="muted">stops the run and waits for you to approve, send back or reject</span></label></div>
      ${gate ? "" : `<label class="field"><span>Passes on (verdicts)</span><input type="text" class="mono" data-pgf="pass" value="${esc((s.pass || []).join(", "))}" placeholder="APPROVE" spellcheck="false"><small>Any other verdict fails the gateway.</small></label>`}
      <label class="field"><span>If it fails, go back to</span><select data-pgf="on_fail"><option value="">Nearest build step</option>${targets.map((o) => `<option value="${esc(o.id)}" ${s.on_fail === o.id ? "selected" : ""}>${esc(pos[o.id])}. ${esc(o.title)}</option>`).join("")}</select></label>
      <div class="acts"><button class="btn" data-pg="connect">Pick on the graph</button>${gate ? "" : `<button class="btn ghost" data-pg="ungate">Remove gate</button>`}</div>
      <div class="row2"><label class="field"><span>Max loops</span><input type="number" min="1" max="10" data-pgf="max_loops" value="${esc(s.max_loops || "")}" placeholder="${esc(ui.cfg.settings.max_iterations)} (default)"></label>
        <label class="check" style="align-self:end"><input type="checkbox" data-pgf="required" ${s.required ? "checked" : ""}> Required before done</label></div>
      ${gate ? "" : ""}</div>` : `<div class="sec"><label class="check"><input type="checkbox" data-pgf="gated"> Gate this step: it passes only on certain verdicts, otherwise work goes back</label></div>`;
  const conds = gate ? `<div class="sec"><h3>Criteria</h3><small class="muted">Katana measures these itself after the phases before it. A blocking condition that fails fails the gateway; a warning is only reported.</small>
      ${(s.conditions || []).map((c, n) => { const M = ui.cfg.options.metrics[c.metric] || {}; return `<div class="cond">
        <select data-pgc="${n}" data-f="metric" aria-label="Condition ${n + 1} metric">${Object.entries(ui.cfg.options.metrics).map(([m, v]) => `<option value="${m}" ${m === c.metric ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select>
        <select data-pgc="${n}" data-f="op" aria-label="comparison">${ui.cfg.options.ops.map((o) => `<option ${o === c.op ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>
        <input type="number" min="0" step="any" data-pgc="${n}" data-f="value" value="${esc(c.value)}" aria-label="threshold">
        <select data-pgc="${n}" data-f="severity" aria-label="severity">${ui.cfg.options.severities.map((v) => `<option value="${v}" ${v === c.severity ? "selected" : ""}>${v === "block" ? "Block" : "Warn"}</option>`).join("")}</select>
        <button class="btn icon" data-pg="cond-remove" data-n="${n}" aria-label="Remove condition ${n + 1}">${icon("trash")}</button>
        ${M.level ? `<label class="field" style="grid-column:1 / -1"><span>Counts findings at this level or worse</span><select data-pgc="${n}" data-f="level">${ui.cfg.options.levels.map((l) => `<option ${l === c.level ? "selected" : ""}>${l}</option>`).join("")}</select></label>` : ""}</div>`; }).join("")}
      <button class="btn ghost" data-pg="cond-add">+ Add condition</button></div>` : "";
  const placement = `<div class="sec"><h3>Placement</h3>
      <label class="field"><span>Phase</span><select data-pgf="phase" aria-label="Move to a phase">${P.map((ph, j) => `<option value="${j}" ${j === i ? "selected" : ""} ${(gate || ph.every(isGate)) && j !== i ? "disabled" : ""}>Phase ${j + 1}${j === i ? "" : " (run in parallel)"}</option>`).join("")}${P[i].length > 1 ? `<option value="new">Own phase, right after</option>` : ""}</select></label>
      <div class="acts"><button class="btn" data-pg="phase-up" ${i === 0 ? "disabled" : ""}>◀ Earlier</button><button class="btn" data-pg="phase-down" ${i === P.length - 1 ? "disabled" : ""}>Later ▶</button>
        <button class="btn" data-pg="up" ${k === 0 ? "disabled" : ""}>${icon("up", 13)}</button><button class="btn" data-pg="down" ${k === P[i].length - 1 ? "disabled" : ""}>${icon("down", 13)}</button>
        <button class="btn ghost" data-pg="delete" style="margin-left:auto">${icon("trash", 13)} Delete</button></div></div>`;
  return `<div style="display:flex;gap:8px;align-items:center">${pgBadge(gate ? null : s.agent)}<h3>${gate ? "Gateway" : "Step"} ${esc(pos[id])}</h3></div>
    <div class="sec">${pgInput(s, "title", "Title", 'maxlength="60"')}
      <div class="row2">${pgInput(s, "id", "Id", 'class="mono" spellcheck="false"')}${pgInput(s, "output", gate ? "Report for the rework step" : "Report (output)", 'class="mono" spellcheck="false"')}</div>
      ${gate ? "" : `<label class="field"><span>Agent${agents[s.agent] && agents[s.agent].writes_code ? " · changes code" : ""}</span><select data-pgf="agent"><optgroup label="Standard">${agentOpts(std)}</optgroup>${mine.length ? `<optgroup label="Yours">${agentOpts(mine)}</optgroup>` : ""}<option value="__new">＋ New agent…</option></select></label>
      <label class="field"><span>Task</span><textarea data-pgf="task" rows="3">${esc(s.task)}</textarea></label>`}</div>
    ${loop}${conds}
    <div class="sec"><h3>${icon("down", 13)} Reads (input artifacts)</h3>${picks("inputs", readIds)}<small class="muted">${gate ? "The reports and code this gateway measures. " : ""}Only what earlier steps write${[...avail.loop].some((a) => !avail.before.has(a)) ? `, and ${icon("loop", 11)} what a feedback loop brings back from later steps` : ""}.</small>
      <h3 style="margin-top:4px">${icon("up", 13)} Writes (output artifacts)</h3>${picks("outputs", writeIds)}<small class="muted">The report path above is always one of them. <button class="btn ghost" style="height:auto;padding:0" data-pg-tab="artifacts" data-new="artifact">New artifact…</button></small></div>
    ${placement}`;
}

/* ---------- artifacts + agents panes ---------- */
function pgArtifactsPane() {
  const pg = ui.pg, table = pgTable(), agents = pgAgents(), stages = flat(pg.draft.pipeline);
  const form = pg.form && pg.form.kind === "artifact" ? pg.form : null;
  const row = (a) => {
    const w = stages.filter((s) => pgProduces(s, table, agents).includes(a.id)), r = stages.filter((s) => pgReads(s).includes(a.id));
    return `<div class="pg-item ${pg.flow === a.id ? "on" : ""}"><div class="h"><b>${esc(a.title)}</b><span class="chip mono">${esc(a.id)}</span>
      ${a.builtin ? `<span class="chip">built in</span>` : `<button class="btn" data-pg="art-edit" data-id="${esc(a.id)}">Edit</button><button class="btn" data-pg="art-delete" data-id="${esc(a.id)}">Delete</button>`}</div>
      ${a.path ? `<span class="mono muted">${esc(a.path)}</span>` : ""}${a.description ? `<span class="muted">${esc(a.description)}</span>` : ""}
      <span class="muted">written by ${w.length ? esc(w.map((s) => s.title).join(", ")) : "<i>nothing yet</i>"} · read by ${r.length ? esc(r.map((s) => s.title).join(", ")) : "<i>nothing</i>"}</span>
      <div class="acts"><button class="btn" data-pg="flow" data-id="${esc(a.id)}">${pg.flow === a.id ? "Showing on graph" : "Show on graph"}</button></div></div>`;
  };
  return `<div class="acts"><h3 style="margin-right:auto;align-self:center">Artifacts</h3>${form ? "" : `<button class="btn" data-pg="art-new">+ New artifact</button>`}</div>
    <small class="muted">Documents steps read as input and write as output. Each step is told which to read first and which to produce.</small>
    ${form ? pgArtifactForm(form) : ""}<div class="pg-list">${Object.values(table).map(row).join("")}</div>`;
}
function pgArtifactForm(f) {
  return `<div class="pg-form"><b>${f.edit ? "Edit artifact" : "New artifact"}</b>
    <label class="field"><span>Title</span><input type="text" data-pgform="title" value="${esc(f.data.title)}" maxlength="60"></label>
    <div class="row2"><label class="field"><span>Id</span><input type="text" class="mono" data-pgform="id" value="${esc(f.data.id)}" ${f.edit ? "disabled" : ""} spellcheck="false"></label>
      <label class="field"><span>Path</span><input type="text" class="mono" data-pgform="path" value="${esc(f.data.path)}" placeholder="docs/spec.md" spellcheck="false"></label></div>
    <label class="field"><span>What it holds</span><input type="text" data-pgform="description" value="${esc(f.data.description)}" maxlength="200"></label>
    ${f.errors.length ? `<ul class="errors">${f.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    <div class="acts"><button class="btn primary" data-pg="form-save">${f.edit ? "Save artifact" : "Add artifact"}</button><button class="btn" data-pg="form-cancel">Cancel</button></div></div>`;
}
function pgAgentsPane() {
  const pg = ui.pg, agents = pgAgents(), stages = flat(pg.draft.pipeline), form = pg.form && pg.form.kind === "agent" ? pg.form : null;
  const row = (a) => {
    const used = stages.filter((s) => s.agent === a.name).map((s) => s.title);
    return `<div class="pg-item"><div class="h">${pgBadge(a.name, 24)}<b>${esc(a.title)}</b>${a.custom ? `<span class="chip accent">custom</span>` : ""}
      ${a.enabled === false ? `<span class="chip">disabled</span>` : ""}<button class="btn" data-pg="agent-edit" data-id="${esc(a.name)}">Edit</button>${a.custom ? `<button class="btn" data-pg="agent-delete" data-id="${esc(a.name)}" ${used.length ? `disabled title="Used by ${esc(used.join(", "))}"` : ""}>Delete</button>` : ""}</div>
      <span class="muted">${esc(a.role || "")}</span><span class="muted">${esc(pgModel(a.model))} · ${esc(a.effort)} · ${a.writes_code ? "changes code" : "report only"}</span>
      <span class="muted">${used.length ? "Used in " + esc(used.join(", ")) : "Not in the plan"}</span></div>`;
  };
  return `<div class="acts"><h3 style="margin-right:auto;align-self:center">Agents</h3>${form ? "" : `<button class="btn" data-pg="agent-new">+ New agent</button>`}</div>
    <small class="muted">Edit any agent’s model, effort and tools here; standard agents keep their title, role and what they do, and their prompt opens in its own editor. Agents you create here belong to this project.</small>
    ${form ? pgAgentForm(form) : ""}<div class="pg-list">${Object.values(agents).filter((a) => a.name !== ui.cfg.options.router).map(row).join("")}</div>`;
}
const PG_PROMPT = (title, role) => `You are the ${title} of an autonomous software team. ${role || ""}`.trim() + `

Read the documents named in your task, do the work, and write your report to the output file you are given.
Be specific: cite file paths and section names. Do not change any file except your output and the others your task names.

End your output file with exactly one verdict line: \`VERDICT: READY\`, or \`VERDICT: APPROVE\` / \`VERDICT: CHANGES_REQUESTED\` when your step is a gate.`;
function pgAgentForm(f) {
  const o = ui.cfg.options, d = f.data, std = !!d.std;  // a standard agent keeps its title, role and kind; the rest is editable
  return `<div class="pg-form"><b>${std ? `Edit ${esc(d.title)}` : f.edit ? "Edit agent" : "New agent"}</b>
    <div class="row2"><label class="field"><span>Title</span><input type="text" data-pgform="title" value="${esc(d.title)}" maxlength="40" placeholder="Product Owner" ${std ? "disabled" : ""}></label>
      <label class="field"><span>Name</span><input type="text" class="mono" data-pgform="name" value="${esc(d.name)}" ${f.edit ? "disabled" : ""} spellcheck="false"></label></div>
    <label class="field"><span>Role (one line)</span><input type="text" data-pgform="role" value="${esc(d.role)}" maxlength="200" placeholder="Prioritises the backlog and writes user stories" ${std ? "disabled" : ""}></label>
    <div class="row2"><label class="field"><span>Model</span><select data-pgform="model">${o.models.map((m) => `<option value="${m}" ${m === d.model ? "selected" : ""}>${cap1(m)}</option>`).join("")}</select></label>
      <label class="field"><span>Effort</span><select data-pgform="effort">${o.efforts.map((e) => `<option ${e === d.effort ? "selected" : ""}>${e}</option>`).join("")}</select></label></div>
    <label class="check"><input type="checkbox" data-pgform="writes_code" ${d.writes_code ? "checked" : ""} ${std ? "disabled" : ""}> Changes code (at most one such step per phase)</label>
    ${std ? `<label class="check"><input type="checkbox" data-pgform="enabled" ${d.enabled ? "checked" : ""}> Enabled (a disabled agent can’t run, so it can’t be in the plan)</label>` : ""}
    <label class="field"><span>Tool allowlist</span><textarea data-pgform="tools" rows="${std ? 4 : 2}" class="mono" spellcheck="false">${esc(d.tools)}</textarea><small>Comma-separated. Shells, network and system commands are refused.</small></label>
    ${std ? `<div class="acts"><button class="btn" data-pg="agent-prompt" data-id="${esc(d.name)}">${icon("text", 13)} Edit prompt…</button><small class="muted">Saved at once, for all projects.</small></div>`
      : `<label class="field"><span>Prompt</span><textarea data-pgform="prompt" rows="9" spellcheck="false">${esc(d.prompt)}</textarea></label>`}
    ${f.errors.length ? `<ul class="errors">${f.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    <div class="acts"><button class="btn primary" data-pg="form-save">${f.edit ? "Save agent" : f.then ? "Add agent and step" : "Add agent"}</button><button class="btn" data-pg="form-cancel">Cancel</button></div></div>`;
}

/* ---------- plan operations ---------- */
function pgUnique(base, taken) { let id = base || "step", n = 2; while (taken.includes(id)) id = `${base}-${n++}`; return id; }
function pgArtifactAt(d, path, title) {  // the artifact for a document path, created if needed
  const hit = [].concat(((ui.cfg && ui.cfg.artifact_table) || []).filter((a) => a.builtin), d.artifacts).find((a) => a.path === path);
  if (hit) return hit.id;
  const ids = [].concat(((ui.cfg && ui.cfg.artifact_table) || []).map((a) => a.id), d.artifacts.map((a) => a.id));
  const id = pgUnique(pgSlug(path.split("/").pop().replace(/\.md$/, "")) || "doc", ids);
  d.artifacts.push({ id, title: String(title).slice(0, 60), path, description: "" });
  return id;
}
function pgFreePath(d, path) {  // a report path no step writes yet, so two steps never overwrite each other's report
  const taken = flat(d.pipeline).map((s) => s.output);
  let out = path, n = 2;
  while (taken.includes(out)) out = path.replace(/\.md$/, `-${n++}.md`);
  return out;
}
function pgDefaultReads(d, g) {  // what a step inserted before phase g reads: the previous phase's outputs
  if (g <= 0) return ["requirements"];
  const table = pgTable(), agents = pgAgents(), prev = d.pipeline[g - 1] || [];
  return [...new Set(prev.flatMap((s) => pgProduces(s, table, agents)))].filter((a) => a !== "requirements").slice(0, 4);
}
function pgNewStage(d, agent, g) {
  const a = pgAgents()[agent] || {}, ids = flat(d.pipeline).map((s) => s.id), id = pgUnique(pgSlug(agent), ids);
  const path = pgFreePath(d, `docs/${id}.md`);
  return { id, title: a.title || agent, agent, output: path, task: a.role || "", inputs: pgDefaultReads(d, g), outputs: [pgArtifactAt(d, path, a.title || id)] };
}
function pgNewGate(d, mode, g) {
  const ids = flat(d.pipeline).map((s) => s.id), id = pgUnique("gate", ids), earlier = flat(d.pipeline.slice(0, g));
  const build = [...earlier].reverse().find((s) => s.agent === "developer") || earlier[earlier.length - 1];
  const path = pgFreePath(d, `docs/${id}-report.md`);
  return { id, title: mode === "manual" ? "Approval gate" : "Quality gate", kind: "gate", output: path, inputs: pgDefaultReads(d, g), outputs: [pgArtifactAt(d, path, "Gate report")],
    on_fail: build ? build.id : null, required: false, gateway: mode, conditions: [defaultCondition("test_pass_rate")] };
}
function pgAdd(kind, t) {
  const pg = ui.pg, at = pg.menuAt || { insert: pg.draft.pipeline.length };
  pg.menu = null; pg.menuAt = null;
  if (kind === "new-agent") { pg.tab = "agents"; pg.form = { kind: "agent", edit: null, then: at, errors: [], data: { title: "", name: "", role: "", model: "sonnet", effort: "medium", writes_code: false, tools: ui.cfg.options.default_tools.join(", "), prompt: PG_PROMPT("…", ""), auto: true, promptAuto: true } }; pgPaint(); return; }
  let made;
  pgCommit((d) => {
    const g = at.insert !== undefined ? at.insert : at.phase;  // the phase it goes before; a parallel step reads what its phase reads
    made = kind === "gateway" ? pgNewGate(d, t.dataset.mode, g) : pgNewStage(d, t.dataset.agent, g);
    if (at.insert !== undefined || kind === "gateway") d.pipeline.splice(g, 0, [made]); else d.pipeline[at.phase].push(made);
  });
  pg.sel = made.id; pg.tab = "step"; pgPaint();
}
function pgGate(s, on) {
  if (on) {
    const stages = flat(ui.pg.draft.pipeline), i = stages.indexOf(s), build = stages.slice(0, i).reverse().find((o) => o.agent === "developer") || stages[i - 1];
    Object.assign(s, { pass: [VERDICT_HINT[s.agent] || "APPROVE"], on_fail: build ? build.id : null, required: false, gateway: "autonomous" });
  } else ["pass", "on_fail", "required", "max_loops", "gateway"].forEach((k) => delete s[k]);
}
function pgSetField(s, f, el, change) {
  const d = ui.pg.draft, v = el.value;
  if (f === "id") {
    const old = s.id, id = v.trim();
    s.id = id;
    flat(d.pipeline).forEach((o) => { if (o.on_fail === old) o.on_fail = id; });
    if (ui.pg.sel === old) ui.pg.sel = id;
  } else if (f === "inputs" || f === "outputs") {
    const set = new Set(s[f] || []); el.checked ? set.add(v) : set.delete(v); s[f] = [...set];
  } else if (f === "pass") {
    const list = v.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
    if (list.length) s.pass = list;
  } else if (f === "gated") pgGate(s, el.checked);
  else if (f === "gateway") s.gateway = v;
  else if (f === "on_fail") s.on_fail = v || null;
  else if (f === "required") s.required = el.checked;
  else if (f === "max_loops") { if (v === "") delete s.max_loops; else s.max_loops = Number(v); }
  else if (f === "output") {
    s.output = v.trim();
    if (change && /^[\w-]+(\/[\w-]+)*\.md$/.test(s.output)) { const a = pgArtifactAt(d, s.output, s.title); if (!(s.outputs || []).includes(a)) s.outputs = [...(s.outputs || []), a]; }
  } else if (f === "agent") s.agent = v;
  else s[f] = v;
}
function pgMove(id, how, value) {
  pgCommit((d) => {
    const P = d.pipeline, i = P.findIndex((ph) => ph.some((s) => s.id === id)), s = P[i].find((x) => x.id === id), k = P[i].indexOf(s);
    if (how === "up" || how === "down") { const j = how === "up" ? k - 1 : k + 1; [P[i][k], P[i][j]] = [P[i][j], P[i][k]]; }
    else if (how === "phase-up" || how === "phase-down") {
      const j = how === "phase-up" ? i - 1 : i + 1;
      if (P[i].length === 1) [P[i], P[j]] = [P[j], P[i]];
      else { P[i].splice(k, 1); P.splice(how === "phase-up" ? i : i + 1, 0, [s]); }
    } else if (how === "phase") {
      P[i].splice(k, 1);
      if (value === "new") P.splice(i + 1, 0, [s]); else P[Number(value)].push(s);
    } else if (how === "delete") {
      P[i].splice(k, 1);
      flat(P).forEach((o) => { if (o.on_fail === id) o.on_fail = null; });
    }
  });
  pgPaint();
}
function pgSaveForm() {
  const pg = ui.pg, f = pg.form, d = f.data; f.errors = [];
  if (f.kind === "artifact") {
    if (!d.title.trim()) f.errors.push("Give the artifact a title.");
    if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(d.id)) f.errors.push("The id is lowercase letters, digits and dashes.");
    if (!/^[\w-]+(\/[\w-]+)*\.md$/.test(d.path)) f.errors.push("The path is a relative .md file such as docs/spec.md.");
    const others = Object.values(pgTable()).filter((a) => a.id !== (f.edit || ""));
    if (others.some((a) => a.id === d.id)) f.errors.push(`The id ${d.id} is already used.`);
    if (others.some((a) => a.path === d.path)) f.errors.push(`${d.path} is already another artifact.`);
    if (f.errors.length) { pgPaint(["side"]); return; }
    pgCommit((dr) => {
      const rec = { id: d.id, title: d.title.trim(), path: d.path, description: d.description.trim() }, i = dr.artifacts.findIndex((a) => a.id === f.edit);
      if (i >= 0) dr.artifacts[i] = rec; else dr.artifacts.push(rec);
    });
  } else if (d.std) {  // a standard agent: model, effort, tools and the enabled switch go in the plan's agent settings
    const tools = [...new Set(d.tools.split(/[,\n]/).map((x) => x.trim()).filter(Boolean))];
    f.errors.push(...toolProblems(tools));
    if (!d.enabled && flat(pg.draft.pipeline).some((s) => s.agent === d.name)) f.errors.push(`${d.title} is in the plan, so it can’t be disabled.`);
    if (f.errors.length) { pgPaint(["side"]); return; }
    pgCommit((dr) => {
      const c = Object.assign({}, dr.agents[d.name], { model: d.model, effort: d.effort });
      if (d.enabled) delete c.enabled; else c.enabled = false;
      if (tools.join(",") === ui.cfg.catalog[d.name].tools.join(",")) delete c.tools; else c.tools = tools;
      dr.agents[d.name] = c;
    });
  } else {
    if (!d.title.trim()) f.errors.push("Give the agent a title.");
    if (!/^[a-z][a-z0-9-]{1,29}$/.test(d.name)) f.errors.push("The name is lowercase letters, digits and dashes, starting with a letter.");
    if (!f.edit && pgAgents()[d.name]) f.errors.push(`The name ${d.name} is already taken.`);
    if (!d.prompt.trim()) f.errors.push("Write the agent’s prompt.");
    const tools = [...new Set(d.tools.split(/[,\n]/).map((x) => x.trim()).filter(Boolean))];
    f.errors.push(...toolProblems(tools));
    if (f.errors.length) { pgPaint(["side"]); return; }
    pgCommit((dr) => {
      dr.custom_agents[d.name] = { title: d.title.trim(), role: d.role.trim(), prompt: d.prompt.trim(), writes_code: !!d.writes_code, tools };
      dr.agents[d.name] = Object.assign({}, dr.agents[d.name], { model: d.model, effort: d.effort });
    });
    if (f.then) {  // created from the + menu: add a step with the new agent where it was asked for
      pg.menuAt = f.then; pg.form = null; pg.tab = "step";
      pgAdd("agent", { dataset: { agent: d.name } });
      return;
    }
  }
  pg.form = null; pgPaint();
}
async function pgSave() {
  const pg = ui.pg;
  if (!pg || pg.saving) return;
  pg.saving = true; pgPaint(["bar"]);
  const wasDirty = dirty(), out = await post("/api/settings", pgBody());
  pg.saving = false;
  if (!out.ok) { pg.issues = { errors: (out.errors || ["Couldn’t save."]).map((m) => ({ message: m, stage: null })), warnings: [] }; pgPaint(["bar", "checks"]); return; }
  ui.cfg = out; ui.cfg.settings.pipeline = phasesOf(out.settings.pipeline); ui.cfg.defaults.pipeline = phasesOf(out.defaults.pipeline);
  const saved = pgPick(ui.cfg.settings);
  if (wasDirty) Object.assign(ui.draft, clone(saved)); else ui.draft = clone(ui.cfg.settings);  // keep unsaved edits made in Settings
  pg.draft = clone(saved); pg.base = JSON.stringify(saved); pg.undo = []; pg.redo = [];
  if (pg.sel && pg.sel !== "$start" && !pgFind(pg.sel)) pg.sel = null;
  toast("Plan saved. It applies to new runs.");
  pgCheck(); pgPaint(); fetchState();
}

/* ---------- events ---------- */
function pgOnClick(e) {
  const pg = ui.pg, t = e.target;
  if (!t.closest) return;
  if (pg && pg.menu && !t.closest("[data-pg-menu], [data-pg-gap], [data-pg-col]")) { pg.menu = pg.menuAt = null; pgPaint(["canvas"]); }
  const open = document.querySelector("details[data-pg-pop][open]");
  if (open && !open.contains(t)) open.open = false;
  const art = t.closest("[data-pg-art]");
  if (art && (t.closest(".pg") || t.closest("#planSteps"))) {
    const id = art.dataset.pgArt;
    if (pg) { pg.flow = pg.flow === id ? null : id; pgPaint(["canvas"]); } else { ui.pgFlow = ui.pgFlow === id ? null : id; render(); }
    return;
  }
  const b = t.closest("[data-pg], [data-pg-add], [data-pg-gap], [data-pg-col], [data-pg-loop], [data-pg-node], [data-pg-tab], [data-pg-select], [data-approve]");
  if (!b) return;
  if (b.dataset.approve) { openApproval(b.dataset.approve); return; }
  if (b.dataset.pg === "open") { pgOpen(); return; }
  if (b.dataset.pg === "open-from-settings") { location.hash = "#plan"; pgOpen(); return; }
  if (b.dataset.pg === "flow-off") { ui.pgFlow = null; render(); return; }
  if (!pg) {  // view mode: a waiting gateway opens its decision
    const run = (ui.data.runs || []).find((r) => r.status === "approval" && ((ui.planRun && ui.planRun !== "none") ? r.id === ui.planRun : true));
    if (b.dataset.pgNode && run && run.approval && run.approval.stage === b.dataset.pgNode) openApproval(run.id);
    return;
  }
  if (b.dataset.pgNode) {
    if (pg.connect) {
      const from = pgFind(pg.connect), to = pgFind(b.dataset.pgNode);
      if (to && from && pgCanTarget({ phases: pg.draft.pipeline, connect: pg.connect }, to)) { pgCommit(() => (from.on_fail = to.id)); pg.sel = from.id; pg.connect = null; pgPaint(); }
      else toast("Pick a step in the same phase or an earlier one.");
      return;
    }
    pg.sel = b.dataset.pgNode; pg.tab = "step"; pgPaint(["canvas", "side"]); return;
  }
  if (b.dataset.pgLoop) { pg.sel = b.dataset.pgLoop; pg.tab = "step"; pgPaint(["canvas", "side"]); document.querySelector(".loopsec") && document.querySelector(".loopsec").scrollIntoView({ block: "nearest" }); return; }
  if (b.dataset.pgSelect) { pg.sel = b.dataset.pgSelect; pg.tab = "step"; pgPaint(["canvas", "side"]); return; }
  if (b.dataset.pgTab) {
    pg.tab = b.dataset.pgTab; if (b.dataset.new === "artifact") pgNewForm("artifact");
    pgPaint(["side"]); return;
  }
  if (b.dataset.pgGap !== undefined) { pg.menuAt = { insert: Number(b.dataset.pgGap) }; pg.menu = { at: pg.menuAt }; pgPaint(["canvas"]); return; }
  if (b.dataset.pgCol !== undefined) { pg.menuAt = { phase: Number(b.dataset.pgCol) }; pg.menu = { at: pg.menuAt }; pgPaint(["canvas"]); return; }
  if (b.dataset.pgAdd) {
    if (!pg.menuAt) { const i = pg.sel && pg.sel !== "$start" ? pgPhaseOf(pg.sel) : -1; pg.menuAt = { insert: i < 0 ? pg.draft.pipeline.length : i + 1 }; }
    const pop = b.closest("details"); if (pop) pop.open = false;
    pgAdd(b.dataset.pgAdd, b); return;
  }
  const act = b.dataset.pg, s = pg.sel && pg.sel !== "$start" ? pgFind(pg.sel) : null;
  if (act === "undo") pgStep("undo", "redo");
  else if (act === "redo") pgStep("redo", "undo");
  else if (act === "save") pgSave();
  else if (act === "close") pgClose();
  else if (act === "connect") { pg.connect = pg.sel; pgPaint(["canvas"]); }
  else if (act === "connect-off") { pg.connect = null; pgPaint(["canvas"]); }
  else if (act === "ungate" && s) { pgCommit(() => pgGate(s, false)); pgPaint(["canvas", "side"]); }
  else if (act === "cond-add" && s) { pgCommit(() => { const used = (s.conditions || []).map((c) => c.metric); (s.conditions = s.conditions || []).push(defaultCondition(Object.keys(ui.cfg.options.metrics).find((m) => !used.includes(m)) || "test_pass_rate")); }); pgPaint(["canvas", "side"]); }
  else if (act === "cond-remove" && s) { pgCommit(() => s.conditions.splice(Number(b.dataset.n), 1)); pgPaint(["canvas", "side"]); }
  else if (["up", "down", "phase-up", "phase-down", "delete"].includes(act) && s) { if (act !== "delete" || confirm(`Delete “${s.title}”?`)) pgMove(s.id, act); }
  else if (act === "flow") { pg.flow = pg.flow === b.dataset.id ? null : b.dataset.id; pgPaint(["canvas", "side"]); }
  else if (act === "art-new") { pgNewForm("artifact"); pgPaint(["side"]); }
  else if (act === "agent-new") { pgNewForm("agent"); pgPaint(["side"]); }
  else if (act === "art-edit") { const a = pg.draft.artifacts.find((x) => x.id === b.dataset.id); pg.form = { kind: "artifact", edit: a.id, errors: [], data: clone(a) }; pgPaint(["side"]); }
  else if (act === "art-delete") {
    const id = b.dataset.id, users = flat(pg.draft.pipeline).filter((x) => (x.inputs || []).includes(id) || (x.outputs || []).includes(id));
    if (confirm(users.length ? `Delete this artifact? It is removed from ${users.length} step${users.length > 1 ? "s" : ""}.` : "Delete this artifact?")) {
      pgCommit((d) => { d.artifacts = d.artifacts.filter((a) => a.id !== id); flat(d.pipeline).forEach((x) => { x.inputs = (x.inputs || []).filter((a) => a !== id); x.outputs = (x.outputs || []).filter((a) => a !== id); }); });
      if (pg.flow === id) pg.flow = null;
      pgPaint();
    }
  } else if (act === "agent-prompt") openPrompt(b.dataset.id);
  else if (act === "agent-edit") { pgEditAgent(b.dataset.id); pgPaint(["side"]);
  } else if (act === "agent-delete") {
    if (confirm("Delete this agent?")) { pgCommit((d) => { delete d.custom_agents[b.dataset.id]; delete d.agents[b.dataset.id]; }); pgPaint(["side", "canvas"]); }
  } else if (act === "form-save") pgSaveForm();
  else if (act === "form-cancel") { const then = pg.form && pg.form.then; pg.form = null; if (then) pg.tab = "step"; pgPaint(["side"]); }
}
function pgEditAgent(n) {  // opens the edit form of an agent: a standard one (model, effort, tools, enabled) or one of the project's own
  const pg = ui.pg, mine = pg.draft.custom_agents[n], c = pg.draft.agents[n] || {};
  if (mine) pg.form = { kind: "agent", edit: n, errors: [], data: { title: mine.title, name: n, role: mine.role, model: c.model || "sonnet", effort: c.effort || "medium", writes_code: !!mine.writes_code, tools: mine.tools.join(", "), prompt: mine.prompt } };
  else { const a = pgAgents()[n]; pg.form = { kind: "agent", edit: n, errors: [], data: { std: true, title: a.title, name: n, role: a.role || "", model: c.model || a.model, effort: c.effort || a.effort, writes_code: !!a.writes_code, enabled: c.enabled !== false, tools: (c.tools || a.tools).join(", "), prompt: "" } }; }
}
function pgEditFromRoster(n) {  // the Edit button on the Overview roster: open the plan editor on this agent's form
  if (!ui.pg) pgOpen();
  if (!ui.pg) return;
  ui.pg.tab = "agents"; pgEditAgent(n);
  if (location.hash === "#plan") render(); else location.hash = "#plan";
}
function pgNewForm(kind) {
  ui.pg.form = kind === "artifact" ? { kind, edit: null, errors: [], data: { title: "", id: "", path: "docs/", description: "", auto: true } } : { kind, edit: null, errors: [], data: { title: "", name: "", role: "", model: "sonnet", effort: "medium", writes_code: false, tools: ui.cfg.options.default_tools.join(", "), prompt: PG_PROMPT("…", ""), auto: true, promptAuto: true } };
}
function pgOnInput(e) {
  const pg = ui.pg, t = e.target;
  if (!pg || !t.closest || !t.closest(".pg")) return;
  const change = e.type === "change";
  if (t.dataset.pgform !== undefined) {  // the new/edit form for an artifact or agent: kept in pg.form until saved
    const f = pg.form, k = t.dataset.pgform, d = f.data;
    const put = (key, value) => { d[key] = value; const el = document.querySelector(`[data-pgform="${key}"]`); if (el) el.value = value; };
    d[k] = t.type === "checkbox" ? t.checked : t.value;
    if (["id", "path", "name"].includes(k)) d.auto = false;
    if (k === "prompt") d.promptAuto = false;
    if (!f.edit && f.kind === "artifact" && k === "title" && d.auto) { put("id", pgSlug(d.title)); put("path", `docs/${d.id}.md`); }
    if (!f.edit && f.kind === "agent" && ["title", "role"].includes(k)) {
      if (k === "title" && d.auto) put("name", pgSlug(d.title));
      if (d.promptAuto) put("prompt", PG_PROMPT(d.title || "…", d.role));
    }
    return;
  }
  const s = pg.sel && pg.sel !== "$start" ? pgFind(pg.sel) : null;
  if (!s) return;
  if (t.dataset.pgc !== undefined) {
    const c = s.conditions[Number(t.dataset.pgc)], f = t.dataset.f;
    if (f === "metric") { if (!change) return; pgCommit(() => (s.conditions[Number(t.dataset.pgc)] = defaultCondition(t.value))); pgPaint(["canvas", "side"]); return; }
    pgCommit(() => (c[f] = f === "value" ? (t.value === "" ? "" : Number(t.value)) : t.value), "c" + t.dataset.pgc + f);
    pgPaint(["canvas"]); return;
  }
  const f = t.dataset.pgf;
  if (!f) return;
  const text = ["title", "id", "task", "output", "pass", "max_loops"].includes(f) && t.type !== "checkbox";
  if (text && !change) { pgCommit(() => pgSetField(s, f, t, false), "f" + f + s.id); pgPaint(["canvas"]); return; }
  if (f === "agent" && t.value === "__new") { pgAdd("new-agent"); pg.menuAt = null; pgPaint(); return; }
  if (f === "phase") { pgMove(s.id, "phase", t.value); return; }
  pgCommit(() => pgSetField(s, f, t, true), text ? "f" + f + s.id : null);
  pgPaint(["canvas", "side"]);
}
document.addEventListener("click", pgOnClick);
document.addEventListener("input", pgOnInput);
document.addEventListener("change", pgOnInput);
document.addEventListener("keydown", (e) => {
  const pg = ui.pg, t = e.target;
  if (!pg) {
    if ((e.key === "Enter" || e.key === " ") && t.dataset && (t.dataset.pgNode || t.dataset.pgArt)) { e.preventDefault(); t.click(); }
    return;
  }
  const typing = t.matches && t.matches("input, textarea, select");
  if (e.key === "Escape") { if (pg.menu || pg.connect) { pg.menu = pg.connect = null; pgPaint(["canvas"]); } return; }
  if ((e.key === "Enter" || e.key === " ") && !typing && t.dataset && (t.dataset.pgNode || t.dataset.pgArt || t.dataset.pgLoop)) { e.preventDefault(); t.click(); return; }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); pgStep(e.shiftKey ? "redo" : "undo", e.shiftKey ? "undo" : "redo"); return; }
  if ((e.key === "Delete" || e.key === "Backspace") && !typing && pg.sel && pg.sel !== "$start" && pgFind(pg.sel)) { const s = pgFind(pg.sel); if (confirm(`Delete “${s.title}”?`)) pgMove(s.id, "delete"); }
});
window.addEventListener("beforeunload", (e) => { if (pgDirty()) { e.preventDefault(); e.returnValue = ""; } });

/* ---------- manual gateway decision ---------- */
function openApproval(runId) {
  const run = ((ui.data && ui.data.runs) || []).find((r) => r.id === runId), a = run && run.approval;
  if (!a) { toast("That run isn’t waiting for a decision."); return; }
  const dlg = $("apprDlg");
  dlg.dataset.run = run.id; dlg.dataset.stage = a.stage;
  $("apprTitle").textContent = `${a.title} needs your decision`;
  $("apprSub").textContent = run.source.replace(/\.md$/, "");
  $("apprBody").innerHTML = `<div>This manual gateway didn’t pass${a.verdict ? ` (verdict <b>${esc(a.verdict)}</b>)` : ""}. Katana stopped the run here and waits for you.</div>
    <ul>${(a.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    <div class="muted" style="font-size:12.5px">${a.report ? `Read the findings: <button class="btn ghost mono" style="height:auto;padding:0" data-report="${esc(run.id)}" data-name="${esc(a.report)}">${esc(a.report)}</button> · ` : ""}rework loops used: ${a.loops_used} of ${a.loops_max}</div>
    <label class="field"><span>Note (optional)</span><input type="text" id="apprNote" maxlength="500" placeholder="Why you decided this"></label>
    <div class="acts" style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn primary" data-appr="rework">Send back to ${esc(a.target_title)}</button>
      <button class="btn" data-appr="approve">Approve anyway and continue</button>
      <button class="btn" data-appr="reject" style="margin-left:auto">Reject: fail the run</button></div>
    <div id="apprErr" class="errors"></div>`;
  if (!dlg.open) dlg.showModal();
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest && e.target.closest("[data-appr]");
  if (!b) return;
  const dlg = $("apprDlg");
  if (b.dataset.appr === "close") { dlg.close(); return; }
  if (b.dataset.appr === "reject" && !confirm("Reject this run? It is marked failed and stops.")) return;
  const out = await post("/api/approval", { run: dlg.dataset.run, stage: dlg.dataset.stage, decision: b.dataset.appr, note: $("apprNote") ? $("apprNote").value : "" });
  if (!out.ok) { $("apprErr").textContent = (out.errors || ["Couldn’t record the decision."]).join(" "); return; }
  dlg.close();
  toast({ approve: "Approved.", rework: "Sent back.", reject: "Run rejected." }[b.dataset.appr] + (out.started ? " The run is continuing." : " It continues on the next scan, or press Run now."));
  fetchState();
});
