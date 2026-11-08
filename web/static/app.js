"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k, d) { try { const v = localStorage.getItem("lr." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("lr." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

const S = {
  lang: store.get("lang", "zh"), set: null, boxes: [], inbox: null, run: null, runId: null, runs: [],
  review: { decisions: {}, exports: [], status: {}, blocking: [] },
  client: "ALL", rules: new Set(), tab: store.get("tab", "todo"), me: null, unmasked: null,
  job: null, jobEnd: null, interrupted: [], focus: 0, view: null,
  outbox: store.get("outbox", []), offline: false,
};
const DECISIONS = ["confirm", "nondeductible", "exclude", "defer"];
const GOOD = new Set(["ocr-confirmed", "ocr-confirmed (second reader)", "snapped", "ocr-zoom", "ocr-role", "ocr", "ocr-keyword"]);
const ICON_LINK = '<svg viewBox="0 0 24 24"><path d="M14 3h7v7M10 14L21 3M21 14v7H3V3h7"/></svg>';

// ---------- language ----------
function t(key, v = {}) {
  const s = (I18N[S.lang] && I18N[S.lang][key]) ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (v[k] ?? ""));
}
function applyLang() {
  document.documentElement.lang = S.lang === "zh" ? "zh-Hant" : "en";
  $$("[data-i18n]").forEach((el) => (el.innerHTML = t(el.dataset.i18n)));
  $$("[data-i18n-title]").forEach((el) => (el.title = t(el.dataset.i18nTitle)));
  $("#langBtn").textContent = t("lang");
}
const ruleShort = (r) => (RULES_TEXT[r]?.[S.lang]?.[0]) || RULES_TEXT[r]?.en?.[0] || r;
const ruleTitle = (e) => (S.lang === "zh" && RULES_TEXT[e.rule_id]?.zh) ? RULES_TEXT[e.rule_id].zh[1] : e.title;
const ruleWhy = (e) => (S.lang === "zh" && RULES_TEXT[e.rule_id]?.zh) ? RULES_TEXT[e.rule_id].zh[2] : e.message;
function citeLabel(c) {
  if (S.lang !== "zh") return c.label;
  const u = c.url || "";
  const m = u.match(/pcode=(\w+)&flno=([\w-]+)/);
  if (m && LAW_ZH[m[1]]) return `${LAW_ZH[m[1]]} 第 ${m[2]} 條`;
  if (u.includes("fia.gov.tw")) return "財政部財政資訊中心：統一編號檢查碼說明";
  if (u.includes("GL009478")) return "營業稅電子資料申報繳稅作業要點";
  if (u.includes("data.gov.tw")) return "政府資料開放平臺：營業登記資料";
  return c.label;
}
function ago(ms) {
  const d = (Date.now() - ms) / 1000;
  if (!isFinite(d) || d < 60) return t("justNow");
  if (d < 3600) return t("minAgo", { n: Math.floor(d / 60) });
  if (d < 86400) return t("hrAgo", { n: Math.floor(d / 3600) });
  return t("dayAgo", { n: Math.floor(d / 86400) });
}
function runTime(id) {
  const m = /(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id || "");
  // the id is in the server's local time, which can differ from this browser's (a host in UTC, a firm in Taipei)
  const off = S.me?.tz_offset_s ?? -new Date().getTimezoneOffset() * 60;
  return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) - off * 1000 : NaN;
}
const tsMs = (ts) => (ts ? Date.parse(ts) : NaN);
const um = () => (S.unmasked && S.unmasked === S.runId ? "&unmask=1" : "");

// ---------- network: retries, offline bar, sign-in ----------
function setOffline(on) {
  if (S.offline === on) return;
  S.offline = on;
  $("#offline").hidden = !on;
}
async function j(url, opt) {
  let r;
  try { r = await fetch(url, opt); } catch (e) { setOffline(true); throw e; }
  setOffline(false);
  if (r.status === 401) { await login(); return j(url, opt); }
  if (!r.ok) {
    let b = {}; try { b = await r.json(); } catch (e) { /* not JSON */ }
    const err = new Error(b.error || `${url} ${r.status}`); err.status = r.status; err.body = b; throw err;
  }
  return r.json();
}
const post = (url, obj) => j(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });
function toast(msg) {
  const el = $("#toast"); el.textContent = msg; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), 5000);
}

function login() {
  return new Promise((resolve) => {
    const dlg = $("#login");
    $("#lgErr").hidden = true;
    $("#loginForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const r = await fetch("api/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: $("#lgUser").value, password: $("#lgPass").value }) });
      const b = await r.json();
      if (!r.ok) { $("#lgErr").textContent = b.error || t("loginFail"); $("#lgErr").hidden = false; return; }
      $("#lgPass").value = "";
      dlg.close(); S.me = b; showUser(); resolve();
    };
    dlg.addEventListener("cancel", (e) => e.preventDefault());
    if (!dlg.open) dlg.showModal();
  });
}
const isAdmin = () => S.me?.role === "admin";
function showUser() {
  const me = S.me || {};
  $("#userPill").hidden = !me.user || me.local;
  $("#userPill").textContent = me.user ? `${me.user} (${me.role})` : "";
  $("#logoutBtn").hidden = !me.user || me.local;
  showMask();
}

// Personal data is masked by default (safeguard regulation Art. 17). A role allowed to unmask gives a reason, which
// the server writes to the operation log; the grant covers one check for ten minutes.
function showMask() {
  const shown = S.unmasked && S.unmasked === S.runId;
  $("#maskPill").textContent = shown ? t("shown") : t("masked");
  $("#maskPill").classList.toggle("ok", !shown);
  const b = $("#unmaskBtn");
  b.hidden = !(S.me && S.me.can_unmask) || !S.runId;
  b.textContent = shown ? t("remask") : t("unmask");
}
async function toggleUnmask() {
  if (S.unmasked === S.runId) { S.unmasked = null; return loadRun(S.runId); }
  const reason = window.prompt(t("unmaskAsk"));
  if (!reason || reason.trim().length < 4) return;
  try {
    const b = await post("api/unmask", { run: S.runId, reason: reason.trim() });
    S.unmasked = S.runId;
    setTimeout(() => { if (S.unmasked === b.run) { S.unmasked = null; loadRun(S.runId); } }, b.expires_s * 1000);
    await loadRun(S.runId);
  } catch (e) { window.alert(e.message || t("notAllowed")); }
}

