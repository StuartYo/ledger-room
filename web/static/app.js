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
  client: "ALL", me: null, unmasked: null,
  job: null, jobEnd: null, interrupted: [], focus: 0, view: null,
  outbox: store.get("outbox", []), offline: false,
  page: ["queue", "documents", "log"].includes(store.get("page", "queue")) ? store.get("page", "queue") : "queue",
  screen: null, qtab: ["done", "agent"].includes(store.get("qtab", "todo")) ? store.get("qtab") : "todo", group: !!store.get("group", false),
  q: "", collapsed: new Set(), allFields: false, zoom: 1, whyOpen: new Set(), last: null, status: null,
  // review screen: the left list folded to a strip (remembered; folded by default on narrow screens), tight field boxes
  miniHide: store.get("miniHide", window.innerWidth < 1600), tb: {}, editing: null,
  agentOpen: false,       // the review panel's "how it was checked" section: one line until clicked
};
S.screen = S.page;
const DECISIONS = ["confirm", "nondeductible", "exclude", "defer"];
const GOOD = new Set(["ocr-confirmed", "ocr-confirmed (second reader)", "snapped", "ocr-zoom", "ocr-role", "ocr", "ocr-keyword", "person", "agent-verified"]);
// fields a person can correct on the review screen (ledger_room/corrections.py EDITABLE)
const EDITABLE = ["invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total"];
// a field a READ_REVIEW reason sends to a person is never labelled "two sources agree", whatever its provenance:
// otherwise the reason says "read from one source only" and the table under it says the opposite
const heldFields = (exs) => new Set((exs || []).filter((e) => e.rule_id === "READ_REVIEW")
  .flatMap((e) => [...(e.evidence_fields || []), ...(e.evidence || []).map((v) => v.field)]));
const provOk = (field, prov, held) => GOOD.has(prov) && !held.has(field);
const FIELD_KEYS = ["doc_type", "invoice_no", "date", "invoice_period", "seller_name", "seller_ubn", "buyer_name", "buyer_ubn", "items", "net", "tax", "total", "note", "category"];
const FIELD_CORE = ["invoice_no", "date", "seller_name", "seller_ubn", "buyer_ubn", "items", "net", "tax", "total"];
const AMOUNT = ["net", "tax", "total"];
const fmtVal = (f, v) => (v == null || v === "" ? t("fEmpty") : AMOUNT.includes(f) ? nt(v) : String(v));

// ---------- icons (Lucide, ISC licence; paths copied) ----------
const ICONS = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  panelLeft: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
  userCheck: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="m16 11 2 2 4-4"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>', chevronRight: '<path d="m9 18 6-6-6-6"/>', chevronLeft: '<path d="m15 18-6-6 6-6"/>',
  arrowLeft: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
  zoomIn: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/>',
  zoomOut: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  shieldAlert: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  logOut: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  eye: '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
  scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/>',
  receipt: '<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z"/><path d="M14 8H8"/><path d="M16 12H8"/><path d="M13 16H8"/>',
  wifiOff: '<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  play: '<path d="M6 3l14 9-14 9V3z"/>', refresh: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
};
const icon = (n, s = 16) => `<svg class="ic" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n] || ""}</svg>`;
const applyIcons = (root = document) => $$("[data-ic]", root).forEach((el) => { if (!el.dataset.icDone) { el.insertAdjacentHTML("afterbegin", icon(el.dataset.ic, el.classList.contains("rb") ? 18 : 16)); el.dataset.icDone = 1; } });
const kbd = (k) => k.split(" ").map((x) => `<kbd>${esc(x)}</kbd>`).join("");

// ---------- language ----------
function t(key, v = {}) {
  const s = (I18N[S.lang] && I18N[S.lang][key]) ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (v[k] ?? ""));
}
function applyLang() {
  document.documentElement.lang = S.lang === "zh" ? "zh-Hant" : "en";
  $$("[data-i18n]").forEach((el) => (el.innerHTML = t(el.dataset.i18n)));
  $$("[data-i18n-title]").forEach((el) => (el.title = t(el.dataset.i18nTitle)));
  $$("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
  $$("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  $("#langBtn").textContent = t("lang");
}
const RT = (r) => RULES_TEXT[r]?.[S.lang] || RULES_TEXT[r]?.en;
const ruleVerdict = (r) => RT(r)?.v || r;
const ruleTitle = (e) => (S.lang === "zh" && RULES_TEXT[e.rule_id]?.zh?.title) || e.title;
const ruleWhy = (e) => (S.lang === "zh" && RULES_TEXT[e.rule_id]?.zh?.why) || e.message;
const LAW_EN = { G0340080: "VAT Act", G0340081: "VAT Enforcement Rules", G0340051: "Income Tax Audit Regulations", G0340003: "Income Tax Act" };
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
// the law in a few words, for a group header: "VAT Act §19" / "營業稅法第 19 條"
function lawShort(e) {
  const c = (e?.citations || []).find((x) => x.url);
  if (!c) return "";
  const m = c.url.match(/pcode=(\w+)&flno=([\w-]+)/);
  if (m) return S.lang === "zh" ? (LAW_ZH[m[1]] ? `${LAW_ZH[m[1]]}第 ${m[2]} 條` : "") : (LAW_EN[m[1]] ? `${LAW_EN[m[1]]} §${m[2]}` : "");
  if (c.url.includes("fia.gov.tw")) return S.lang === "zh" ? "財政部檢查碼規則" : "MOF check-digit rule";
  return "";
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
// "12 min ago" for a run id that carries its time; the id itself for one that does not (an evaluation run)
const runLabel = (id) => (isNaN(runTime(id)) ? id : ago(runTime(id)));
const um = () => (S.unmasked && S.unmasked === S.runId ? "&unmask=1" : "");
const nt = (n) => (n == null || n === "" || isNaN(+n) ? "—" : "NT$" + Number(n).toLocaleString("en-US"));
const shortName = (n) => String(n || "").replace(/(股份)?有限公司$/, "");

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
// a short message at the bottom; with an undo function it carries an Undo button (Z does the same)
function toast(msg, undo) {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(msg)}</span>` + (undo ? `<button class="tbtn" id="toastUndo">${icon("undo", 14)}${t("undo")}<kbd>Z</kbd></button>` : "");
  el.hidden = false;
  if (undo) $("#toastUndo").onclick = () => { undo(); el.hidden = true; };
  clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), undo ? 8000 : 5000);
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
  const b = $("#userBtn");
  b.textContent = (me.user || "?").slice(0, 2).toUpperCase();
  b.title = me.user ? t("roleOf", { u: me.user, r: me.role }) : "";
  b.hidden = !me.user;
  showMask();
}

// Personal data is masked by default (safeguard regulation Art. 17). A role allowed to unmask gives a reason, which
// the server writes to the operation log; the grant covers one check for ten minutes.
const isShown = () => !!(S.unmasked && S.unmasked === S.runId);
const canUnmask = () => !!(S.me && S.me.can_unmask && S.runId);
function showMask() {
  let tag = $("#maskTag");
  if (!tag) { $("#synthTag").insertAdjacentHTML("afterend", '<button class="tag warn" id="maskTag" hidden></button>'); tag = $("#maskTag"); tag.onclick = toggleUnmask; }
  tag.hidden = !isShown();
  tag.innerHTML = icon("eye", 13) + esc(t("shown"));
  tag.title = t("remask");
  if (!$("#setPop").hidden) renderSettings();
  if (!$("#statusPop").hidden) renderStatusPop();
}
async function toggleUnmask() {
  closePops();
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

// ---------- popovers: status, account, settings ----------
function closePops() { $$(".pop").forEach((p) => (p.hidden = true)); }
function openPop(id, anchor, side) {
  const p = $(id), was = !p.hidden;
  closePops();
  if (was) return false;
  p.hidden = false;
  const r = anchor.getBoundingClientRect();
  if (side === "right") { p.style.left = r.right + 8 + "px"; p.style.top = ""; p.style.bottom = window.innerHeight - r.bottom + "px"; p.style.right = ""; }
  else { p.style.top = r.bottom + 6 + "px"; p.style.right = Math.max(8, window.innerWidth - r.right) + "px"; p.style.left = ""; p.style.bottom = ""; }
  return true;
}
function maskBlock() {
  const shown = isShown();
  return `<div class="prow"><div class="ph2">${icon(shown ? "eye" : "eyeOff", 15)}<b>${esc(shown ? t("shown") : t("masked"))}</b></div>
    <p>${esc(t("maskTip"))}</p>${canUnmask() ? `<button class="btn ghost sm" data-unmask title="${esc(t("unmaskTip"))}">${esc(shown ? t("remask") : t("unmask"))}</button>` : ""}</div>`;
}
function bindPop(p) { $$("[data-unmask]", p).forEach((b) => (b.onclick = toggleUnmask)); }
function modelLine() {
  const s = S.status;
  if (!s) return t("modelLoading");
  return (s.model_loaded ? t("modelOn", { m: s.model, d: s.device }) : t("modelLater")) + (s.strict ? ` · ${t("strict")}` : "");
}
// the review agent on this machine (server agent_status): on, waiting for the first check, or not here at all
function agentLine() {
  const a = S.status?.agent;
  if (!a) return "";
  if (!a.available) return t("agentOff");
  const mem = a.memory_gb ? ` · ${t("agentMem", { g: a.memory_gb })}` : "";
  return (a.loaded ? t("agentOn", { m: a.model, d: a.device }) : t("agentLater", { m: a.model })) + mem;
}
function renderStatusPop() {
  const p = $("#statusPop");
  const al = agentLine();
  p.innerHTML = `<div class="prow"><div class="ph2"><span class="dot"></span><b>${esc(t("statusLocal"))}</b></div><p>${esc(t("localTip"))}</p><p class="mono">${esc(modelLine())}</p>${al ? `<p class="mono">${esc(al)}</p>` : ""}</div>` + maskBlock();
  bindPop(p);
}
function renderUserPop() {
  const me = S.me || {}, p = $("#userPop");
  p.innerHTML = `<div class="prow"><div class="ph2"><b>${esc(me.user || "")}</b><span class="muted">${esc(me.role || "")}</span></div></div>` +
    (me.local ? "" : `<button class="menuitem" id="logoutBtn">${icon("logOut", 15)}${esc(t("logout"))}</button>`);
  $("#logoutBtn") && ($("#logoutBtn").onclick = logout);
}
function renderSettings() {
  const me = S.me || {}, p = $("#setPop");
  p.innerHTML = `<div class="prow"><div class="lbl">${esc(t("langName"))}</div><div class="seg">
      <button data-lang="zh" class="${S.lang === "zh" ? "on" : ""}">中文</button><button data-lang="en" class="${S.lang === "en" ? "on" : ""}">English</button></div></div>
    <div class="prow"><div class="lbl">${esc(t("privacy"))}</div></div>${maskBlock()}
    <div class="prow"><div class="lbl">${esc(t("account"))}</div><div class="ph2"><b>${esc(me.user || "")}</b><span class="muted">${esc(me.role || "")}</span></div>
      ${me.local || !me.user ? "" : `<button class="btn ghost sm" id="logoutBtn2">${icon("logOut", 14)}${esc(t("logout"))}</button>`}</div>
    <p class="foot">${esc(t("foot"))}</p>`;
  $$("[data-lang]", p).forEach((b) => (b.onclick = () => setLang(b.dataset.lang)));
  $("#logoutBtn2") && ($("#logoutBtn2").onclick = logout);
  bindPop(p);
}
async function logout() { await fetch("api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); location.reload(); }
function setLang(l) {
  S.lang = l; store.set("lang", l);
  $("#toast").hidden = true;
  applyLang(); showUser(); renderStatus(); renderAll();
  if (!$("#setPop").hidden) renderSettings();
  if (S.screen === "log") renderLog();
}

// ---------- start-up ----------
async function init() {
  applyIcons(); applyLang();
  S.me = await (await fetch("api/me")).json();
  if (!S.me.user) await login();
  showUser();
  $("#langBtn").onclick = () => setLang(S.lang === "zh" ? "en" : "zh");
  $("#statusBtn").onclick = (e) => { e.stopPropagation(); renderStatusPop(); openPop("#statusPop", $("#statusBtn")); };
  $("#userBtn").onclick = (e) => { e.stopPropagation(); renderUserPop(); openPop("#userPop", $("#userBtn")); };
  $("#navSettings").onclick = (e) => { e.stopPropagation(); renderSettings(); openPop("#setPop", $("#navSettings"), "right"); };
  document.addEventListener("click", (e) => { if (!e.target.closest(".pop")) closePops(); });
  $$("[data-nav]").forEach((b) => (b.onclick = () => go(b.dataset.nav)));
  $("#navExport").onclick = () => confirmExport();
  $$("[data-qtab]").forEach((b) => (b.onclick = () => { S.qtab = b.dataset.qtab; store.set("qtab", S.qtab); S.focus = 0; renderAll(); }));
  $$("[data-group]").forEach((b) => (b.onclick = () => setGroup(b.dataset.group === "1")));
  $("#search").addEventListener("input", (e) => { S.q = e.target.value.trim(); S.focus = 0; renderAll(); });
  $("#revBack").onclick = closeReview;
  $("#viewPrev").onclick = () => stepView(-1);
  $("#viewNext").onclick = () => stepView(1);
  $("#zIn").onclick = () => zoom(1.25); $("#zOut").onclick = () => zoom(0.8); $("#zFit").onclick = () => zoom(0);
  $("#boxToggle").onclick = toggleFields; $("#fieldToggle").onclick = toggleFields;
  $("#miniToggle").onclick = toggleMini;
  applyMini();
  window.addEventListener("resize", () => { if (S.screen === "review") { layoutPaper(); redrawBoxes(); } });
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
  go(S.page);
  if (first) await loadInbox(first);
  flush();
}
function renderInboxSelect(value) {
  const sel = $("#inbox");
  sel.innerHTML = S.boxes.map((b) => `<option value="${esc(b.set)}">${esc(b.name?.[S.lang] || b.set)}</option>`).join("");
  if (value) sel.value = value;
}
async function status() {
  try { S.status = await j("api/status"); } catch (e) { S.status = null; }
  renderStatus();
}
// "Intel(R) Arc(TM) Pro B70 Graphics (dGPU)" -> "Arc Pro B70"
const hwShort = (s) => String(s || "").replace(/\((R|TM|C)\)/gi, "").replace(/\(.*?\)/g, "").replace(/^Intel\s+/i, "").replace(/\s+Graphics\b/i, "").replace(/\s+/g, " ").trim();
function renderStatus() {
  const hw = hwShort(S.status?.hardware);
  $("#statusText").textContent = t("statusLocal") + (hw ? ` · ${hw}` : "");
  $("#statusBtn").title = `${t("localTip")}\n${modelLine()}${agentLine() ? "\n" + agentLine() : ""}`;
}

async function loadInbox(set) {
  S.set = set; S.client = "ALL"; S.run = null; S.runId = null; S.jobEnd = null; S.focus = 0; S.collapsed.clear(); S.last = null;
  S.review = { decisions: {}, exports: [], status: {}, blocking: [] };
  if (S.screen === "review") { S.screen = S.page; S.view = null; }
  S.inbox = await j(`api/inbox?set=${encodeURIComponent(set)}`);
  $("#synthTag").hidden = !S.inbox.synthetic;
  const runs = await j("api/runs");
  S.runs = runs.filter((r) => r.startsWith(set + "-"));
  const pick = S.runs[0] || (runs.includes("eval-" + set) ? "eval-" + set : null);
  if (pick) await loadRun(pick); else renderAll();
  await checkJobs();
}
async function loadRun(runId) {
  S.runId = runId;
  S.run = await j(`api/result?run=${encodeURIComponent(runId)}${um()}`);
  S.runGen = (S.runGen || 0) + 1;
  await loadReview();
  renderAll();
  showMask();
  if (S.screen === "log") renderLog();
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
const SEV = { high: 0, hold: 1, review: 2, medium: 3, warning: 4 };
function clientName(id) { return S.inbox?.clients.find((c) => c.id === id)?.name || id; }
function docById(id) { return S.run?.docs.find((d) => d.doc_id === id); }
const evValue = (exs, f) => exs.flatMap((e) => e.evidence || []).find((v) => v.field === f)?.value;
let _items = null, _itemsKey = null;
function items() {
  // cached until the run, the review state or the language changes (renderAll calls this many times)
  const key = [S.runId, S.runGen, S.lang, JSON.stringify(S.review.decisions), JSON.stringify(S.review.status)].join("|");
  if (_items && _itemsKey === key) return _items;
  const by = new Map();
  for (const e of allExceptions()) {
    if (e.severity === "info") continue;
    if (!by.has(e.doc_id)) by.set(e.doc_id, { doc_id: e.doc_id, client_id: e.client_id, exs: [], einv: e.source === "e-invoice" });
    by.get(e.doc_id).exs.push(e);
  }
  const blocking = new Set(S.review.blocking || []);
  const rank = (e) => (blocking.has(e.rule_id) || e.severity === "hold" ? 0 : 10) + (SEV[e.severity] ?? 5);
  _items = [...by.values()].map((it) => {
    const st = S.review.status?.[it.doc_id];
    it.exs.sort((a, b) => rank(a) - rank(b));
    it.primary = it.exs[0];
    it.status = st;
    it.must = st ? st === "HOLD" : it.exs.some((e) => blocking.has(e.rule_id) || e.severity === "hold");
    it.dec = S.review.decisions?.[it.doc_id] || null;
    it.suggest = suggestFor(it.primary);              // always one of the four keys
    it.paper = it.exs.some((e) => PAPER_FIRST.has(e.rule_id));
    const d = docById(it.doc_id), f = d?.fields || {};
    it.doc = d;
    it.inv = f.invoice_no || evValue(it.exs, "invoice_no") || null;
    it.total = f.total ?? evValue(it.exs, "total");
    it.tax = f.tax ?? evValue(it.exs, "tax");
    it.type = f.doc_type;
    return it;
  });
  _itemsKey = key;
  return _items;
}
const itemMap = () => new Map(items().map((it) => [it.doc_id, it]));
// ---------- the review agent (ledger_room/agent.py): a 27B with tools proposes, code verifies, a person can override ----------
// a voucher the agent cleared: every value it changed passed the code check, the rules found nothing left, nobody undid it
const agentOk = (d) => d?.agent?.status === "verified" && !d.agent.overridden;
// "total NT$1,150 -> NT$1,155", or "confirmed seller UBN" when it only confirmed what was read
function agentChanges(a, sep) {
  return Object.entries(a?.checks || {}).filter(([, c]) => c.accepted).map(([k, c]) => (String(c.old ?? "") === String(c.value ?? "")
    ? t("agConfirmF", { f: t("f_" + k) }) : `${t("f_" + k)} ${fmtVal(k, c.old)} → ${fmtVal(k, c.value)}`)).join(sep || (S.lang === "zh" ? "・" : " · "));
}
const agentHow = (a) => [...new Set(Object.values(a?.checks || {}).filter((c) => c.accepted).flatMap((c) => c.evidence || []))].map((e) => t("agEvS_" + e)).join(S.lang === "zh" ? "、" : ", ");
// vouchers the agent cleared that are not in the queue (a cleared one with a warning left stays "worth a look")
function agentItems() {
  if (!S.run) return [];
  const im = itemMap(), q = S.q.toLowerCase();
  return S.run.docs.filter((d) => agentOk(d) && !im.has(d.doc_id) && (S.client === "ALL" || d.client_id === S.client)
      && (!q || [clientName(d.client_id), d.client_id, d.doc_id, d.fields?.invoice_no].some((x) => String(x || "").toLowerCase().includes(q))))
    .map((d) => ({ doc_id: d.doc_id, client_id: d.client_id, doc: d, agent: true, inv: d.fields?.invoice_no, total: d.fields?.total, tax: d.fields?.tax, type: d.fields?.doc_type }))
    .sort((a, b) => a.client_id.localeCompare(b.client_id) || a.doc_id.localeCompare(b.doc_id));
}
function sortItems(list) {
  return list.sort((a, b) => (b.must - a.must) || (SEV[a.primary.severity] ?? 5) - (SEV[b.primary.severity] ?? 5)
    || a.client_id.localeCompare(b.client_id) || a.doc_id.localeCompare(b.doc_id));
}
function matches(it) {
  if (!S.q) return true;
  const q = S.q.toLowerCase();
  return [clientName(it.client_id), it.client_id, it.doc_id, it.inv].some((x) => String(x || "").toLowerCase().includes(q));
}
const inFilter = (it) => (S.client === "ALL" || it.client_id === S.client) && matches(it);
const todo = () => sortItems(items().filter((it) => !it.dec && inFilter(it)));
const done = () => sortItems(items().filter((it) => it.dec && inFilter(it)));
const pendingMust = () => items().filter((it) => it.must && !it.dec);
const undecided = () => items().filter((it) => !it.dec);

// the queue as groups: by priority (must decide, worth a look) or by the main reason each voucher was held
function groups() {
  if (S.qtab === "agent") return [{ key: "all", items: agentItems() }];
  const list = S.qtab === "todo" ? todo() : done();
  if (!S.group) {
    if (S.qtab === "done") return [{ key: "all", items: list }];
    const g = [{ key: "must", label: t("grpMust"), must: true, items: list.filter((it) => it.must) },
      { key: "look", label: t("grpLook"), must: false, items: list.filter((it) => !it.must) }];
    return g.filter((x) => x.items.length);
  }
  const by = new Map();
  list.forEach((it) => {
    const r = it.primary.rule_id;
    if (!by.has(r)) by.set(r, { key: r, label: ruleVerdict(r), law: lawShort(it.primary), items: [], must: false });
    const g = by.get(r); g.items.push(it); g.must = g.must || it.must;
  });
  return [...by.values()].sort((a, b) => (b.must - a.must) || b.items.length - a.items.length || a.key.localeCompare(b.key));
}
const visibleRows = () => groups().flatMap((g) => (S.collapsed.has(g.key) ? [] : g.items));

// ---------- decisions: saved at once; kept in this browser until the server has them ----------
function applyLocal(o) {
  if (o.decision === "undo") delete S.review.decisions[o.doc_id];
  else S.review.decisions[o.doc_id] = { decision: o.decision, user: S.me?.user, ts: new Date(o.at).toISOString(), pending: true };
}
// decide one voucher or a whole group; the previous state of each is kept so Undo (Z) puts it back
function decide(docIds, decision, msg) {
  if (!S.runId || !docIds.length) return;
  const prev = docIds.map((id) => ({ id, d: S.review.decisions[id]?.decision || null }));
  const at = Date.now();
  docIds.forEach((id) => { const o = { run: S.runId, doc_id: id, decision, at }; S.outbox.push(o); applyLocal(o); });
  store.set("outbox", S.outbox);
  S.last = { run: S.runId, prev };
  if (msg) toast(msg, undoLast);
  renderAll();
  flush();
}
function undoLast() {
  const L = S.last;
  if (!L || L.run !== S.runId) return;
  S.last = null;
  if (L.kind === "agent") {           // take back "do not use the agent's fixes" (or the restore)
    post("api/agent-override", { run: L.run, doc_id: L.doc, action: L.action === "reject" ? "restore" : "reject" })
      .then(() => loadRun(S.runId)).then(() => toast(t("toastUndone", { n: 1 }))).catch((e) => toast(e.message));
    return;
  }
  if (L.kind === "correct") {         // put the field back to the value shown before the correction
    post("api/correct", { run: L.run, doc_id: L.doc, field: L.field, value: L.prev == null ? "" : String(L.prev) })
      .then(() => loadRun(S.runId)).then(() => toast(t("toastUndone", { n: 1 }))).catch((e) => toast(e.message));
    return;
  }
  const at = Date.now();
  L.prev.forEach(({ id, d }) => { const o = { run: S.runId, doc_id: id, decision: d || "undo", at }; S.outbox.push(o); applyLocal(o); });
  store.set("outbox", S.outbox);
  toast(t("toastUndone", { n: L.prev.length }));
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

// ---------- screens ----------
function go(page) {
  closePops();
  if (page !== "review") { S.page = page; store.set("page", page); }
  S.screen = page; S.focus = 0;
  if (page === "log") renderLog();
  renderAll();
}
function renderAll() {
  ["queue", "review", "documents", "log"].forEach((p) => ($("#v-" + p).classList.toggle("on", S.screen === p)));
  $$("[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === (S.screen === "review" ? (S.view?.mode ? "queue" : S.page) : S.screen)));
  renderKeybar();
  if (!S.inbox) return;
  renderInboxSelect(S.set);
  const p = pendingMust().length;
  document.title = S.run && p ? t("titlePending", { n: p }) : "Ledger Room";
  if (S.screen === "queue") { renderOverview(); renderQueue(); renderClients(); renderChecks(); }
  else if (S.screen === "review") renderReview();
  else if (S.screen === "documents") renderDocs();
}

function newDocs() {
  if (!S.run) return 0;
  const seen = new Set(S.run.docs.map((d) => d.doc_id));
  return S.inbox.docs.filter((d) => !seen.has(d.doc_id)).length;
}
const lastExport = () => (S.review.exports || []).slice(-1)[0];
function latestDecisionTs() {
  return Math.max(0, ...Object.values(S.review.decisions || {}).map((d) => tsMs(d.ts) || 0));
}
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
function runUser() { return S.run?.operator || S.jobEnd?.user || ""; }
// who ran the check (the signed-in account; kept in the audit log), labelled so it does not read like a stray word;
// a single local user without accounts is "local" and says nothing
function runByText() {
  const u = runUser();
  return u && !(S.me?.local && u === "local") ? esc(t("metaRunBy", { u })) : "";
}

function btn(id, label, cls = "", extra = "", ic = "", k = "") {
  return `<button class="btn ${cls}" id="${id}" ${extra}>${ic ? icon(ic, 15) : ""}<span>${label}</span>${k ? kbd(k) : ""}</button>`;
}
const adminOnly = () => (isAdmin() ? "" : `disabled title="${esc(t("onlyAdmin"))}"`);

// overview: a one-line title, one line under it, the buttons, then the period strip (or the progress of a check)
function renderOverview() {
  const st = stepState();
  let head = "", sub = "", btns = "";
  if (S.job) {
    // after the reading, the agent takes the held vouchers: its own counter and bar, not "50 of 50" standing still
    const ag = j0.status === "agent", gi = ag ? j0.agent_i || 0 : j0.i, gn = ag ? j0.agent_n || 0 : j0.n;
    const per = ag ? j0.agent_avg_s : j0.avg_s || j0.last_s, left = gn ? gn - gi : 0;
    const eta = per && left ? (left * per < 60 ? t("progEtaS") : t("progEta", { m: Math.ceil((left * per) / 60) })) : "";
    const main = j0.status === "loading model" ? t("loadingModel") : j0.status === "queued" ? t("queued")
      : ag ? t("progAgent", { i: Math.min(gi + 1, gn), n: gn }) : t("progMain", { i: j0.i, n: j0.n });
    const bits = [ag ? t("progAgentRead", { n: j0.n }) : "", eta, per ? t("progPer", { s: Math.round(per * 10) / 10 }) : "",
      !ag && j0.n_failed ? t("progFail", { n: j0.n_failed }) : "", !ag && j0.cached ? t("progCached") : ""].filter(Boolean);
    const d = S.inbox.docs.find((x) => x.doc_id === j0.doc_id);
    $("#ovTitle").textContent = t("progTitle");
    $("#ovSub").textContent = t("progLeave");
    $("#ovBtns").innerHTML = "";
    $("#periodCard").innerHTML = `<div class="progress">${d ? `<img src="img/${esc(d.image)}" alt="">` : `<div class="thumbph">${icon("scan", 20)}</div>`}
      <div class="grow"><div class="prog-main">${esc(main)}</div><div class="muted">${esc(bits.join(" · "))}</div>
      <div class="pbar"><span style="width:${gn ? (100 * gi) / gn : 2}%"></span></div></div></div>`;
    return;
  }
  const intr = S.interrupted[0];
  const runB = (label, cls = "primary") => btn("runBtn", label, cls, adminOnly(), "play");
  const recheckB = btn("runBtn", t("recheck"), "ghost", adminOnly(), "refresh");
  const expB = (locked) => btn("exportBtn", t("exportBtn"), locked ? "ghost" : "primary", "", locked ? "lock" : "download");
  if (!st.s1) { head = t("emptyTitle"); sub = t("emptyInbox", { set: esc(S.set) }); }
  else if (S.jobEnd?.status === "error") { head = t("errTitle"); sub = t("errSub", { e: esc(S.jobEnd.error || "") }); btns = runB(t("runBtn")); }
  else if (intr) {
    head = t("intTitle", { i: intr.i, n: intr.n }); sub = t("intSub", { d: intr.n_done });
    btns = btn("runBtn", t("restart"), "ghost", adminOnly()) + btn("resumeBtn", t("resume", { k: intr.n_done + 1 }), "primary", adminOnly(), "play");
  } else if (!S.run) { head = t("noRunTitle"); sub = t("noRunSub"); btns = runB(t("runBtn")); }
  else if (newDocs()) { head = t("newDocsTitle", { n: newDocs() }); sub = t("newDocsSub"); btns = runB(t("recheck")); }
  else if (undecided().length) {
    const all = undecided(), must = all.filter((it) => it.must);
    head = S.jobEnd ? t("doneTitle", { n: S.run.summary.n_docs, p: all.length }) : t("ovPending", { n: all.length });
    sub = must.length ? t("ovMust", { m: must.length }) + riskText(must) : t("ovLookOnly", { n: all.length });
    btns = recheckB + expB(!!must.length) + btn("reviewBtn", t("startReview"), "primary", "", "", "Enter");
  } else {
    const ex = lastExport();
    head = S.jobEnd && !Object.keys(S.review.decisions).length ? t("doneZero", { n: S.run.summary.n_docs }) : st.s4 ? t("exportedTitle") : t("allDoneTitle");
    sub = st.s4 ? t("exportedSub", { who: esc(ex.user), when: ago(tsMs(ex.ts)) }) : t("allDoneSub");
    btns = recheckB + expB(false);
  }
  if (S.jobEnd?.carried) sub += ` · ${t("carried", { n: S.jobEnd.carried })}`;
  $("#ovTitle").textContent = head;
  $("#ovSub").innerHTML = sub;
  $("#ovBtns").innerHTML = btns;
  $("#runBtn") && ($("#runBtn").onclick = () => confirmRun());
  $("#resumeBtn") && ($("#resumeBtn").onclick = () => startRun({ resume: intr.run_id }));
  $("#reviewBtn") && ($("#reviewBtn").onclick = startDeciding);
  $("#exportBtn") && ($("#exportBtn").onclick = () => confirmExport());
  $("#riskInfo") && ($("#riskInfo").onclick = (e) => { e.stopPropagation(); toast($("#riskInfo").title); });
  renderPeriod();
}
// the tax at stake on the must-decide vouchers: the period check's wrong-claim figure when it ran, else the tax on them
function riskText(must) {
  const pc = S.run?.period_check;
  const top = (pc?.risk_ranking || []).filter((r) => r.wrong_claim);
  const info = (extra) => ` <button class="infobtn" id="riskInfo" title="${esc(t("riskNote") + extra)}" aria-label="${esc(t("riskNote"))}">${icon("info", 14)}</button>`;
  if (top.length && S.client === "ALL") {
    const w = top.reduce((s, r) => s + (r.wrong_claim || 0), 0);
    const by = top.slice(0, 3).map((r) => `${shortName(clientName(r.client_id))} ${nt(r.wrong_claim)}`).join(" · ");
    return " · " + t("riskWrong", { t: nt(w) }) + info("\n" + by);
  }
  const tax = must.reduce((s, it) => s + (isNaN(+it.tax) || it.tax == null ? 0 : +it.tax), 0);
  return tax ? " · " + t("riskHeld", { t: nt(tax) }) + info("") : "";
}
function startDeciding() {
  const rows = visibleRows();
  const f = (S.qtab === "todo" && rows[S.focus]) || todo().find((it) => it.must) || todo()[0];
  if (f) openDoc(f.doc_id, "todo");
}
function renderPeriod() {
  if (!S.run) { $("#periodCard").innerHTML = ""; $("#periodCard").hidden = true; return; }
  $("#periodCard").hidden = false;
  const its = items();
  const ids = new Set(S.inbox.docs.map((d) => d.doc_id).concat(its.map((it) => it.doc_id)));
  const must = its.filter((it) => it.must && !it.dec).length, look = its.filter((it) => !it.must && !it.dec).length;
  const im = itemMap();
  // agent_counts.js: the strip's agent part and the line under it count the same documents
  const sp = agentSplit(S.run.docs, im), ag = sp.cleared;
  const dec = its.filter((it) => it.dec).length, ready = ids.size - its.length - ag;
  const parts = [["m", must], ["w", look], ["d", dec], ["a", ag], ["", ready]];
  const cells = ids.size <= 240
    ? `<div class="cells" style="grid-template-columns:repeat(${ids.size},1fr)">${parts.map(([c, n]) => `<i class="${c}"></i>`.repeat(n)).join("")}</div>`
    : `<div class="cells bar">${parts.filter(([, n]) => n).map(([c, n]) => `<i class="${c}" style="flex:${n}"></i>`).join("")}</div>`;
  const lg = (cls, n, k) => `<span><i class="sw ${cls}"></i><b>${n}</b>${t(k)}</span>`;
  const legend = lg("m", must, "lgMust") + lg("w", look, "lgLook") + (ag || S.run.summary?.agent?.tried ? lg("a", ag, "lgAgent") : "")
    + lg("", ready, "lgReady") + (dec ? lg("d", dec, "lgDone") : "");
  const s = S.run.summary || {};
  const when = runLabel(S.runId);
  const replay = !S.runId.endsWith("-live");
  const per = s.mean_read_s ? Math.round(s.mean_read_s * 10) / 10 : null;
  const timed = !isNaN(runTime(S.runId)), dot = S.lang === "zh" ? "・" : " · ";
  const last = [timed ? (per ? t("metaLast", { when, s: per }) : t("metaLastNoS", { when })) : per ? t("progPer", { s: per }) : "",
    replay ? t("howCache") : "", runByText()].filter(Boolean).join(dot);
  const hist = S.runs.length > 1 ? `<select id="runPick" class="runpick" aria-label="${esc(t("history"))}" title="${esc(t("history"))}">${S.runs.slice(0, 12).map((r) =>
    `<option value="${esc(r)}" ${r === S.runId ? "selected" : ""}>${esc(runLabel(r))}${r.endsWith("-live") ? "" : " · " + esc(t("howCache"))}</option>`).join("")}</select>` : "";
  const pc = S.run.period_check;
  let pline = "";
  if (pc?.error) pline = t("periodErr", { e: pc.error });
  else if (pc) {
    const ps = S.client === "ALL" ? pc.summary : pc.by_client?.[S.client];
    if (ps) pline = t("periodLine", { who: S.client === "ALL" ? t("thisPeriod") : t("thisPeriodOf", { c: clientName(S.client) }), e: ps.einvoices, p: ps.paper, h: ps.held.total, r: ps.ready.total });
  }
  // the agent step of this check: ran here, replayed from stored runs, or not on this machine (never pretends it ran)
  const A = s.agent || {}, off = S.status?.agent && !S.status.agent.available;
  const atext = agentMetaText(A, sp, t);
  const aline = atext ? atext + (!A.ran && off ? dot + t("agentOff") : "") : off ? t("agentOff") : "";
  $("#periodCard").innerHTML = `<div class="strip">${cells}<div class="legend">${legend}</div></div>
    <div class="periodmeta"><div>${t("metaRead")}</div><div>${last}</div>${aline ? `<div class="agline">${icon("bot", 13)}<span>${aline.split("\n").map(esc).join("<br>")}</span></div>` : ""}${pline ? `<div>${esc(pline)}</div>` : ""}${hist ? `<div>${hist}</div>` : ""}</div>`;
  $("#runPick") && ($("#runPick").onchange = (e) => { S.jobEnd = null; loadRun(e.target.value); });
}

function sugLabel(it) { return t("d_" + it.suggest); }
function renderQueue() {
  $$("[data-qtab]").forEach((b) => b.classList.toggle("on", b.dataset.qtab === S.qtab));
  $$("[data-group]").forEach((b) => b.classList.toggle("on", (b.dataset.group === "1") === S.group));
  const allTodo = items().filter((it) => !it.dec && inFilter(it)).length, allDone = items().filter((it) => it.dec && inFilter(it)).length;
  $("#cntTodo").textContent = S.run ? allTodo : "";
  $("#cntDone").textContent = S.run ? allDone : "";
  const nAgent = agentItems().length;
  $("#cntAgent").textContent = S.run ? nAgent : "";
  $("#tabAgent").hidden = !S.run || (!nAgent && !S.run.summary?.agent?.tried && S.qtab !== "agent");
  const el = $("#qTable");
  const empty = (msg) => (el.innerHTML = `<tbody><tr><td class="empty" colspan="8">${esc(msg)}</td></tr></tbody>`);
  if (!S.run) return empty(t("noRunList"));
  const gs = groups();
  if (!gs.length || !gs.some((g) => g.items.length)) return empty(S.q ? t("noMatch", { q: S.q }) : S.qtab === "todo" ? t("noTodo") : S.qtab === "agent" ? t("noAgentList") : t("noDone"));
  const flat = visibleRows();
  S.focus = Math.max(0, Math.min(S.focus, flat.length - 1));
  if (S.qtab === "agent") return renderAgentRows(el, flat);
  const isTodo = S.qtab === "todo";
  let i = 0;
  const row = (it) => {
    const k = i++;
    const extra = it.exs.length - 1;
    const last = isTodo
      ? `<td class="sug">${esc(sugLabel(it))}</td><td class="keys">${kbd("1 2 3 4")}</td>`
      : `<td class="sug"><b class="dl">${esc(t("d_" + it.dec.decision))}</b><span class="by">${it.dec.pending ? esc(t("saving")) : esc(t("decidedBy", { who: it.dec.user || "", when: ago(tsMs(it.dec.ts)) }))}</span></td>
         <td class="keys"><button class="linkbtn" data-undo="${esc(it.doc_id)}">${esc(t("change"))}</button></td>`;
    return `<tr class="row ${k === S.focus ? "sel" : ""}" data-i="${k}" data-doc="${esc(it.doc_id)}">
      <td class="pad"><i class="sdot ${it.dec ? "d" : it.must ? "" : "w"}" title="${esc(it.must ? t("must") : t("should"))}"></i></td>
      <td class="cl"><span class="cname">${esc(shortName(clientName(it.client_id)))}</span><span class="cid">${esc(it.client_id)}</span></td>
      <td class="no mono">${esc(it.inv || DOC_TYPE[S.lang]?.[it.type] || "—")}</td>
      <td class="why"><span class="reason"><span class="rt">${esc(ruleVerdict(it.primary.rule_id))}</span>${extra ? `<span class="more" title="${esc(it.exs.slice(1).map((e) => ruleVerdict(e.rule_id)).join("\n"))}">+${extra}</span>` : ""}</span></td>
      <td class="r num">${esc(nt(it.total))}</td><td class="r num">${esc(nt(it.tax))}</td>${last}</tr>`;
  };
  const head = (g) => {
    if (g.key === "all") return "";
    const col = S.collapsed.has(g.key);
    const batch = isTodo && S.group ? `<button class="batch" data-batch="${esc(g.key)}">${esc(t("decideAll"))}</button>` : "";
    return `<tr class="grp" data-g="${esc(g.key)}"><td colspan="8"><div class="gh">
      <button class="gcol" data-col="${esc(g.key)}" aria-expanded="${!col}" title="${esc(col ? t("expand") : t("collapse"))}">${icon(col ? "chevronRight" : "chevronDown", 14)}
        ${S.group ? `<i class="sdot ${g.must ? "" : "w"}"></i>` : ""}<b>${esc(g.label)}</b><span class="gn">${g.items.length}</span>${g.law ? `<span class="law">${esc(g.law)}</span>` : ""}</button>
      <span class="sp"></span>${batch}</div></td></tr>`;
  };
  el.innerHTML = `<thead><tr><th class="pad"></th><th>${t("thClient")}</th><th>${t("thNo")}</th><th>${t("thWhy")}</th><th class="r">${t("thTotal")}</th><th class="r">${t("thTax")}</th><th>${isTodo ? t("thSug") : t("thDecision")}</th><th></th></tr></thead>
    <tbody>${gs.map((g) => head(g) + (S.collapsed.has(g.key) ? "" : g.items.map(row).join(""))).join("")}</tbody>`;
  $$("tr.row", el).forEach((tr) => (tr.onclick = (e) => {
    if (e.target.closest("[data-undo]")) return;
    S.focus = +tr.dataset.i; openDoc(tr.dataset.doc, S.qtab);
  }));
  $$("[data-undo]", el).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); const it = itemMap().get(b.dataset.undo); decide([b.dataset.undo], "undo", t("toastOne", { c: shortName(clientName(it.client_id)), d: t("taken") })); }));
  $$("[data-col]", el).forEach((b) => (b.onclick = () => { const k = b.dataset.col; S.collapsed.has(k) ? S.collapsed.delete(k) : S.collapsed.add(k); S.focus = 0; renderQueue(); }));
  $$("[data-batch]", el).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); decideGroup(b.dataset.batch); }));
}
// the vouchers the agent cleared: what it changed and how code verified it; opening one shows the whole check
function renderAgentRows(el, flat) {
  el.innerHTML = `<thead><tr><th class="pad"></th><th>${t("thClient")}</th><th>${t("thNo")}</th><th>${t("thAgentDid")}</th><th class="r">${t("thTotal")}</th><th class="r">${t("thTax")}</th><th>${t("thAgentHow")}</th><th></th></tr></thead>
    <tbody>${flat.map((it, k) => `<tr class="row ${k === S.focus ? "sel" : ""}" data-i="${k}" data-doc="${esc(it.doc_id)}">
      <td class="pad"><i class="sdot a" title="${esc(t("agentShort"))}"></i></td>
      <td class="cl"><span class="cname">${esc(shortName(clientName(it.client_id)))}</span><span class="cid">${esc(it.client_id)}</span></td>
      <td class="no mono">${esc(it.inv || DOC_TYPE[S.lang]?.[it.type] || "—")}</td>
      <td class="why"><span class="reason"><span class="rt">${esc(agentChanges(it.doc.agent))}</span></span></td>
      <td class="r num">${esc(nt(it.total))}</td><td class="r num">${esc(nt(it.tax))}</td>
      <td class="sug agv">${icon("check", 13)}<span>${esc(agentHow(it.doc.agent))}</span></td><td class="keys"></td></tr>`).join("")}</tbody>`;
  $$("tr.row", el).forEach((tr) => (tr.onclick = () => { S.focus = +tr.dataset.i; openDoc(tr.dataset.doc, "agent"); }));
}
function markFocus() {
  $$("#qTable tr.row").forEach((r) => r.classList.toggle("sel", +r.dataset.i === S.focus));
  const f = $(`#qTable tr.row[data-i="${S.focus}"]`);
  if (f) f.scrollIntoView({ block: "nearest" });
}
function setGroup(on) { S.group = on; store.set("group", on); S.collapsed.clear(); S.focus = 0; renderAll(); }

function renderClients() {
  const left = {}, must = {}, docsBy = {};
  undecided().forEach((it) => { left[it.client_id] = (left[it.client_id] || 0) + 1; if (it.must) must[it.client_id] = 1; });
  S.inbox.docs.forEach((d) => (docsBy[d.client_id] = (docsBy[d.client_id] || 0) + 1));
  const item = (id, name, n, of, hot) => `<li data-id="${esc(id)}" class="${S.client === id ? "on" : ""}" title="${n ? esc(t("leftN", { n })) : ""}">
      <span class="nm">${esc(name)}</span><span class="ct">${S.run ? `<b class="${hot ? "hot" : ""}">${n || 0}</b> / ` : ""}${of}</span>
      ${id === "ALL" ? "" : `<div class="cbar"><i style="width:${of ? Math.min(100, ((n || 0) / of) * 100) : 0}%"></i></div>`}</li>`;
  $("#clients").innerHTML = item("ALL", t("allClients"), undecided().length, S.inbox.docs.length, pendingMust().length) +
    S.inbox.clients.map((c) => item(c.id, shortName(c.name), left[c.id], docsBy[c.id] || 0, must[c.id])).join("");
  $$("#clients li").forEach((li) => (li.onclick = () => { S.client = li.dataset.id; S.focus = 0; renderAll(); }));
}

// "Before you export": the four steps of the old guide as a checklist
function renderChecks() {
  const st = stepState(), ex = lastExport();
  const li = (ok, ic, a, b, bad) => `<li class="${ok ? "ok" : "no"}${bad ? " bad" : ""}"><span class="ci">${icon(ic, 17)}</span><span><b>${esc(a)}</b>${esc(b)}</span></li>`;
  const sk = S.inbox.skipped?.length;
  const read = st.running ? (S.job.status === "agent"
      ? li(false, "clock", t("chkAgent", { i: S.job.agent_i || 0, n: S.job.agent_n || 0 }), t("chkReadSub"))
      : li(false, "clock", t("chkRun", { i: S.job.i || 0, n: S.job.n || st.n }), t("chkReadSub")))
    : !st.s1 ? li(false, "clock", t("emptyTitle"), "")
    : !S.run ? li(false, "clock", t("chkNone"), t("chkReadSub"))
    : newDocs() ? li(false, "clock", t("chkNew", { n: newDocs() }), t("chkReadSub"))
    : li(true, "check", t("chkReadOk", { n: S.run.summary?.n_docs ?? st.n }), sk ? t("chkReadSkip", { n: sk }) : t("chkReadOkSub"));
  const dec = !S.run ? "" : st.p ? li(false, "clock", t("chkDecNo", { n: st.p }), t("chkDecSub")) : li(true, "check", t("chkDecOk"), "");
  const lo = S.review.log_ok;
  const log = !S.run ? "" : lo === true ? li(true, "shield", t("chkLogOk"), t("chkLogOkSub"))
    : lo === false ? li(false, "shieldAlert", t("chkLogBad"), t("chkLogBadSub"), true) : li(false, "shield", t("chkLogNone"), t("chkLogNoneSub"));
  const exp = !S.run ? "" : st.s4 ? li(true, "check", t("chkExpOk", { when: ago(tsMs(ex.ts)) }), t("chkExpOkSub", { who: ex.user || "" }))
    : li(false, "download", t("chkExpNo"), t("chkExpNoSub"));
  $("#checks").innerHTML = read + dec + log + exp;
  $("#expWrap").innerHTML = S.run ? `<button class="btn ${st.s3 ? "primary" : "ghost"} wide" id="expBtn2">${icon(st.s3 ? "download" : "lock", 15)}<span>${esc(t("exportBtn"))}</span></button>` : "";
  $("#expBtn2") && ($("#expBtn2").onclick = () => confirmExport());
}

// ---------- group decisions (from direction B): one reason, one decision for the whole group, with Undo ----------
async function decideGroup(key) {
  const g = groups().find((x) => x.key === key);
  if (!g) return;
  const list = g.items.filter((it) => !it.dec);
  if (!list.length) return;
  const sugs = list.map((it) => it.suggest).filter(Boolean);
  const pre = sugs.length === list.length && sugs.every((s) => s === sugs[0]) ? sugs[0] : null;
  const shown = list.slice(0, 6).map((it) => `<li><span>${esc(shortName(clientName(it.client_id)))}</span><span class="mono">${esc(it.inv || it.doc_id)}</span><span class="num">${esc(nt(it.total))}</span></li>`).join("");
  const html = `<p>${esc(t("gdBody", { n: list.length }))}</p><ul class="gdlist">${shown}${list.length > 6 ? `<li class="muted">${esc(t("gdMore", { n: list.length - 6 }))}</li>` : ""}</ul>
    <div class="lbl">${esc(t("gdPick"))}</div><div class="gdpick">${DECISIONS.map((d, k) => `<label class="${pre === d ? "sugg" : ""}"><input type="radio" name="gd" value="${d}" ${pre === d ? "checked" : ""}>
      <kbd>${k + 1}</kbd><span><b>${esc(t("d_" + d))}</b><small>${esc(pre === d ? t("suggested") + " · " + t("d_" + d + "_sub") : t("d_" + d + "_sub"))}</small></span></label>`).join("")}</div>`;
  const okWait = (dlg) => {
    const ok = $("#cfOk"), set = () => (ok.disabled = !$("input[name=gd]:checked", dlg));
    $$("input[name=gd]", dlg).forEach((r) => (r.onchange = set)); set();
    ($("input[name=gd]:checked", dlg) || $("input[name=gd]", dlg)).focus();
    dlg.onkeydown = (e) => { if (["1", "2", "3", "4"].includes(e.key)) { const r = $$("input[name=gd]", dlg)[+e.key - 1]; r.checked = true; set(); e.preventDefault(); } };
  };
  if (!(await ask(t("gdTitle", { r: g.label }), html, t("gdGo", { n: list.length }), okWait))) return;
  const pick = $("#confirm input[name=gd]:checked")?.value;
  if (!pick) return;
  decide(list.map((it) => it.doc_id), pick, t("toastGroup", { n: list.length, d: t("d_" + pick) }));
}

// ---------- all vouchers ----------
function renderDocs() {
  const docs = S.inbox.docs.filter((d) => (S.client === "ALL" || d.client_id === S.client) &&
    (!S.q || [clientName(d.client_id), d.client_id, d.doc_id].some((x) => String(x).toLowerCase().includes(S.q.toLowerCase()))));
  const byDoc = itemMap();
  $("#docsSub").textContent = t("docsSub", { n: docs.length, m: new Set(docs.map((d) => d.client_id)).size });
  $("#docGrid").innerHTML = docs.length ? docs.map((d) => {
    const it = byDoc.get(d.doc_id);
    const st = S.review.status?.[d.doc_id];
    const dd = docById(d.doc_id), ok = !it && agentOk(dd);
    const label = it?.dec ? t("d_" + it.dec.decision) : ok ? t("agentShort") : st ? t("st_" + st) : "";
    const ex = it ? ruleVerdict(it.primary.rule_id) + (it.exs.length > 1 ? ` +${it.exs.length - 1}` : "") : ok ? agentChanges(dd.agent) : S.run ? t("noEx") : "";
    return `<button class="doc" data-open="${esc(d.doc_id)}">
      <span class="thumb"><img src="img/${esc(d.image)}" alt="" loading="lazy"></span>
      <span class="dn"><i class="sdot ${!it ? (ok ? "a" : "n") : it.dec ? "d" : it.must ? "" : "w"}"></i>${esc(shortName(clientName(d.client_id)))}<span class="cid">${esc(d.client_id)}</span></span>
      <span class="ds">${label ? `<b>${esc(label)}</b> · ` : ""}${esc(ex)}</span><span class="fn mono">${esc(d.doc_id)}</span></button>`;
  }).join("") : `<p class="empty">${esc(S.q ? t("noMatch", { q: S.q }) : t("emptyTitle"))}</p>`;
  $$("#docGrid .doc").forEach((el) => (el.onclick = () => openDoc(el.dataset.open, null)));
}

// ---------- one voucher: the queue on the left, the image, the reason and the four decisions on the right ----------
function openDoc(docId, mode, keep) {
  const it0 = itemMap().get(docId);
  const d = docById(docId) || S.inbox.docs.find((x) => x.doc_id === docId) || (it0 && { doc_id: docId, client_id: it0.client_id, platform: true });
  if (!d) return;
  let ids;
  if (keep && S.view?.ids.includes(docId)) ids = S.view.ids;
  else {
    const fromQueue = ["todo", "done", "agent"].includes(mode);
    const list = fromQueue ? visibleRows() : mode == null ? $$("#docGrid .doc").map((x) => x.dataset.open) : null;
    ids = fromQueue ? list.map((it) => it.doc_id) : list && list.length ? list : [docId];
    if (!ids.includes(docId)) ids = [docId];
  }
  if (S.view?.docId !== docId) { S.whyOpen = new Set(); S.zoom = 1; S.editing = null; S.agentOpen = false; }
  S.view = { docId, mode, ids };
  S.screen = "review";
  closePops();
  renderAll();
}
function closeReview() {
  const v = S.view;
  S.screen = S.page; S.view = null; S.editing = null;
  if (v?.mode && S.page === "queue") {
    const k = visibleRows().findIndex((it) => it.doc_id === v.docId);
    if (k >= 0) S.focus = k;
  }
  renderAll();
  if (S.screen === "queue") markFocus();
}
function viewDoc() {
  const v = S.view, it = itemMap().get(v.docId);
  const d = docById(v.docId) || S.inbox.docs.find((x) => x.doc_id === v.docId) || (it && { doc_id: v.docId, client_id: it.client_id, platform: true });
  return { d, it, exs: it ? it.exs : [] };
}
// the fields a voucher was held for, at most two: these are framed on the image by default
function keyFields(exs, d) {
  const out = [];
  const has = (f) => d.boxes?.[f] || exs.some((e) => (e.evidence || []).some((v) => v.field === f && v.box));
  const add = (f) => { if (out.length < 2 && !out.includes(f) && has(f)) out.push(f); };
  exs.forEach((e) => (e.evidence || []).forEach((v) => add(v.field)));
  exs.forEach((e) => (e.evidence_fields || []).forEach(add));
  return out;
}
function stake(e, it) {
  const bits = [];
  const v = (e.evidence || [])[0];
  if (v && v.value != null && v.value !== "") bits.push(`${t("f_" + v.field)} ${["total", "tax", "net"].includes(v.field) ? nt(v.value) : v.value}`);
  if (v?.field === "tax") return bits.concat(e.related_docs?.length ? [t("stakeSame", { d: e.related_docs[0] })] : []).join(S.lang === "zh" ? "・" : " · ");
  if (e.related_docs?.length) bits.push(t("stakeSame", { d: e.related_docs[0] }));
  if (it.tax != null && !isNaN(+it.tax) && +it.tax) bits.push(t("stakeTax", { t: nt(it.tax) }));
  return bits.join(S.lang === "zh" ? "・" : " · ");
}
function renderReview() {
  const v = S.view;
  if (!v) return;
  const { d, it, exs } = viewDoc();
  if (!d) return;
  const docId = v.docId, f = d.fields || {}, map = itemMap();
  // queue on the left: the list this review started from; decided ones stay, dimmed
  const leftN = v.ids.filter((id) => map.has(id) && !map.get(id).dec).length;
  $("#miniHead").textContent = v.mode === "done" ? t("tabDone") : v.mode === "todo" ? t("tabTodo") : v.mode === "agent" ? t("tabAgent") : t("navDocs");
  $("#miniLeft").textContent = t("revLeft", { n: leftN });
  $("#mini").innerHTML = v.ids.map((id) => {
    const x = map.get(id), dd = docById(id) || S.inbox.docs.find((y) => y.doc_id === id) || {};
    const aok = !x && agentOk(dd);
    const line = x ? (x.dec ? `${t("tabDone")} · ${t("d_" + x.dec.decision)}` : ruleVerdict(x.primary.rule_id)) : aok ? t("agentShort") : t("noEx");
    const tot = x ? x.total : dd.fields?.total;
    return `<li class="${id === docId ? "sel" : ""} ${!x || x.dec ? "done" : ""}" data-id="${esc(id)}"><i class="sdot ${!x ? (aok ? "a" : "n") : x.dec ? "d" : x.must ? "" : "w"}"></i>
      <span class="a">${esc(shortName(clientName(x?.client_id || dd.client_id)))}</span><span class="t num">${esc(nt(tot))}</span><span class="b">${esc(line)}</span></li>`;
  }).join("");
  $$("#mini li").forEach((li) => (li.onclick = () => openDoc(li.dataset.id, v.mode, true)));
  $("#mini li.sel")?.scrollIntoView({ block: "nearest" });

  // header
  const k = v.ids.indexOf(docId);
  $("#viewTitle").textContent = shortName(clientName(d.client_id));
  $("#viewCid").textContent = d.client_id;
  $("#viewPos").textContent = v.ids.length > 1 && k >= 0 ? t("pos", { k: k + 1, n: v.ids.length }) : "";
  $("#viewPrev").disabled = k <= 0; $("#viewNext").disabled = k < 0 || k >= v.ids.length - 1;
  const sep = S.lang === "zh" ? "・" : " · ";
  $("#viewMeta").textContent = d.platform ? t("einvTitle") : [DOC_TYPE[S.lang]?.[f.doc_type] || f.doc_type, f.invoice_no, f.date, f.total != null ? `${t("f_total")} ${nt(f.total)}` : ""].filter(Boolean).join(sep);
  $("#viewFile").textContent = d.platform ? "" : docId;

  // why it was held: a one-line verdict per reason; reason, law, evidence and what to check fold out
  const held = heldFields(exs);
  const block = (e, i) => {
    const R = RT(e.rule_id) || {};
    const cites = (e.citations || []).filter((c) => c.url || S.lang !== "zh" || /[一-鿿]/.test(c.label)).map((c) => c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${icon("link", 13)}${esc(citeLabel(c))}</a>`
      : `<span>${esc(citeLabel(c))}</span>`).join("");
    const evs = (e.evidence || []).map((x) => {
      const ok = provOk(x.field, x.provenance, held);
      const val = ["net", "tax", "total"].includes(x.field) ? nt(x.value) : x.value;
      return `<div class="evrow">${x.crop ? `<img src="out/${esc(x.crop)}" alt="">` : ""}<span class="k">${esc(t("f_" + x.field))}</span><span class="v">${esc(val)}</span>${ok ? "" : `<span class="flag" title="${esc(t("provTip", { p: x.provenance || "n/a" }))}"><i></i>${esc(t("readOnce"))}</span>`}</div>`;
    }).join("");
    const rel = e.related_docs?.length ? `<div class="rel"><span class="k">${esc(t("sameVoucher"))}</span>${e.related_docs.map((r) => {
      const rd = docById(r); return `<button class="relbtn" data-open="${esc(r)}">${rd ? `<img src="img/${esc(rd.image)}" alt="">` : ""}<span class="mono">${esc(r)}</span></button>`;
    }).join("")}</div>` : "";
    const st = stake(e, it || {});
    return `<div class="exb ${i ? "also" : ""}">${i ? `<div class="lbl">${esc(t("also"))}</div>` : ""}
      <div class="verdict">${esc(ruleVerdict(e.rule_id))}${st ? `<small>${esc(st)}</small>` : ""}</div>
      <details data-w="${i}" ${S.whyOpen.has(i) ? "open" : ""}><summary>${esc(t("reasonLaw"))}${icon("chevronDown", 14)}</summary>
        <dl><dt>${esc(t("dWhy"))}</dt><dd><b>${esc(ruleTitle(e))}</b><br>${esc(ruleWhy(e))}</dd>
        ${cites ? `<dt>${esc(t("dLaw"))}</dt><dd class="cites">${cites}</dd>` : ""}
        ${evs || rel ? `<dt>${esc(t("dEvidence"))}</dt><dd>${evs}${rel}</dd>` : ""}
        ${R.check ? `<dt>${esc(t("dCheck"))}</dt><dd>${esc(R.check)}</dd>` : ""}</dl></details></div>`;
  };
  const A = d.agent, aok = agentOk(d);
  $("#viewEx").innerHTML = exs.length
    ? `<div class="lbl"><i class="sdot ${it.must ? "" : "w"}"></i>${esc(it.must ? t("whyMust") : t("whyLook"))}</div>` + exs.map(block).join("")
      + (it.paper && !it.dec ? `<p class="paperhint">${icon("receipt", 14)}<span>${esc(t("paperHint"))}</span></p>` : "")
    : aok ? `<div class="lbl"><i class="sdot a"></i>${esc(t("agHeldWas"))}</div><ul class="agwas">${(A.held_exceptions || []).map((e) =>
        `<li><s>${esc(ruleVerdict(e.rule_id))}</s>${(e.evidence_fields || []).length ? `<small>${esc(e.evidence_fields.map((x) => t("f_" + x)).join(S.lang === "zh" ? "、" : ", "))}</small>` : ""}</li>`).join("")}</ul>
        <div class="verdict agv">${icon("check", 16)}<span>${esc(t("agSumVerified"))}</span></div>`
    : `<p class="muted">${esc(S.run ? t("viewNone") : t("viewNotRun"))}</p>`;
  renderAgentBox(d);
  // toggle events arrive after the fact: ignore one from a panel that was already redrawn or another voucher
  $$("#viewEx details").forEach((x) => x.addEventListener("toggle", () => {
    if (!x.isConnected || S.view?.docId !== docId) return;
    x.open ? S.whyOpen.add(+x.dataset.w) : S.whyOpen.delete(+x.dataset.w);
  }));
  $$("#viewEx [data-open]").forEach((b) => (b.onclick = () => openDoc(b.dataset.open, v.mode, true)));

  // the four decisions
  // the four decisions, always in view at the bottom of the panel; the suggested key is outlined, never filled
  const cleared = !it && d.corrections && Object.keys(d.corrections).length && !d.platform && !aok;
  const nextId = v.ids.slice(k + 1).find((id) => map.has(id) && !map.get(id).dec);
  $("#viewDecide").innerHTML = aok && !it
    ? `<div class="lbl">${esc(t("decideLbl"))}</div><div class="decided agdec"><span class="dl">${icon("check", 15)}${esc(t("agDecided"))}</span><span class="sp"></span>
        <button class="btn ghost sm" id="agReject">${icon("undo", 14)}<span>${esc(t("agReject"))}</span></button></div>`
    : cleared
    ? `<div class="decided"><span class="dl">${icon("check", 15)}${esc(t("recheckClear"))}</span>${nextId ? `<span class="sp"></span><button class="btn ghost sm" id="goNext">${esc(t("nextDoc"))}${kbd("J")}</button>` : ""}</div>`
    : !it ? "" : it.dec
    ? `<div class="lbl">${esc(t("decideLbl"))}</div><div class="decided"><span class="dl">${icon("check", 15)}${esc(t("decidedAs", { d: t("d_" + it.dec.decision) }))}</span>
        <span class="muted">${esc(it.dec.pending ? t("saving") : t("decidedBy", { who: it.dec.user || "", when: ago(tsMs(it.dec.ts)) }))}</span><span class="sp"></span>
        <button class="btn ghost sm" id="undoOne">${icon("undo", 14)}<span>${esc(t("change"))}</span>${kbd("U")}</button></div>`
    : `<div class="lbl"><span>${esc(t("decideLbl"))}</span><span class="muted acc">${kbd("Enter")}${esc(t("kAccept"))}</span></div>
       <div class="dgrid">${DECISIONS.map((x, n) => `<button class="dbtn ${it.suggest === x ? "sugg" : ""}" data-d="${x}" title="${esc(t("d_" + x + "_tip"))}"><kbd>${n + 1}</kbd>
         <b>${esc(t("d_" + x))}${it.suggest === x ? `<span class="stag" title="${esc(t("sugTip"))}">${esc(t("sugTag"))}</span>` : ""}</b><small>${esc(t("d_" + x + "_sub"))}</small></button>`).join("")}</div>`;
  $$("#viewDecide .dbtn").forEach((b) => (b.onclick = () => decideInViewer(b.dataset.d)));
  $("#goNext") && ($("#goNext").onclick = () => stepView(1));
  $("#agReject") && ($("#agReject").onclick = () => agentOverride("reject"));
  $("#undoOne") && ($("#undoOne").onclick = () => decide([docId], "undo", t("toastOne", { c: shortName(clientName(d.client_id)), d: t("taken") })));

  // what was read: only the fields read once are labelled; the rest carry no label
  const key = keyFields(exs, d);
  const p = d.provenance || {};
  const keys = S.allFields ? FIELD_KEYS : FIELD_CORE.concat(FIELD_KEYS.filter((x) => !FIELD_CORE.includes(x) && (held.has(x) || key.includes(x)) && f[x] != null));
  let agree = 0, counted = 0;
  const corr = d.corrections || {};
  const rows = d.fields ? keys.map((x) => {
    let val = f[x];
    if (x === "items") val = (val || []).map((i) => [i.desc, i.qty != null ? `× ${i.qty}` : "", i.amount != null ? nt(i.amount) : ""].filter(Boolean).join(" ")).join(S.lang === "zh" ? "；" : "; ");
    else if (x === "doc_type" && val) val = DOC_TYPE[S.lang]?.[val] || val;
    else if (AMOUNT.includes(x) && val != null) val = nt(val);
    const has = val != null && val !== "";
    const ok = provOk(x, p[x], held);
    if (has && p[x]) { counted++; if (ok) agree++; }
    const c = corr[x];
    // a value the agent proposed and code verified: changed (first reading struck through) or confirmed as read
    const acc = !c && aok && A.checks?.[x]?.accepted ? A.checks[x] : null;
    const ac = acc && String(acc.old ?? "") !== String(acc.value ?? "") ? acc : null;
    const flag = c ? `<span class="ptag" title="${esc(t("fCorrectedTip", { who: c.user || "", old: fmtVal(x, c.old) }))}">${icon("userCheck", 12)}${esc(t("fCorrected"))}</span>`
      : acc ? `<span class="ptag ag" title="${esc(t(ac ? "fAgentTip" : "fAgentTipSame", { old: fmtVal(x, acc.old), v: fmtVal(x, acc.value), ev: (acc.evidence || []).map((e) => t("agEvS_" + e)).join(S.lang === "zh" ? "、" : ", ") }))}">${icon("bot", 12)}${esc(t("fAgent"))}</span>`
      : has && p[x] && !ok ? `<span class="flag" title="${esc(t("provTip", { p: p[x] }))}"><i></i>${esc(t("readOnce"))}</span>` : "";
    const shown = has ? esc(val) : '<span class="muted">—</span>';
    const o = c || ac;
    const was = !o ? "" : o.old == null || o.old === "" ? `<span class="was none">${esc(t("fWasNone"))}</span>`
      : `<s class="was" title="${esc(t("fWas"))}">${esc(fmtVal(x, o.old))}</s>`;
    if (S.editing?.doc === docId && S.editing.field === x) {
      return `<tr class="editing ${key.includes(x) ? "key" : ""}"><td>${esc(t("f_" + x))}</td><td><form class="fedit" data-f="${x}" autocomplete="off">
        <input class="mono" name="v" value="${esc(S.editing.value)}" aria-label="${esc(t("fInputAria", { f: t("f_" + x) }))}" spellcheck="false">
        <button class="btn sm primary" type="submit">${esc(t("fSave"))}${kbd("Enter")}</button><button class="btn sm ghost" type="button" data-cancel>${esc(t("fCancel"))}${kbd("Esc")}</button>
        <div class="ferr" role="alert">${esc(S.editing.err || "")}</div></form></td></tr>`;
    }
    const cell = EDITABLE.includes(x) && !d.platform
      ? `${was}<button class="fv${o ? " fixed" : ""}" data-edit="${x}" title="${esc(t("fEditTip"))}"><span>${shown}</span>${icon("pencil", 12)}</button>${flag}${c ? `<button class="linkbtn rv" data-revert="${x}">${esc(t("fRevert"))}</button>` : ""}`
      : `${was}${shown}${flag}`;
    return `<tr class="${key.includes(x) ? "key" : ""}${o ? " corrected" : ""}"><td>${esc(t("f_" + x))}</td><td>${cell}</td></tr>`;
  }).join("") : `<tr><td class="muted">${esc(t(d.platform ? "einvFields" : "notRead"))}</td></tr>`;
  $("#viewFields").innerHTML = rows;
  $("#editHint").textContent = d.fields && !d.platform ? t("editHint") : "";
  $$("#viewFields [data-edit]").forEach((b) => (b.onclick = () => startEdit(b.dataset.edit)));
  $$("#viewFields [data-revert]").forEach((b) => (b.onclick = () => saveField(b.dataset.revert, null, true)));
  const fe = $("#viewFields form.fedit");
  if (fe) {
    const inp = $("input", fe);
    fe.onsubmit = (ev) => { ev.preventDefault(); saveField(fe.dataset.f, inp.value); };
    $("[data-cancel]", fe).onclick = cancelEdit;
    inp.onkeydown = (ev) => { if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); cancelEdit(); } };
    inp.oninput = () => { S.editing.value = inp.value; };
    if (document.activeElement !== inp) { inp.focus(); inp.select(); }
  }
  $("#readHead").textContent = t("readLbl") + (counted ? (S.lang === "zh" ? "・" : " · ") + t("readAgree", { a: agree, n: counted }) : "");
  $("#fieldToggle").textContent = S.allFields ? t("showKey") : t("showAll");
  $("#fieldToggle").hidden = !d.fields;
  $("#boxToggle").innerHTML = icon("scan", 15) + `<span>${esc(S.allFields ? t("showKey") : t("showAll"))}</span>${kbd("F")}`;
  $("#boxToggle").classList.toggle("on", S.allFields);
  $("#boxToggle").hidden = !!d.platform;

  // the image
  const img = $("#viewImg");
  // drop the previous voucher's load handler first: left in place, it fires when this image loads and draws that
  // voucher's field boxes on top of this one
  img.onload = null;
  $("#paper").hidden = !!d.platform;
  $("#viewNoImg").hidden = !d.platform;
  $("#viewNoImg").innerHTML = d.platform ? `${icon("receipt", 28)}<b>${esc(t("einvTitle"))}</b><p>${esc(t("einvNoPhoto"))}</p>` : "";
  $("#zOrig").hidden = !!d.platform;
  if (d.platform) { img.removeAttribute("src"); clearBoxes(); return; }
  const src = `img/${d.image}`;
  $("#zOrig").href = src;
  img.alt = docId;
  const draw = () => { if (S.view?.docId !== docId) return; layoutPaper(); drawBoxes(d, exs, key); };
  if (img.getAttribute("src") !== src) { clearBoxes(); img.src = src; }
  if (img.complete && img.naturalWidth) draw(); else img.onload = draw;
  loadTight(docId).then(() => { if (S.view?.docId === docId && img.complete && img.naturalWidth) draw(); });
  const nx = v.ids[k + 1]; if (nx) setTimeout(() => loadTight(nx), 300);     // the next voucher's boxes, ahead of time
}
// "How it was checked": one line until clicked; then what the AI looked at, what code verified (ticks) and the outcome
const agKey = (k) => I18N[S.lang]?.[k] != null;
function agentStep(s, A) {
  const a = s.args || {}, r = String(s.result || ""), tool = agKey("agTool_" + s.tool) ? s.tool : "bad";
  let what = "";
  if (s.tool === "zoom") {
    const m = r.match(/crop: ([\s\S]*)$/), txt = m ? m[1].trim() : "";
    what = !m ? t("agZoomFail") : txt && txt !== "(nothing read)" ? t("agZoomRead", { t: txt.length > 90 ? txt.slice(0, 90) + "…" : txt }) : t("agZoomNone");
  } else if (s.tool === "calc") {
    const nums = AMOUNT.filter((k) => a[k] != null && a[k] !== "").map((k) => `${t("f_" + k)} ${nt(a[k])}`).join(S.lang === "zh" ? "、" : ", ");
    what = `${nums} → ${/NOT consistent/.test(r) ? t("agCalcBad") : /consistent/.test(r) ? t("agCalcOk") : t("agCalcPart")}`;
  } else if (s.tool === "ubn_check") {
    const m = r.match(/^(\d{8}): check digit (valid|INVALID)/);
    what = m ? t(m[2] === "valid" ? "agUbnOk" : "agUbnBad", { u: m[1] }) : t("agUbnNot8");
  } else if (s.tool === "history") {
    const bits = [...r.matchAll(/seller UBN (\d+) confirmed on (\d+)/g)].map((m) => t("agHistHit", { u: m[1], n: m[2] }));
    if (/no other confirmed document/.test(r)) bits.push(t("agHistNone"));
    const same = r.match(/also on: \[(.*?)\]/);
    if (same) bits.push(t("agHistInvSame", { d: same[1].replace(/'/g, "") }));
    if (/not on any other document/.test(r)) bits.push(t("agHistInvNone"));
    what = bits.join(S.lang === "zh" ? "；" : "; ");
  } else if (s.tool === "platform") {
    const m = r.match(/seller UBN (\S+) net (\S+) tax (\S+)/);
    what = m ? t("agPlatHit", { u: m[1], n: nt(m[2]), t: nt(m[3]) }) : /not in the platform/.test(r) ? t("agPlatMiss") : t("agPlatNone");
  } else if (s.tool === "finish") {
    const pr = Object.entries(A.proposed || {}).map(([k, v]) => `${t("f_" + k)} ${fmtVal(k, v)}`).join(S.lang === "zh" ? "、" : ", ");
    const nh = (A.need_human || []).map((k) => t("f_" + k)).join(S.lang === "zh" ? "、" : ", ");
    what = [pr ? t("agPropose", { l: pr }) : t("agProposeNone"), nh ? t("agNeedHuman", { l: nh }) : ""].filter(Boolean).join(S.lang === "zh" ? "；" : "; ");
  }
  return `<li><span class="n">${s.step || ""}</span><span class="tl">${esc(t("agTool_" + tool))}</span><span class="tw">${esc(what)}</span></li>`;
}
function renderAgentBox(d) {
  const box = $("#viewAgent"), A = d.agent;
  box.hidden = !A;
  if (!A) { box.innerHTML = ""; return; }
  const ov = A.overridden, st = ov ? "overridden" : A.status === "verified" ? "verified" : A.status === "error" ? "error" : "held";
  const sum = st === "verified" ? t("agSumVerified") : st === "overridden" ? t("agSumOverridden") : st === "error" ? t("agSumError") : t("agSumHeld");
  const sep = S.lang === "zh" ? "、" : ", ";
  const checks = Object.entries(A.checks || {}).map(([k, c]) => {
    const same = String(c.old ?? "") === String(c.value ?? "");
    const val = same ? t("agConfirmV", { v: fmtVal(k, c.value) }) : `${fmtVal(k, c.old)} → ${fmtVal(k, c.value)}`;
    const why = c.accepted ? (c.evidence || []).map((e) => t("agEv_" + e)).join(sep)
      : t("agRefused") + (S.lang === "zh" ? "：" : ": ") + t("agWhy_" + String(c.why || "").split(":")[0]);
    return `<li class="${c.accepted ? "ok" : "no"}">${icon(c.accepted ? "check" : "x", 14)}<span class="k">${esc(t("f_" + k))}</span><span class="v mono">${esc(val)}</span><span class="w">${esc(why)}</span></li>`;
  }).join("") || `<li class="no">${icon("x", 14)}<span class="w">${esc(t("agNoProposal"))}</span></li>`;
  const still = (A.still_held_for || []).filter((x) => x !== "after-other-fixes");
  const res = st === "verified" ? t("agResVerified") : st === "overridden" ? t("agResOverridden", { who: ov.user || "" })
    : st === "error" ? t("agResError") : t("agResHeld") + (still.length ? ` (${still.map((x) => ruleVerdict(String(x).split(":")[0])).join(sep)})` : "");
  const meta = [A.model, A.seconds != null ? t("agSecs", { s: Math.round(A.seconds * 10) / 10 }) : "", t("agSteps", { n: (A.steps || []).length }),
    A.replayed ? t("agReplayed") : "", A.verify_version ? t("agRule", { v: A.verify_version }) : ""].filter(Boolean).join(S.lang === "zh" ? "・" : " · ");
  const open = S.agentOpen;
  box.innerHTML = `<button class="aghead" id="agToggle" aria-expanded="${open}">${icon("bot", 15)}<b>${esc(t("agTitle"))}</b>
      <span class="agsum ${st}">${esc(sum)}</span>${icon(open ? "chevronDown" : "chevronRight", 14)}</button>
    ${open ? `<ol class="agflow">
      <li><div class="lbl">${esc(t("agStep1"))}</div><ul class="agtools">${(A.steps || []).map((s) => agentStep(s, A)).join("")}</ul></li>
      <li><div class="lbl">${esc(t("agStep2"))}</div><ul class="agchecks">${checks}</ul></li>
      <li><div class="lbl">${esc(t("agStep3"))}</div><p class="agres">${esc(res)}</p>
        ${ov ? `<button class="btn ghost sm" id="agRestore">${icon("undo", 14)}<span>${esc(t("agRestore"))}</span></button>` : ""}</li>
    </ol><p class="agmeta">${esc(meta)}</p>` : ""}`;
  $("#agToggle").onclick = () => { S.agentOpen = !S.agentOpen; renderAgentBox(d); };
  $("#agRestore") && ($("#agRestore").onclick = () => agentOverride("restore"));
}
async function agentOverride(action) {
  const { d } = viewDoc();
  if (!d || !S.runId) return;
  try { await post("api/agent-override", { run: S.runId, doc_id: d.doc_id, action }); }
  catch (e) { toast(e.message); return; }
  S.last = { run: S.runId, kind: "agent", doc: d.doc_id, action };
  await loadRun(S.runId);
  toast(t(action === "reject" ? "agRejected" : "agRestored"), undoLast);
}
// fit the voucher in the stage, times the zoom
function layoutPaper() {
  const v = S.view; if (!v) return;
  const { d } = viewDoc(); if (!d || d.platform) return;
  const img = $("#viewImg"), stage = $("#stage"), paper = $("#paper");
  const [W, H] = d.image_size || [img.naturalWidth, img.naturalHeight];
  if (!W || !H) return;
  const fit = Math.min((stage.clientWidth - 48) / W, (stage.clientHeight - 48) / H);
  const s = fit * S.zoom;
  paper.style.width = Math.round(W * s) + "px"; paper.style.height = Math.round(H * s) + "px";
  stage.classList.toggle("zoomed", S.zoom > 1);
  $("#zLabel").textContent = Math.round(S.zoom * 100) + "%";
}
// ---- field boxes: around the value only, turned with the paper (server: ledger_room/tight_boxes.py) ----
function loadTight(docId) {
  const k = S.runId + "|" + docId + "|" + JSON.stringify(docById(docId)?.corrections || {});
  if (S.tb[k]) return S.tb[k].p;
  const ent = { p: null, boxes: null };
  S.tb[k] = ent;
  ent.p = j(`api/boxes?run=${encodeURIComponent(S.runId)}&doc=${encodeURIComponent(docId)}`)
    .then((r) => { ent.boxes = r.boxes || {}; }).catch(() => { ent.boxes = null; ent.failed = true; });
  return ent.p;
}
const tightOf = (docId) => S.tb[S.runId + "|" + docId + "|" + JSON.stringify(docById(docId)?.corrections || {})];
function clearBoxes() { $$("#paper .hl, #paper .hlsvg, #paper .hlab").forEach((h) => h.remove()); }
let _boxArgs = null;
function redrawBoxes() { if (_boxArgs && S.view?.docId === _boxArgs[0].doc_id) drawBoxes(..._boxArgs); }
// dark pixels of the voucher image, for placing labels where the paper is blank
let _ink = null;
function inkMap(img) {
  if (_ink && _ink.src === img.currentSrc) return _ink;
  const W = img.naturalWidth, H = img.naturalHeight, s = Math.min(1, 900 / Math.max(W, H));
  const c = document.createElement("canvas"); c.width = Math.max(1, Math.round(W * s)); c.height = Math.max(1, Math.round(H * s));
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, c.width, c.height);
  let data = null; try { data = ctx.getImageData(0, 0, c.width, c.height).data; } catch (e) { data = null; }
  _ink = { src: img.currentSrc, s, w: c.width, h: c.height, data };
  return _ink;
}
function inkIn(m, x, y, w, h) {       // share of dark pixels in an image-pixel rectangle
  if (!m.data) return 0;
  const x0 = Math.max(0, Math.floor(x * m.s)), y0 = Math.max(0, Math.floor(y * m.s));
  const x1 = Math.min(m.w, Math.ceil((x + w) * m.s)), y1 = Math.min(m.h, Math.ceil((y + h) * m.s));
  let n = 0, dark = 0;
  for (let yy = y0; yy < y1; yy++) for (let xx = x0; xx < x1; xx++) {
    const i = (yy * m.w + xx) * 4; n++;
    if (m.data[i] * 0.299 + m.data[i + 1] * 0.587 + m.data[i + 2] * 0.114 < 150) dark++;
  }
  return n ? dark / n : 1;
}
const hit = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
function drawBoxes(d, exs, key) {
  _boxArgs = [d, exs, key];
  const paper = $("#paper"), img = $("#viewImg");
  clearBoxes();
  if (!d.image_size) return;
  const [W, H] = d.image_size;
  const tight = tightOf(d.doc_id);
  if (tight && !tight.boxes && !tight.failed) return;          // still working them out: draw nothing rather than flicker
  const T = tight?.boxes || null;
  const raw = { ...(d.boxes || {}) };
  exs.forEach((e) => (e.evidence || []).forEach((v) => { if (v.box && !raw[v.field]) raw[v.field] = v.box; }));
  const corr = d.corrections || {};
  const shapes = [];
  const fields = new Set([...Object.keys(raw), ...Object.keys(T || {})]);
  fields.forEach((f) => {
    const isKey = key.includes(f) || !!corr[f];
    if (!isKey && !S.allFields) return;
    const tb = T?.[f];
    if (tb?.none) return;                                          // corrected value not found on the page: no box
    if (corr[f] && !tb?.moved && T) return;                        // the reader's box points at the misread value
    const q = tb?.quad || (raw[f] && [[raw[f][0], raw[f][1]], [raw[f][2], raw[f][1]], [raw[f][2], raw[f][3]], [raw[f][0], raw[f][3]]]);
    if (!q) return;
    const xs = q.map((p) => p[0]), ys = q.map((p) => p[1]);
    shapes.push({ f, q, isKey, bb: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], fixed: !!corr[f] });
  });
  if (!shapes.length) return;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "hlsvg"); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
  shapes.forEach((sh) => {
    const pg = document.createElementNS(svgNS, "polygon");
    pg.setAttribute("points", sh.q.map((p) => p.join(",")).join(" "));
    pg.setAttribute("class", sh.isKey ? "key" : "");
    pg.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(pg);
  });
  paper.appendChild(svg);
  // labels: only on the key fields (all fields: on hover), outside the box, where the paper is blank
  const scale = paper.clientWidth / W || 1;
  const m = img.complete && img.naturalWidth ? inkMap(img) : { data: null };
  const placed = [];
  const stage = $("#stage");
  const gutterL = paper.offsetLeft - stage.scrollLeft, gutterR = stage.clientWidth - (paper.offsetLeft + paper.offsetWidth);
  shapes.filter((sh) => sh.isKey).forEach((sh) => {
    const el = document.createElement("span");
    el.className = "hlab" + (sh.fixed ? " fixed" : "");
    el.textContent = t("f_" + sh.f) + (sh.fixed ? t("boxMoved") : "");
    paper.appendChild(el);
    const lw = (el.offsetWidth + 2) / scale, lh = (el.offsetHeight + 2) / scale, g = 5 / scale;
    const [x0, y0, x1, y1] = sh.bb, cy = (y0 + y1) / 2;
    const cands = [[x0, y0 - g - lh], [x1 + g, cy - lh / 2], [x1 - lw, y0 - g - lh], [x0, y1 + g], [x0 - g - lw, cy - lh / 2], [x1 - lw, y1 + g]];
    let best = null;
    cands.forEach(([x, y], i) => {
      if (x < 0 || y < 0 || x + lw > W || y + lh > H) return;
      const r = [x, y, x + lw, y + lh];
      if (shapes.some((o) => hit(r, o.bb)) || placed.some((o) => hit(r, o))) return;
      const ink = inkIn(m, x, y, lw, lh);
      const score = ink + i * 0.004;                                // ties go to the earlier (above, then right)
      if (!best || score < best.score) best = { x, y, ink, score, r };
    });
    let x, y, lead = null;
    if (best && best.ink < 0.035) ({ x, y } = best);
    else {
      // no blank spot next to the box: put the label in the margin beside the paper, with a thin leader line
      const left = (x0 + x1) / 2 < W / 2;
      const room = (left ? gutterL : gutterR) - 8;
      if (room * 1 >= el.offsetWidth) {
        x = left ? -lw - 8 / scale : W + 8 / scale; y = Math.min(Math.max(0, cy - lh / 2), H - lh);
        lead = left ? [x + lw, cy, x0, cy] : [x, cy, x1, cy];
        el.classList.add("out");
      } else if (best) ({ x, y } = best);
      else { x = x0; y = Math.max(0, y0 - g - lh); }
    }
    el.style.left = (x / W) * 100 + "%"; el.style.top = (y / H) * 100 + "%";
    placed.push([x, y, x + lw, y + lh]);
    if (lead) {
      const ln = document.createElementNS(svgNS, "line");
      ln.setAttribute("x1", lead[0]); ln.setAttribute("y1", lead[1]); ln.setAttribute("x2", lead[2]); ln.setAttribute("y2", lead[3]);
      ln.setAttribute("class", "lead"); ln.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(ln);
    }
  });
}
function zoom(f) {
  S.zoom = f ? Math.max(0.5, Math.min(4, S.zoom * f)) : 1;
  layoutPaper();
  redrawBoxes();
}
// ---- the list on the left of the review screen folds to a strip ([ key); remembered ----
function applyMini() {
  $("#rev").classList.toggle("folded", !!S.miniHide);
  const b = $("#miniToggle");
  b.title = t(S.miniHide ? "miniShow" : "miniHide") + " ( [ )";
  b.setAttribute("aria-label", t(S.miniHide ? "miniShow" : "miniHide"));
  b.setAttribute("aria-expanded", String(!S.miniHide));
}
function toggleMini() {
  S.miniHide = !S.miniHide; store.set("miniHide", S.miniHide); applyMini();
  if (S.screen === "review") requestAnimationFrame(() => { layoutPaper(); redrawBoxes(); });
}

// ---- correcting a field the reader got wrong (server: /api/correct, ledger_room/corrections.py) ----
function startEdit(field) {
  const { d } = viewDoc();
  if (!d?.fields || d.platform || !EDITABLE.includes(field)) return;
  const v = d.fields[field];
  S.editing = { doc: d.doc_id, field, value: v == null ? "" : AMOUNT.includes(field) ? Number(v).toLocaleString("en-US") : String(v), err: "" };
  renderReview();
}
function cancelEdit() {
  const f = S.editing?.field;
  S.editing = null; renderReview();
  if (f) $(`#viewFields [data-edit="${f}"]`)?.focus();
}
async function saveField(field, value, revert) {
  const { d } = viewDoc();
  if (!d || !S.runId) return;
  const before = d.fields[field];
  let r;
  try {
    r = await post("api/correct", revert ? { run: S.runId, doc_id: d.doc_id, field, revert: true } : { run: S.runId, doc_id: d.doc_id, field, value });
  } catch (e) {
    const msg = e.body?.code ? t("fErr_" + e.body.code) : t("fErrNet");
    if (S.editing) { S.editing.err = msg; renderReview(); } else toast(msg);
    return;
  }
  S.editing = null;
  if (!r.changed) { toast(t("fSame")); renderReview(); return; }
  S.last = { run: S.runId, kind: "correct", doc: d.doc_id, field, prev: before };
  await loadRun(S.runId);
  const label = t("f_" + field);
  toast(revert ? t("fReverted", { f: label, v: fmtVal(field, r.value) }) : t("fSaved", { f: label, v: fmtVal(field, r.value) }), undoLast);
  $(`#viewFields [data-edit="${field}"]`)?.focus();
}

function toggleFields() { S.allFields = !S.allFields; renderReview(); }
function decideInViewer(dec) {
  const v = S.view;
  if (!v) return;
  const it = itemMap().get(v.docId);
  if (!it || it.dec) return;
  const k = v.ids.indexOf(v.docId);
  decide([v.docId], dec, t("toastOne", { c: shortName(clientName(it.client_id)), d: t("d_" + dec) }));
  const map = itemMap();
  const rest = v.ids.slice(k + 1).concat(v.ids.slice(0, Math.max(k, 0))).filter((id) => map.has(id) && !map.get(id).dec);
  if (rest.length) openDoc(rest[0], v.mode, true); else closeReview();
}
function stepView(dir) {
  const v = S.view;
  if (!v) return;
  const k = v.ids.indexOf(v.docId) + dir;
  if (k >= 0 && k < v.ids.length) openDoc(v.ids[k], v.mode, true);
}

// ---------- keys ----------
function renderKeybar() {
  const K = (k, l) => `<span>${kbd(k)}${esc(l)}</span>`;
  let left = "";
  if (S.screen === "review") left = K("J K", t("kMove")) + K("1 2 3 4", t("kDecide")) + K("Enter", t("kAccept")) + K("C", t("kEdit")) + K("U", t("kChange")) + K("E", t("kReason")) + K("F", t("kFields")) + K("[", t("kList")) + K("Esc", t("kBack"));
  else if (S.screen === "queue") left = K("J K", t("kMove")) + K("Enter", t("kOpen")) + (S.qtab === "todo" ? K("1 2 3 4", t("kDecide")) + K("G", t("kGroup")) + (S.group ? K("Shift D", t("kDecideGroup")) : "") : S.qtab === "done" ? K("U", t("kChange")) : "");
  $("#keybar").innerHTML = left + K("Z", t("kUndo")) + K("/", t("kSearch"));
}
function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const inField = /INPUT|SELECT|TEXTAREA/.test(e.target.tagName);
  if (e.key === "Escape") {
    if (!$$(".pop").every((p) => p.hidden)) { closePops(); return; }
    if (e.target.id === "search") { e.target.value = ""; S.q = ""; e.target.blur(); renderAll(); return; }
    if (S.editing && e.target.closest?.(".fedit")) { cancelEdit(); e.preventDefault(); return; }
    if (S.screen === "review" && !$("#confirm").open && !$("#login").open) { closeReview(); e.preventDefault(); }
    return;
  }
  if (inField || $("#confirm").open || $("#login").open) return;
  const k = e.key.toLowerCase();
  if ((k === "enter" || k === " ") && /^(BUTTON|A|SUMMARY)$/.test(e.target.tagName)) return;   // a focused control handles its own Enter
  if (e.target.tagName === "BUTTON") e.target.blur();   // a shortcut moves on from the button clicked last: no stale focus ring
  if (k === "/") { $("#search").focus(); e.preventDefault(); return; }
  if (k === "z") { undoLast(); e.preventDefault(); return; }
  if (S.screen === "review") {
    const id = S.view?.docId, dec = !!S.review.decisions[id];
    if (k === "[") { toggleMini(); e.preventDefault(); return; }
    if (k === "enter" && !dec && itemMap().get(id)) { decideInViewer(itemMap().get(id).suggest); e.preventDefault(); return; }
    if (k === "c") {
      // the field the paper check is about first: one read once, then the fields of a paper-first reason, then the framed ones
      const { d: dd, exs: xx } = viewDoc();
      const order = [...heldFields(xx), ...xx.filter((x) => PAPER_FIRST.has(x.rule_id)).flatMap((x) => x.evidence_fields || []), ...keyFields(xx, dd || {})];
      const kf = order.find((x) => EDITABLE.includes(x)) || EDITABLE.find((x) => dd?.fields && x in dd.fields);
      if (kf) startEdit(kf);
      e.preventDefault(); return;
    }
    if (k === "j" || k === "arrowright" || k === "arrowdown") stepView(1);
    else if (k === "k" || k === "arrowleft" || k === "arrowup") stepView(-1);
    else if (["1", "2", "3", "4"].includes(k) && !dec) decideInViewer(DECISIONS[+k - 1]);
    else if (k === "u" && dec) decide([id], "undo", t("toastOne", { c: shortName(clientName(itemMap().get(id)?.client_id)), d: t("taken") }));
    else if (k === "e") { const all = $$("#viewEx details"); const open = !all.every((x) => x.open); all.forEach((x) => (x.open = open)); }
    else if (k === "f") toggleFields();
    else return;
    e.preventDefault();
    return;
  }
  if (S.screen !== "queue") return;
  if (k === "g" && S.qtab === "todo") { setGroup(!S.group); e.preventDefault(); return; }
  const list = visibleRows();
  if (!list.length) return;
  const cur = list[S.focus];
  if (k === "j" || k === "arrowdown") S.focus = Math.min(S.focus + 1, list.length - 1);
  else if (k === "k" || k === "arrowup") S.focus = Math.max(S.focus - 1, 0);
  else if (k === "enter" || k === "o") { openDoc(cur.doc_id, S.qtab); e.preventDefault(); return; }
  else if (S.qtab === "todo" && ["1", "2", "3", "4"].includes(k)) {
    const d = DECISIONS[+k - 1];
    decide([cur.doc_id], d, t("toastOne", { c: shortName(clientName(cur.client_id)), d: t("d_" + d) })); e.preventDefault(); markFocus(); return;
  }
  else if (S.qtab === "done" && k === "u") { decide([cur.doc_id], "undo", t("toastOne", { c: shortName(clientName(cur.client_id)), d: t("taken") })); e.preventDefault(); return; }
  else if (S.qtab === "todo" && S.group && e.shiftKey && k === "d") { decideGroup(cur.primary.rule_id); e.preventDefault(); return; }
  else return;
  e.preventDefault();
  markFocus();
}

// ---------- logs ----------
async function renderLog() {
  const clear = () => { $("#logTable").innerHTML = ""; $("#opHead").innerHTML = ""; $("#opTable").innerHTML = ""; };
  if (!S.runId) { $("#logHead").innerHTML = `<p class="muted">${esc(t("logNone"))}</p>`; clear(); return; }
  if (S.me && S.me.role !== "admin") { $("#logHead").innerHTML = `<p class="muted">${esc(t("logAdmin"))}</p>`; clear(); return; }
  const L = await j(`api/audit-log?run=${encodeURIComponent(S.runId)}${um()}`);
  $("#logHead").innerHTML = verifyBox(L) + `<details class="how"><summary>${esc(t("howVerified"))}${icon("chevronDown", 14)}</summary><p>${esc(t("logExplain"))}</p></details>`;
  renderOplog();
  $("#logTable").innerHTML = `<thead><tr><th>${t("lt_seq")}</th><th>${t("lt_time")}</th><th>${t("lt_event")}</th><th>${t("lt_doc")}</th><th>${t("lt_detail")}</th><th>${t("lt_hash")}</th></tr></thead><tbody>` +
    L.entries.map((e) => `<tr><td class="num">${e.seq}</td><td title="${esc(e.ts || "")}">${esc(ago(tsMs(e.ts)))}</td><td title="${esc(e.event)}">${esc(evName(e.event))}</td>
      <td class="mono">${esc(e.doc_id || "")}</td>
      <td>${esc(e.event === "agent_check" ? [t("agSt_" + e.status), agentChanges({ checks: e.checks })].filter(Boolean).join(" · ") : e.rule_id ? ruleVerdict(e.rule_id) : (e.event === "doc_read" ? `${e.model || ""} ${e.device || ""}${e.cached ? ` (${t("ev_cached")})` : ""}` : e.event === "run_start" ? (S.lang === "zh" ? t("localTip") : e.network) : e.event === "run_end" ? `${e.n_exceptions}` : ""))}</td>
      <td class="h mono" title="${esc(e.hash || "")}">${esc((e.hash || "").slice(0, 10))}</td></tr>`).join("") + "</tbody>";
}
// an event name in the logs, in words; an unknown one stays as written
const evName = (ev) => (I18N[S.lang]?.["ev_" + ev] ?? ev);
function verifyBox(L) {
  const n = L.entries_verified;
  const what = !L.chain_ok ? t("v_broken", { n }) : L.mac_ok === false ? t("v_mac", { n }) : L.tail_ok === false ? t("v_tail")
    : L.unsigned ? t("v_unsigned", { n }) : L.mac_ok ? t("v_ok", { n }) : t("v_nokey", { n });
  const ok = L.verified;
  return `<div class="verify ${ok ? "ok" : L.unsigned && L.chain_ok ? "warn" : "bad"}">${icon(ok ? "shield" : "shieldAlert", 16)}<span>${esc(what)}</span></div>`;
}
async function renderOplog() {
  let L;
  try { L = await j("api/oplog"); } catch (e) { $("#opHead").innerHTML = ""; $("#opTable").innerHTML = ""; return; }
  $("#opHead").innerHTML = verifyBox(L);
  $("#opTable").innerHTML = `<thead><tr><th>${t("lt_seq")}</th><th>${t("lt_time")}</th><th>${t("lt_event")}</th><th>${t("lt_user")}</th><th>${t("lt_detail")}</th><th>${t("lt_hash")}</th></tr></thead><tbody>` +
    L.entries.slice().reverse().slice(0, 200).map((e) => `<tr><td class="num">${e.seq}</td><td title="${esc(e.ts || "")}">${esc(ago(tsMs(e.ts)))}</td>
      <td title="${esc(e.event)}">${esc(evName(e.event))}</td><td>${esc(e.user || "")}</td><td class="mono">${esc([e.decision ? (e.decision === "undo" ? t("taken") : t("d_" + e.decision)) : "",
        e.event === "correct" ? `${t("f_" + e.field)} ${fmtVal(e.field, e.old)} → ${fmtVal(e.field, e.new)}` : "",
        e.event === "agent_override" ? t("agOver_" + e.action) : "", e.doc_id, e.run_id || e.inbox || e.host || e.path].filter(Boolean).join(" · "))}</td>
      <td class="h mono" title="${esc(e.hash || "")}">${esc((e.hash || "").slice(0, 10))}</td></tr>`).join("") + "</tbody>";
}

// ---------- checks: confirm, start, follow, resume ----------
function ask(title, html, okLabel, setup) {
  return new Promise((resolve) => {
    const dlg = $("#confirm");
    $("#cfTitle").textContent = title; $("#cfBody").innerHTML = html;
    $("#cfOk").hidden = !okLabel; $("#cfOk").disabled = false; $("#cfOk").textContent = okLabel || "";
    $("#cfCancel").textContent = okLabel ? t("cancel") : t("close");
    dlg.onkeydown = null;
    dlg.onclose = () => resolve(dlg.returnValue === "ok");
    dlg.returnValue = "";
    dlg.showModal();
    if (setup) setup(dlg);
    else (okLabel ? $("#cfOk") : $("#cfCancel")).focus();
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
    if (S.screen === "queue") { renderOverview(); renderChecks(); }
  }
}

// ---------- export ----------
async function confirmExport() {
  closePops();
  if (!S.run) { toast(t("expNoRun")); return; }
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
  const p = pendingMust().length;
  const row = (k, v, cls = "") => `<tr class="${cls}"><td>${k}</td><td class="num">${v}</td></tr>`;
  const ex = lastExport();
  const html = `<table class="counts">${row(t("expReady"), c.ready)}${c.nonded ? row(t("expNonded"), c.nonded, "sub") : ""}${row(t("expExclude"), c.exclude)}${row(t("expDefer"), c.defer)}${row(t("expNotIn"), c.notin)}${c.hold ? row(t("expHold"), c.hold, "hot") : ""}</table>
    ${p ? `<p class="lockline">${icon("lock", 14)}${esc(t("expLocked", { n: p }))}</p>` : `<p class="muted">${t("expNote")}</p>`}
    ${ex ? `<p class="warnline">${esc(t("expAgain", { who: ex.user, when: ago(tsMs(ex.ts)) }))}</p>` : ""}
    <p><a class="dl2" href="api/export-summary?run=${encodeURIComponent(S.runId)}">${icon("download", 14)}${t("expSummary")}</a></p>`;
  if (!(await ask(t("confirmExpTitle"), html, p ? null : t("expGo")))) return;
  location.href = `api/export?run=${encodeURIComponent(S.runId)}${um()}`;
  setTimeout(async () => { await loadReview(); renderAll(); }, 1500);
}

init();