// ---------- start-up ----------
async function init() {
  applyLang();
  S.me = await (await fetch("api/me")).json();
  if (!S.me.user) await login();
  showUser();
  $("#logoutBtn").onclick = async () => { await fetch("api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); location.reload(); };
  $("#unmaskBtn").onclick = toggleUnmask;
  $("#langBtn").onclick = () => { S.lang = S.lang === "zh" ? "en" : "zh"; store.set("lang", S.lang); applyLang(); showUser(); status(); renderAll(); if (S.tab === "log") renderLog(); };
  $$(".tab").forEach((x) => (x.onclick = () => setTab(x.dataset.tab)));
  $("#closeViewer").onclick = () => $("#viewer").close();
  $("#viewer").addEventListener("click", (e) => { if (e.target.id === "viewer") $("#viewer").close(); });
  $("#viewPrev").onclick = () => stepView(-1);
  $("#viewNext").onclick = () => stepView(1);
  document.addEventListener("keydown", onKey);
  window.addEventListener("beforeunload", (e) => { if (S.outbox.length) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("online", flush);
  S.boxes = await j("api/inboxes");
  const sel = $("#inbox");
  sel.onchange = () => { store.set("set", sel.value); loadInbox(sel.value); };
  const want = store.get("set", null);
  const first = S.boxes.some((b) => b.set === want) ? want : S.boxes[0]?.set;
  renderInboxSelect(first);
  status();
  setTab(S.tab);
  if (first) await loadInbox(first);
  flush();
}
function renderInboxSelect(value) {
  const sel = $("#inbox");
  sel.innerHTML = S.boxes.map((b) => `<option value="${esc(b.set)}">${esc(b.name?.[S.lang] || b.set)}</option>`).join("");
  if (value) sel.value = value;
}

async function status() {
  try {
    const s = await j("api/status");
    const strict = s.strict ? ` · ${t("strict")}` : "";
    $("#modelPill").textContent = (s.model_loaded ? t("modelOn", { m: s.model, d: s.device }) : t("modelLater")) + strict;
  } catch (e) { $("#modelPill").textContent = t("modelLoading"); }
}

async function loadInbox(set) {
  S.set = set; S.client = "ALL"; S.rules.clear(); S.run = null; S.runId = null; S.jobEnd = null; S.focus = 0;
  S.review = { decisions: {}, exports: [], status: {}, blocking: [] };
  S.inbox = await j(`api/inbox?set=${encodeURIComponent(set)}`);
  $("#synthBanner").hidden = !S.inbox.synthetic;
  const runs = await j("api/runs");
  S.runs = runs.filter((r) => r.startsWith(set + "-"));
  const pick = S.runs[0] || (runs.includes("eval-" + set) ? "eval-" + set : null);
  if (pick) await loadRun(pick); else renderAll();
  await checkJobs();
}

async function loadRun(runId) {
  S.runId = runId;
  S.run = await j(`api/result?run=${encodeURIComponent(runId)}${um()}`);
  await loadReview();
  renderAll();
  showMask();
  if (S.tab === "log") renderLog();
}
async function loadReview() {
  if (!S.runId) return;
  try { S.review = await j(`api/review?run=${encodeURIComponent(S.runId)}${um()}`); }
  catch (e) { S.review = { decisions: {}, exports: [], status: {}, blocking: [] }; }
  // decisions made while offline and not sent yet win over what the server has
  S.outbox.filter((o) => o.run === S.runId).forEach(applyLocal);
}

// ---------- the review queue: one item per voucher ----------
const allExceptions = () => (S.run ? S.run.exceptions.concat(S.run.period_check?.exceptions || []) : []);
function items() {
  const by = new Map();
  for (const e of allExceptions()) {
    if (e.severity === "info") continue;
    if (!by.has(e.doc_id)) by.set(e.doc_id, { doc_id: e.doc_id, client_id: e.client_id, exs: [], einv: e.source === "e-invoice" });
    by.get(e.doc_id).exs.push(e);
  }
  const blocking = new Set(S.review.blocking || []);
  return [...by.values()].map((it) => {
    const st = S.review.status?.[it.doc_id];
    it.status = st;
    it.must = st ? st === "HOLD" : it.exs.some((e) => blocking.has(e.rule_id) || e.severity === "hold");
    it.dec = S.review.decisions?.[it.doc_id] || null;
    const sug = it.exs.map((e) => SUGGEST[e.rule_id]).find(Boolean);
    it.suggest = sug || null;
    return it;
  });
}
const SEV = { high: 0, hold: 1, review: 2, medium: 3, warning: 4 };
function sortItems(list) {
  return list.sort((a, b) => (b.must - a.must) || a.client_id.localeCompare(b.client_id) || a.doc_id.localeCompare(b.doc_id));
}
const inFilter = (it) => (S.client === "ALL" || it.client_id === S.client) && (!S.rules.size || it.exs.some((e) => S.rules.has(e.rule_id)));
const todo = () => sortItems(items().filter((it) => !it.dec && inFilter(it)));
const done = () => sortItems(items().filter((it) => it.dec && inFilter(it)));
const pendingMust = () => items().filter((it) => it.must && !it.dec);

// ---------- decisions: saved at once; kept in this browser until the server has them ----------
function applyLocal(o) {
  if (o.decision === "undo") delete S.review.decisions[o.doc_id];
  else S.review.decisions[o.doc_id] = { decision: o.decision, user: S.me?.user, ts: new Date(o.at).toISOString(), pending: true };
}
function decide(docId, decision) {
  if (!S.runId) return;
  const o = { run: S.runId, doc_id: docId, decision, at: Date.now() };
  S.outbox.push(o); store.set("outbox", S.outbox);
  applyLocal(o);
  renderAll();
  flush();
}
let flushing = false;
async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    while (S.outbox.length) {
      const o = S.outbox[0];
      try {
        await post("api/review", { run: o.run, doc_id: o.doc_id, decision: o.decision });
        S.outbox.shift(); store.set("outbox", S.outbox);
        const d = S.review.decisions[o.doc_id];
        if (d && o.run === S.runId) delete d.pending;
      } catch (e) {
        if (e.status) {   // the server refused it: drop it, say so
          S.outbox.shift(); store.set("outbox", S.outbox);
          toast(t("saveFail", { e: e.message }));
          if (o.run === S.runId) await loadReview();
        } else {          // no connection: try again shortly
          setTimeout(flush, 3000);
          return;
        }
      }
    }
    renderAll();
  } finally { flushing = false; }
}

// ---------- rendering ----------
function renderAll() {
  if (!S.inbox) return;
  renderInboxSelect(S.set);
  renderSteps(); renderStage(); renderClients(); renderChips(); renderTodo(); renderDone(); renderDocs();
  const p = pendingMust().length;
  document.title = S.run && p ? t("titlePending", { n: p }) : "Ledger Room";
  $("#cntTodo").textContent = S.run ? (p || todo().length || "") : "";
  $("#cntTodo").classList.toggle("hot", !!p);
  $("#cntDone").textContent = S.run ? (done().length || "") : "";
}

function newDocs() {
  if (!S.run) return 0;
  const seen = new Set(S.run.docs.map((d) => d.doc_id));
  return S.inbox.docs.filter((d) => !seen.has(d.doc_id)).length;
}
const lastExport = () => (S.review.exports || []).slice(-1)[0];

function stepState() {
  const n = S.inbox.docs.length, running = !!S.job;
  const s1 = n > 0;
  const s2 = !!S.run && !newDocs() && !running;
  const p = pendingMust().length;
  const s3 = s2 && p === 0;
  const ex = lastExport();
  const s4 = s3 && !!ex && tsMs(ex.ts) >= latestDecisionTs();
  return { s1, s2, s3, s4, p, n, running };
}
function latestDecisionTs() {
  return Math.max(0, ...Object.values(S.review.decisions || {}).map((d) => tsMs(d.ts) || 0));
}
function renderSteps() {
  const st = stepState();
  const cur = !st.s1 ? 1 : !st.s2 ? 2 : !st.s3 ? 3 : 4;
  const ex = lastExport();
  const sub = [
    st.s1 ? t("s1sub", { n: st.n, m: S.inbox.clients.length }) : t("s1empty"),
    st.running ? t("s2run", { i: S.job.i || 0, n: S.job.n || st.n }) : !S.run ? t("s2none") : newDocs() ? t("s2new", { n: newDocs() }) : t("s2done", { when: ago(runTime(S.runId)) }),
    !S.run ? t("s3wait") : st.p ? t("s3left", { n: st.p }) : t("s3done"),
    st.s4 ? t("s4done", { when: ago(tsMs(ex.ts)) }) : st.s3 ? t("s4ready") : t("s4lock"),
  ];
  const done = [st.s1, st.s2, st.s3, st.s4];
  $("#steps").innerHTML = [1, 2, 3, 4].map((k) => {
    const cls = done[k - 1] ? "done" : k === cur ? "now" : "later";
    const icon = done[k - 1] ? '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>' : `<span>${k}</span>`;
    return `<li class="step ${cls}"><div class="num">${icon}</div><div><div class="sname">${t("step" + k)}</div><div class="ssub">${esc(sub[k - 1])}</div></div></li>`;
  }).join("");
}

function btn(id, label, sub, cls = "", extra = "") {
  return `<button class="btn ${cls}" id="${id}" ${extra}>${label}${sub ? `<small>${sub}</small>` : ""}</button>`;
}
function renderStage() {
  const st = stepState();
  const box = $("#stage");
  const runBtn = (label) => btn("runBtn", label, t("runSub"), "primary", isAdmin() ? "" : `disabled title="${esc(t("onlyAdmin"))}"`);
  let head = "", body = "", btns = "";
  if (S.job) {
    const j0 = S.job;
    const per = j0.avg_s || j0.last_s;
    const left = j0.n ? j0.n - j0.i : 0;
    const eta = per && left ? (left * per < 60 ? t("progEtaS") : t("progEta", { m: Math.ceil((left * per) / 60) })) : "";
    const main = j0.status === "loading model" ? t("loadingModel") : j0.status === "queued" ? t("queued") : t("progMain", { i: j0.i, n: j0.n });
    const bits = [eta, per ? t("progPer", { s: Math.round(per * 10) / 10 }) : "", j0.n_failed ? t("progFail", { n: j0.n_failed }) : "", j0.cached ? t("progCached") : ""].filter(Boolean);
    const d = S.inbox.docs.find((x) => x.doc_id === j0.doc_id);
    box.innerHTML = `<div class="progress">
      <div class="prog-row">${d ? `<img src="img/${esc(d.image)}" alt="">` : ""}
        <div class="grow"><div class="prog-main">${esc(main)}</div><div class="muted">${esc(bits.join(" · "))}</div></div></div>
      <div class="bar"><span style="width:${j0.n ? (100 * j0.i) / j0.n : 2}%"></span></div>
      <p class="muted small">${t("progLeave")}</p></div>` + statsHtml();
    return;
  }
  if (!st.s1) {
    box.innerHTML = `<div class="stage-head"><div><h2>${t("s1empty")}</h2><p class="muted">${t("emptyInbox", { set: esc(S.set) })}</p></div></div>`;
    return;
  }
  const intr = S.interrupted[0];
  if (S.jobEnd?.status === "error") {
    head = t("errTitle"); body = t("errSub", { e: esc(S.jobEnd.error || "") }); btns = runBtn(t("runBtn"));
  } else if (intr) {
    head = t("intTitle", { i: intr.i, n: intr.n }); body = t("intSub", { d: intr.n_done });
    btns = btn("resumeBtn", t("resume", { k: intr.n_done + 1 }), "", "primary", isAdmin() ? "" : `disabled title="${esc(t("onlyAdmin"))}"`)
      + btn("runBtn", t("restart"), "", "", isAdmin() ? "" : "disabled");
  } else if (!S.run) {
    head = t("noRunTitle"); body = t("noRunSub"); btns = runBtn(t("runBtn"));
  } else if (newDocs()) {
    head = t("newDocsTitle", { n: newDocs() }); body = t("newDocsSub"); btns = runBtn(t("recheck"));
  } else if (st.p) {
    head = S.jobEnd ? t("doneTitle", { n: S.run.summary.n_docs, p: st.p }) : t("pendingTitle", { n: st.p });
    body = t("pendingSub");
    btns = btn("reviewBtn", t("startReview"), t("startReviewSub"), "primary") + btn("runBtn", t("recheck"), "", "", isAdmin() ? "" : "disabled");
  } else {
    const ex = lastExport();
    head = S.jobEnd && !Object.keys(S.review.decisions).length ? t("doneZero", { n: S.run.summary.n_docs }) : st.s4 ? t("exportedTitle") : t("allDoneTitle");
    body = st.s4 ? t("exportedSub", { who: esc(ex.user), when: ago(tsMs(ex.ts)) }) : t("allDoneSub");
    btns = btn("exportBtn", t("exportBtn"), t("exportSub"), "primary") + btn("runBtn", t("recheck"), "", "", isAdmin() ? "" : "disabled");
  }
  const how = S.runId?.endsWith("-live") ? t("howLive") : t("howCache");
  const lr = S.run ? t("showing", { when: ago(runTime(S.runId)), who: esc(runUser()), how: runUser() ? how : how.replace(/^[，, ]+/, "") }) : "";
  const hist = S.runs.length > 1 ? `<select id="runPick" class="runpick" aria-label="${esc(t("history"))}">${S.runs.slice(0, 12).map((r) =>
    `<option value="${esc(r)}" ${r === S.runId ? "selected" : ""}>${esc(ago(runTime(r)))}${r.endsWith("-live") ? "" : " · " + esc(t("howCache").replace(/^[，, ]+/, ""))}</option>`).join("")}</select>` : "";
  if (S.jobEnd?.carried) body += `<br>${t("carried", { n: S.jobEnd.carried })}`;
  const skipped = S.inbox.skipped?.length ? `<p class="warnline small">${esc(t("skipped", { n: S.inbox.skipped.length, f: S.inbox.skipped.slice(0, 3).join(", ") }))}</p>` : "";
  box.innerHTML = `<div class="stage-head"><div><h2>${head}</h2><p class="muted">${body}</p>${skipped}</div><div class="btns">${btns}</div></div>
    ${lr ? `<div class="runline muted small"><span>${lr}</span>${hist}</div>` : ""}` + statsHtml();
  $("#runBtn") && ($("#runBtn").onclick = () => confirmRun());
  $("#resumeBtn") && ($("#resumeBtn").onclick = () => startRun({ resume: intr.run_id }));
  $("#reviewBtn") && ($("#reviewBtn").onclick = () => { setTab("todo"); const f = todo().find((it) => it.must) || todo()[0]; if (f) openDoc(f.doc_id, "todo"); });
  $("#exportBtn") && ($("#exportBtn").onclick = () => confirmExport());
  $("#runPick") && ($("#runPick").onchange = (e) => { S.jobEnd = null; loadRun(e.target.value); });
}
function runUser() { return S.run?.operator || S.jobEnd?.user || ""; }

function statsHtml() {
  const s = S.run?.summary;
  if (!s) return "";
  const its = items();
  const affected = new Set(its.map((it) => it.client_id)).size;
  const tile = (v, k) => `<div class="stat"><div class="v">${v ?? "-"}</div><div class="k">${k}</div></div>`;
  return `<div class="stats">${tile(s.n_docs, t("tiles_docs"))}${tile(its.length, t("tiles_ex"))}${tile(affected, t("tiles_clients"))}${tile(s.mean_read_s != null ? s.mean_read_s + " s" : null, t("tiles_read"))}${periodLine()}</div>`;
}
function periodLine() {
  const pc = S.run?.period_check;
  if (!pc) return "";
  if (pc.error) return `<div class="period muted">${t("periodErr", { e: esc(pc.error) })}</div>`;
  const s = S.client === "ALL" ? pc.summary : pc.by_client?.[S.client];
  if (!s) return "";
  const who = S.client === "ALL" ? t("thisPeriod") : t("thisPeriodOf", { c: esc(clientName(S.client)) });
  return `<div class="period">${t("periodLine", { who, e: s.einvoices, p: s.paper, h: s.held.total, r: s.ready.total })}</div>` + riskLine(pc, s);
}
function riskLine(pc, s) {
  const nt = (n) => "NT$" + Number(n || 0).toLocaleString("en-US");
  const amounts = (r) => t("riskAmt", { w: nt(r.wrong_claim), u: nt(r.unconfirmed) });
  const note = `<div class="period muted">${t("riskNote")}</div>`;
  if (S.client !== "ALL") {
    const r = s.tax_at_risk;
    return r ? `<div class="period">${t("riskOne")}${amounts(r)}</div>` + note : "";
  }
  const top = (pc.risk_ranking || []).filter((r) => r.wrong_claim || r.unconfirmed).slice(0, 3);
  if (!top.length) return "";
  return `<div class="period">${t("riskFirst")}` + top.map((r) => `${esc(clientName(r.client_id))}: ${amounts(r)}`).join(" · ") + `</div>` + note;
}

function clientName(id) { return S.inbox?.clients.find((c) => c.id === id)?.name || id; }
function docById(id) { return S.run?.docs.find((d) => d.doc_id === id); }

function renderClients() {
  const left = {}, docsBy = {};
  pendingMust().forEach((it) => (left[it.client_id] = (left[it.client_id] || 0) + 1));
  S.inbox.docs.forEach((d) => (docsBy[d.client_id] = (docsBy[d.client_id] || 0) + 1));
  const total = S.run ? pendingMust().length : null;
  const item = (id, name, meta, n) =>
    `<li data-id="${esc(id)}" class="${S.client === id ? "active" : ""}"><span class="cname">${esc(name)}</span>` +
    `<span class="badge ${n ? "hot" : S.run ? "ok" : ""}" title="${n ? esc(t("leftN", { n })) : ""}">${S.run ? (n || '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>') : "-"}</span><span class="cmeta">${esc(meta)}</span></li>`;
  $("#clients").innerHTML = item("ALL", t("allClients"), t("docsN", { n: S.inbox.docs.length }), total) +
    S.inbox.clients.map((c) => item(c.id, c.name, `${c.id} · ${t("docsN", { n: docsBy[c.id] || 0 })}`, left[c.id])).join("");
  $$("#clients li").forEach((li) => (li.onclick = () => { S.client = li.dataset.id; S.focus = 0; renderAll(); }));
}

function renderChips() {
  const its = items().filter((it) => S.client === "ALL" || it.client_id === S.client);
  const count = {};
  its.forEach((it) => new Set(it.exs.map((e) => e.rule_id)).forEach((r) => (count[r] = (count[r] || 0) + 1)));
  const rules = Object.keys(count).sort((a, b) => count[b] - count[a]);
  $("#ruleChips").innerHTML = rules.map((r) => `<button class="chip ${S.rules.has(r) ? "on" : ""}" data-r="${r}">${esc(ruleShort(r))}<b>${count[r]}</b></button>`).join("");
  $$("#ruleChips .chip").forEach((c) => (c.onclick = () => {
    S.rules.has(c.dataset.r) ? S.rules.delete(c.dataset.r) : S.rules.add(c.dataset.r);
    S.focus = 0; renderAll();
  }));
}

function decisionButtons(it) {
  return `<div class="decide" role="group">${DECISIONS.map((d, k) =>
    `<button class="dbtn ${it.suggest === d ? "sug" : ""}" data-doc="${esc(it.doc_id)}" data-d="${d}" title="${esc(t("d_" + d + "_tip"))}"><kbd>${k + 1}</kbd>${t("d_" + d)}</button>`).join("")}</div>`;
}
function itemCard(it, i, mode) {
  const crops = it.exs.flatMap((e) => e.evidence || []).filter((v, k, a) => a.findIndex((x) => x.crop === v.crop) === k)
    .slice(0, 3).map((v) => `<img src="out/${esc(v.crop)}" alt="${esc(v.field)}" title="${esc(t("f_" + v.field))}: ${esc(v.value)}">`).join("");
  const img = it.exs.find((e) => e.image)?.image;
  const exHtml = it.exs.map((e) => {
    const cites = (e.citations || []).filter((c) => c.url || S.lang !== "zh" || /[一-鿿]/.test(c.label)).map((c) => c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${ICON_LINK}${esc(citeLabel(c))}</a>`
      : `<span>${esc(citeLabel(c))}</span>`).join("");
    const prov = (e.evidence || []).map((v) => `${esc(t("f_" + v.field))}: <span class="prov ${GOOD.has(v.provenance) ? "good" : "weak"}" title="${esc(t("provTip", { p: v.provenance || "n/a" }))}">${GOOD.has(v.provenance) ? t("good") : t("weak")}</span>`).join(" ");
    const rel = e.related_docs?.length ? `<div class="rel">${t("sameVoucher")} ${e.related_docs.map((r) => {
      const d = docById(r); return d ? `<img src="img/${esc(d.image)}" data-open="${esc(r)}" title="${esc(r)}"><span class="mono">${esc(r)}</span>` : esc(r);
    }).join(" ")}</div>` : "";
    return `<div class="exrow"><h3>${esc(ruleTitle(e))}</h3><p class="msg">${esc(ruleWhy(e))}</p>${prov ? `<div class="meta">${prov}</div>` : ""}<div class="cite">${cites}</div>${rel}</div>`;
  }).join("");
  const src = it.exs.find((e) => e.source)?.source;
  const tag = it.must ? `<span class="tagm must">${t("must")}</span>` : `<span class="tagm should">${t("should")}</span>`;
  const sug = mode === "todo" ? `<span class="sugline">${it.suggest ? t("suggest", { d: t("d_" + it.suggest) }) : t("suggestPaper")}</span>` : "";
  const dec = it.dec ? `<div class="decided"><span class="dlabel d-${esc(it.dec.decision)}">${t("d_" + it.dec.decision)}</span>
      <span class="muted small">${it.dec.pending ? t("saving") : t("decidedBy", { who: esc(it.dec.user || ""), when: ago(tsMs(it.dec.ts)) })}</span>
      <button class="linkbtn" data-undo="${esc(it.doc_id)}">${t("undo")}</button></div>` : "";
  return `<article class="card ex ${S.focus === i ? "focus" : ""}" data-i="${i}" data-docid="${esc(it.doc_id)}" style="animation-delay:${Math.min(i, 12) * 20}ms">
    <div class="ev">${it.einv ? `<div class="einv" title="${esc(t("einvTip"))}">${t("einvBox")}</div>`
      : `${crops}${img ? `<img class="thumb" src="img/${esc(img)}" data-open="${esc(it.doc_id)}" alt="">` : ""}`}</div>
    <div class="exbody">
      <div class="exhead">${tag}<span class="cn">${esc(clientName(it.client_id))}</span><span class="mono muted small">${esc(it.doc_id)}</span>
        ${src ? `<span class="src">${t("periodSrc", { s: it.einv ? t("einv") : t("paper") })}</span>` : ""}</div>
      ${exHtml}
      <div class="actions">${mode === "todo" ? decisionButtons(it) : dec}${sug}
        ${it.einv ? "" : `<button class="linkbtn" data-open="${esc(it.doc_id)}">${t("open")}</button>`}</div>
    </div></article>`;
}
function bindCards(root, mode) {
  $$("[data-open]", root).forEach((el) => (el.onclick = (ev) => { ev.stopPropagation(); openDoc(el.dataset.open, mode); }));
  $$(".dbtn", root).forEach((b) => (b.onclick = (ev) => { ev.stopPropagation(); decide(b.dataset.doc, b.dataset.d); }));
  $$("[data-undo]", root).forEach((b) => (b.onclick = (ev) => { ev.stopPropagation(); decide(b.dataset.undo, "undo"); }));
  $$(".ex", root).forEach((c) => (c.onclick = () => { S.focus = +c.dataset.i; markFocus(); }));
}
function renderTodo() {
  const el = $("#todoList");
  if (!S.run) { el.innerHTML = `<div class="card empty">${t("noRunList")}</div>`; return; }
  const list = todo();
  if (!list.length) { el.innerHTML = `<div class="card empty">${t("noTodo")}</div>`; return; }
  S.focus = Math.min(S.focus, list.length - 1);
  const firstOpt = list.findIndex((it) => !it.must);
  el.innerHTML = list.map((it, i) => (i === firstOpt ? `<h4 class="grouphead">${t("optionalHead")}</h4>` : "") + itemCard(it, i, "todo")).join("");
  bindCards(el, "todo");
}
function renderDone() {
  const el = $("#doneList");
  const list = done();
  el.innerHTML = list.length ? list.map((it, i) => itemCard(it, i, "done")).join("") : `<div class="card empty">${t("noDone")}</div>`;
  bindCards(el, "done");
}
function markFocus() {
  const pane = S.tab === "done" ? "#doneList" : "#todoList";
  $$(pane + " .ex").forEach((c) => c.classList.toggle("focus", +c.dataset.i === S.focus));
  const f = $(`${pane} .ex[data-i="${S.focus}"]`);
  if (f) f.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function renderDocs() {
  const docs = S.inbox.docs.filter((d) => S.client === "ALL" || d.client_id === S.client);
  const byDoc = {};
  items().forEach((it) => (byDoc[it.doc_id] = it));
  $("#docGrid").innerHTML = docs.map((d) => {
    const it = byDoc[d.doc_id];
    const st = S.review.status?.[d.doc_id];
    const label = it?.dec ? t("d_" + it.dec.decision) : st ? t("st_" + st) : "";
    const ex = it ? it.exs.map((e) => ruleShort(e.rule_id)).join("、") : S.run ? t("noEx") : "";
    return `<div class="card doc ${it && it.must && !it.dec ? "flag" : ""}" data-open="${esc(d.doc_id)}">
      <img src="img/${esc(d.image)}" alt="" loading="lazy"><div class="dn">${esc(d.doc_id)}</div>
      <div class="ds">${label ? `<b>${esc(label)}</b> · ` : ""}<span class="${it ? "" : "muted"}">${esc(ex)}</span></div></div>`;
  }).join("");
  $$("#docGrid .doc").forEach((el) => (el.onclick = () => openDoc(el.dataset.open, null)));
}

// ---------- voucher viewer: big image, the issues, the four decisions, next / previous ----------
function openDoc(docId, mode) {
  // an e-invoice from the platform download has no photo: it opens with its reasons and the decision buttons only
  const it0 = items().find((x) => x.doc_id === docId);
  const d = docById(docId) || S.inbox.docs.find((x) => x.doc_id === docId) || (it0 && { doc_id: docId, client_id: it0.client_id, platform: true });
  if (!d) return;
  const list = mode === "todo" ? todo() : mode === "done" ? done() : null;
  S.view = { docId, mode, ids: list ? list.map((it) => it.doc_id) : [docId] };
  $("#viewTitle").textContent = `${clientName(d.client_id)} · ${docId}`;
  const img = $("#viewImg");
  // drop the previous voucher's load handler first: left in place, it fires when this image loads and draws that
  // voucher's field boxes on top of this one
  img.onload = null;
  $("#imgBox").hidden = !!d.platform;
  $("#viewNoImg").hidden = !d.platform;
  $("#viewNoImg").textContent = d.platform ? t("einvNoPhoto") : "";
  if (d.platform) img.removeAttribute("src"); else img.src = `img/${d.image}`;
  const it = items().find((x) => x.doc_id === docId);
  const exs = it ? it.exs : [];
  const want = new Set(exs.flatMap((e) => (e.evidence || []).map((v) => v.field)));
  const box = $("#imgBox");
  box.querySelectorAll(".hl").forEach((h) => h.remove());
  const draw = () => {
    if (S.view?.docId !== docId) return;
    box.querySelectorAll(".hl").forEach((h) => h.remove());
    if (!d.boxes || !d.image_size) return;
    const [W, H] = d.image_size;
    Object.entries(d.boxes).forEach(([f, b]) => {
      if (want.size && !want.has(f)) return;
      const el = document.createElement("div");
      el.className = "hl";
      el.style.left = (b[0] / W) * 100 + "%"; el.style.top = (b[1] / H) * 100 + "%";
      el.style.width = ((b[2] - b[0]) / W) * 100 + "%"; el.style.height = ((b[3] - b[1]) / H) * 100 + "%";
      el.innerHTML = `<em>${esc(t("f_" + f))}</em>`;
      box.appendChild(el);
    });
  };
  if (img.complete && img.naturalWidth) draw(); else img.onload = draw;
  $("#viewEx").innerHTML = exs.length
    ? exs.map((e) => `<div class="vex"><b>${esc(ruleTitle(e))}</b><br>${esc(ruleWhy(e))}</div>`).join("")
    : `<p class="muted">${S.run ? t("viewNone") : t("viewNotRun")}</p>`;
  $("#viewDecide").innerHTML = it ? (it.dec
    ? `<div class="decided"><span class="dlabel d-${esc(it.dec.decision)}">${t("d_" + it.dec.decision)}</span><span class="muted small">${t("decidedBy", { who: esc(it.dec.user || ""), when: ago(tsMs(it.dec.ts)) })}</span><button class="linkbtn" data-undo="${esc(docId)}">${t("undo")}</button></div>`
    : decisionButtons(it) + `<p class="sugline">${it.suggest ? t("suggest", { d: t("d_" + it.suggest) }) : t("suggestPaper")}</p>`) : "";
  $$("#viewDecide .dbtn").forEach((b) => (b.onclick = () => decideInViewer(b.dataset.d)));
  $$("#viewDecide [data-undo]").forEach((b) => (b.onclick = () => { decide(docId, "undo"); openDoc(docId, mode === "done" ? "todo" : mode); }));
  const f = d.fields || {}, p = d.provenance || {};
  const keys = ["doc_type", "invoice_no", "date", "invoice_period", "seller_name", "seller_ubn", "buyer_name", "buyer_ubn", "items", "net", "tax", "total", "note", "category"];
  $("#viewFields").innerHTML = d.fields ? keys.map((key) => {
    let v = f[key];
    if (key === "items") v = (v || []).map((i) => `${i.desc} x${i.qty ?? "?"} = ${i.amount ?? "?"}`).join("; ");
    if (key === "doc_type" && v) v = DOC_TYPE[S.lang]?.[v] || v;
    const pv = p[key] ? `<span class="prov ${GOOD.has(p[key]) ? "good" : "weak"}" title="${esc(t("provTip", { p: p[key] }))}">${GOOD.has(p[key]) ? t("good") : t("weak")}</span>` : "";
    return `<tr><td>${t("f_" + key)}</td><td>${esc(v ?? "")} ${pv}</td></tr>`;
  }).join("") : `<tr><td class="muted">${t(d.platform ? "einvFields" : "notRead")}</td></tr>`;
  const k = S.view.ids.indexOf(docId);
  $("#viewPos").textContent = S.view.ids.length > 1 ? `${k + 1} / ${S.view.ids.length}` : "";
  $("#viewPrev").disabled = k <= 0; $("#viewNext").disabled = k < 0 || k >= S.view.ids.length - 1;
  if (!$("#viewer").open) $("#viewer").showModal();
}
function decideInViewer(d) {
  const v = S.view;
  if (!v) return;
  const k = v.ids.indexOf(v.docId);
  decide(v.docId, d);
  const rest = v.ids.slice(k + 1).concat(v.ids.slice(0, k)).filter((id) => !S.review.decisions[id]);
  if (rest.length) openDoc(rest[0], v.mode); else $("#viewer").close();
}
function stepView(dir) {
  const v = S.view;
  if (!v) return;
  const k = v.ids.indexOf(v.docId) + dir;
  if (k >= 0 && k < v.ids.length) openDoc(v.ids[k], v.mode);
}

function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if ($("#viewer").open) {
    if (k === "j" || k === "arrowright") stepView(1);
    else if (k === "k" || k === "arrowleft") stepView(-1);
    else if (["1", "2", "3", "4"].includes(k) && !S.review.decisions[S.view?.docId]) decideInViewer(DECISIONS[+k - 1]);
    else return;
    e.preventDefault();
    return;
  }
  if ($("#confirm").open || $("#login").open || (S.tab !== "todo" && S.tab !== "done")) return;
  const list = S.tab === "todo" ? todo() : done();
  if (!list.length) return;
  if (k === "j" || k === "arrowdown") S.focus = Math.min(S.focus + 1, list.length - 1);
  else if (k === "k" || k === "arrowup") S.focus = Math.max(S.focus - 1, 0);
  else if (k === "enter" || k === "o") { openDoc(list[S.focus].doc_id, S.tab); e.preventDefault(); return; }
  else if (S.tab === "todo" && ["1", "2", "3", "4"].includes(k)) { decide(list[S.focus].doc_id, DECISIONS[+k - 1]); e.preventDefault(); return; }
  else if (S.tab === "done" && k === "u") { decide(list[S.focus].doc_id, "undo"); e.preventDefault(); return; }
  else return;
  e.preventDefault();
  markFocus();
}

function setTab(x) {
  S.tab = x; store.set("tab", x); S.focus = 0;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === x));
  ["todo", "done", "documents", "log"].forEach((p) => ($("#tab-" + p).hidden = p !== x));
  if (x === "log") renderLog();
  else if (S.inbox) renderAll();
}

// ---------- logs ----------
async function renderLog() {
  if (!S.runId) { $("#logHead").innerHTML = `<p class="muted">${t("logNone")}</p>`; $("#logTable").innerHTML = ""; return; }
  if (S.me && S.me.role !== "admin") {
    $("#logHead").innerHTML = `<p class="muted">${t("logAdmin")}</p>`;
    $("#logTable").innerHTML = ""; $("#opHead").innerHTML = ""; $("#opTable").innerHTML = ""; return;
  }
  const L = await j(`api/audit-log?run=${encodeURIComponent(S.runId)}${um()}`);
  $("#logHead").innerHTML = verifyBox(L) + `<p class="muted small">${t("logExplain")}</p>`;
  renderOplog();
  $("#logTable").innerHTML = `<tr><th>${t("lt_seq")}</th><th>${t("lt_time")}</th><th>${t("lt_event")}</th><th>${t("lt_doc")}</th><th>${t("lt_detail")}</th><th>${t("lt_hash")}</th></tr>` +
    L.entries.map((e) => `<tr><td>${e.seq}</td><td title="${esc(e.ts || "")}">${esc(ago(tsMs(e.ts)))}</td><td>${esc(e.event)}</td>
      <td class="mono">${esc(e.doc_id || "")}</td>
      <td>${esc(e.rule_id || (e.event === "doc_read" ? `${e.model || ""} ${e.device || ""}${e.cached ? " (cached)" : ""}` : e.event === "run_start" ? e.network : e.event === "run_end" ? `${e.n_exceptions}` : ""))}</td>
      <td class="h">${esc((e.hash || "").slice(0, 12))}</td></tr>`).join("");
}
function verifyBox(L) {
  const n = L.entries_verified;
  const what = !L.chain_ok ? t("v_broken", { n }) : L.mac_ok === false ? t("v_mac", { n }) : L.tail_ok === false ? t("v_tail")
    : L.unsigned ? t("v_unsigned", { n }) : L.mac_ok ? t("v_ok", { n }) : t("v_nokey", { n });
  const ok = L.verified;
  return `<div class="verify ${ok ? "ok" : L.unsigned && L.chain_ok ? "warn" : "bad"}"><svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>${ok ? '<path d="M9 12l2 2 4-4"/>' : ""}</svg>${esc(what)}</div>`;
}
async function renderOplog() {
  let L;
  try { L = await j("api/oplog"); } catch (e) { $("#opHead").innerHTML = ""; $("#opTable").innerHTML = ""; return; }
  $("#opHead").innerHTML = verifyBox(L);
  $("#opTable").innerHTML = `<tr><th>${t("lt_seq")}</th><th>${t("lt_time")}</th><th>${t("lt_event")}</th><th>${t("lt_user")}</th><th>${t("lt_detail")}</th><th>${t("lt_hash")}</th></tr>` +
    L.entries.slice().reverse().slice(0, 200).map((e) => `<tr><td>${e.seq}</td><td title="${esc(e.ts || "")}">${esc(ago(tsMs(e.ts)))}</td>
      <td>${esc(e.event)}</td><td>${esc(e.user || "")}</td><td class="mono">${esc([e.decision, e.doc_id, e.run_id || e.inbox || e.host || e.path].filter(Boolean).join(" · "))}</td>
      <td class="h">${esc((e.hash || "").slice(0, 12))}</td></tr>`).join("");
}

// ---------- checks: confirm, start, follow, resume ----------
function ask(title, html, okLabel) {
  return new Promise((resolve) => {
    const dlg = $("#confirm");
    $("#cfTitle").textContent = title; $("#cfBody").innerHTML = html; $("#cfOk").textContent = okLabel;
    dlg.onclose = () => resolve(dlg.returnValue === "ok");
    dlg.returnValue = "";
    dlg.showModal();
    $("#cfOk").focus();
  });
}
async function confirmRun() {
  const n = S.inbox.docs.length;
  const per = S.run?.summary?.mean_read_s && S.runId?.endsWith("-live") ? S.run.summary.mean_read_s : null;
  const html = `<p>${t("confirmRunBody", { n, m: S.inbox.clients.length })}</p>` +
    (per ? `<p>${t("confirmRunEta", { min: Math.max(1, Math.ceil((n * per) / 60)), s: per })}</p>` : "") +
    `<p class="muted">${t("confirmRunLeave")}</p>`;
  if (await ask(t("confirmRunTitle"), html, t("start"))) startRun({ live: true });
}
async function startRun(opt) {
  S.jobEnd = null;
  try {
    const job = await post("api/run", { set: S.set, ...opt });
    followJob(job);
  } catch (e) {
    if (e.status === 409 && e.body?.job) followJob(e.body.job);   // already running: join it
    else toast(e.message);
  }
}
async function checkJobs() {
  try {
    const r = await j(`api/jobs?set=${encodeURIComponent(S.set)}`);
    S.interrupted = r.interrupted || [];
    if (r.active && (!S.job || S.job.id !== r.active.id)) followJob(r.active);
    else renderAll();
  } catch (e) { /* offline: the bar says so */ }
}
let following = null;
async function followJob(job) {
  S.job = job; S.interrupted = [];
  following = job.id;
  renderAll();
  let wait = 600;
  while (following === job.id) {
    await sleep(wait);
    let s;
    try {
      s = await j("api/job/" + job.id);
      wait = 600;
    } catch (e) {
      if (e.status === 404) {          // the server restarted: the check stopped; offer to resume it
        S.job = null; following = null; await checkJobs(); return;
      }
      wait = Math.min(wait * 2, 8000); // no connection: keep trying, more slowly
      continue;
    }
    if (s.set !== S.set) { S.job = null; following = null; return; }
    S.job = s;
    if (s.status === "done") {
      S.job = null; following = null; S.jobEnd = s;
      const runs = await j("api/runs");
      S.runs = runs.filter((r) => r.startsWith(S.set + "-"));
      await loadRun(s.run_id); status();
      S.jobEnd = s; renderAll();
      return;
    }
    if (s.status === "error") {
      S.job = null; following = null; S.jobEnd = s; await checkJobs(); return;
    }
    renderSteps(); renderStage();
  }
}

// ---------- export ----------
async function confirmExport() {
  const its = items();
  const c = { ready: 0, nonded: 0, exclude: 0, defer: 0, notin: 0, hold: 0 };
  const decided = S.review.decisions || {};
  Object.entries(S.review.status || {}).forEach(([doc, st]) => {
    const d = decided[doc]?.decision;
    if (d === "exclude") c.exclude++;
    else if (d === "defer") c.defer++;
    else if (d === "nondeductible") { c.ready++; c.nonded++; }
    else if (d === "confirm" || st === "READY") c.ready++;
    else if (st === "HOLD") c.hold++;
    else c.notin++;
  });
  const row = (k, v, cls = "") => `<tr class="${cls}"><td>${k}</td><td class="num">${v}</td></tr>`;
  const ex = lastExport();
  const html = `<table class="counts">${row(t("expReady"), c.ready)}${c.nonded ? row("　" + t("expNonded"), c.nonded, "muted") : ""}${row(t("expExclude"), c.exclude)}${row(t("expDefer"), c.defer)}${row(t("expNotIn"), c.notin)}${c.hold ? row(t("expHold"), c.hold, "hot") : ""}</table>
    <p class="muted small">${t("expNote")}</p>${ex ? `<p class="warnline small">${t("expAgain", { who: esc(ex.user), when: ago(tsMs(ex.ts)) })}</p>` : ""}
    <p class="small"><a href="api/export-summary?run=${encodeURIComponent(S.runId)}">${t("expSummary")}</a></p>`;
  if (!(await ask(t("confirmExpTitle"), html, t("expGo")))) return;
  location.href = `api/export?run=${encodeURIComponent(S.runId)}${um()}`;
  setTimeout(async () => { await loadReview(); renderAll(); }, 1500);
}

init();
