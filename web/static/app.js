"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { set: "main", inbox: null, run: null, runId: null, client: "ALL", rules: new Set(), tab: "exceptions" };
const RULE_SHORT = {
  UBN_CHECKSUM: "UBN checksum", DUP_CROSS_MONTH: "Duplicate, same client", DUP_CROSS_CLIENT: "Duplicate, across clients",
  S19_NONDEDUCTIBLE: "Art. 19 non-deductible", OVER_LIMIT: "Over policy limit", PERIOD_MISMATCH: "Period mismatch",
};
const GOOD = new Set(["ocr-confirmed", "snapped", "ocr-zoom", "ocr-role", "ocr", "ocr-keyword"]);
const ICON_LINK = '<svg viewBox="0 0 24 24"><path d="M14 3h7v7M10 14L21 3M21 14v7H3V3h7"/></svg>';

async function j(url, opt) { const r = await fetch(url, opt); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); }

async function init() {
  const boxes = await j("/api/inboxes");
  const sel = $("#inbox");
  sel.innerHTML = boxes.map((b) => `<option value="${esc(b.set)}">${esc(b.set)} (${b.n_docs} documents, ${b.n_clients} clients)</option>`).join("");
  sel.value = boxes.some((b) => b.set === "main") ? "main" : boxes[0]?.set;
  sel.onchange = () => loadInbox(sel.value);
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => setTab(t.dataset.tab)));
  $("#replayBtn").onclick = () => startRun(false);
  $("#liveBtn").onclick = () => startRun(true);
  $("#closeViewer").onclick = () => $("#viewer").close();
  $("#viewer").addEventListener("click", (e) => { if (e.target.id === "viewer") $("#viewer").close(); });
  status();
  await loadInbox(sel.value);
}

async function status() {
  try {
    const s = await j("/api/status");
    if (s.model_loaded) $("#modelPill").textContent = `Model: ${s.model} on OpenVINO (${s.device})`;
  } catch (e) { /* ignore */ }
}

async function loadInbox(set) {
  S.set = set; S.client = "ALL"; S.rules.clear(); S.run = null; S.runId = null;
  S.inbox = await j(`/api/inbox?set=${encodeURIComponent(set)}`);
  const runs = await j("/api/runs");
  const mine = runs.filter((r) => r.startsWith(set + "-"));
  const pick = mine[0] || (runs.includes("eval-" + set) ? "eval-" + set : null);
  if (pick) await loadRun(pick); else renderAll();
}

async function loadRun(runId) {
  S.runId = runId;
  S.run = await j(`/api/result?run=${encodeURIComponent(runId)}`);
  renderAll();
  if (S.tab === "log") renderLog();
}

const SEV_ORDER = { high: 0, medium: 1, review: 2 };
function exceptions() {
  if (!S.run) return [];
  return S.run.exceptions
    .filter((e) => (S.client === "ALL" || e.client_id === S.client) && (!S.rules.size || S.rules.has(e.rule_id)))
    .slice()
    .sort((a, b) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3) || a.client_id.localeCompare(b.client_id) || a.doc_id.localeCompare(b.doc_id));
}

function renderAll() { renderClients(); renderStats(); renderChips(); renderExceptions(); renderDocs(); }

function renderClients() {
  const counts = {};
  (S.run?.exceptions || []).forEach((e) => (counts[e.client_id] = (counts[e.client_id] || 0) + 1));
  const docsBy = {};
  S.inbox.docs.forEach((d) => (docsBy[d.client_id] = (docsBy[d.client_id] || 0) + 1));
  const total = S.run ? S.run.exceptions.length : "";
  const item = (id, name, meta, n) =>
    `<li data-id="${esc(id)}" class="${S.client === id ? "active" : ""}"><span class="cname">${esc(name)}</span>` +
    `<span class="badge ${n ? "hot" : ""}">${S.run ? n || 0 : "-"}</span><span class="cmeta">${esc(meta)}</span></li>`;
  $("#clients").innerHTML = item("ALL", "All clients", `${S.inbox.docs.length} documents`, total) +
    S.inbox.clients.map((c) => item(c.id, c.name, `${c.id} · UBN ${c.ubn} · ${docsBy[c.id] || 0} docs`, counts[c.id])).join("");
  document.querySelectorAll("#clients li").forEach((li) => (li.onclick = () => { S.client = li.dataset.id; renderAll(); }));
}

function renderStats() {
  const s = S.run?.summary;
  const affected = s ? Object.values(s.by_client).filter((x) => x > 0).length : null;
  const tile = (v, k) => `<div class="stat"><div class="v">${v ?? "-"}</div><div class="k">${k}</div></div>`;
  $("#stats").innerHTML = tile(s?.n_docs, "documents read") + tile(s?.n_exceptions, "exceptions") +
    tile(affected, "clients with exceptions") + tile(s?.mean_read_s != null ? s.mean_read_s + " s" : null, "read time per document");
  $("#batchSub").textContent = S.runId
    ? `Showing run ${S.runId}. Reads every document in every client folder, then runs the rule engine across the whole firm.`
    : "Reads every document in every client folder, then runs the rule engine across the whole firm.";
}

function renderChips() {
  const all = (S.run?.exceptions || []).filter((e) => S.client === "ALL" || e.client_id === S.client);
  $("#ruleChips").innerHTML = Object.keys(RULE_SHORT).map((r) => {
    const n = all.filter((e) => e.rule_id === r).length;
    return `<button class="chip ${S.rules.has(r) ? "on" : ""}" data-r="${r}">${RULE_SHORT[r]}<b>${n}</b></button>`;
  }).join("");
  document.querySelectorAll(".chip").forEach((c) => (c.onclick = () => {
    S.rules.has(c.dataset.r) ? S.rules.delete(c.dataset.r) : S.rules.add(c.dataset.r);
    renderChips(); renderExceptions();
  }));
}

function docById(id) { return S.run?.docs.find((d) => d.doc_id === id); }
function clientName(id) { return S.inbox.clients.find((c) => c.id === id)?.name || id; }

function renderExceptions() {
  const list = exceptions();
  if (!S.run) { $("#exList").innerHTML = `<div class="card empty">No run yet for this inbox. Press <b>Replay batch</b> or <b>Run live</b>.</div>`; return; }
  if (!list.length) { $("#exList").innerHTML = `<div class="card empty">No exceptions for this selection.</div>`; return; }
  $("#exList").innerHTML = list.map((e, i) => {
    const crops = e.evidence.map((v) => `<img src="/out/${esc(v.crop)}" alt="${esc(v.field)}" title="${esc(v.field)}: ${esc(v.value)} (${esc(v.provenance || "")})">`).join("");
    const prov = e.evidence.map((v) => `${esc(v.field)}: <span class="prov ${GOOD.has(v.provenance) ? "good" : "weak"}">${esc(v.provenance || "n/a")}</span>`).join(" ");
    const cites = e.citations.map((c) => c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${ICON_LINK}${esc(c.label)}</a>`
      : `<span>${esc(c.label)}</span>`).join("");
    const rel = e.related_docs.length ? `<div class="rel">Same voucher: ${e.related_docs.map((r) => {
      const d = docById(r); return d ? `<img src="/img/${esc(d.image)}" data-doc="${esc(r)}" title="${esc(r)}"><span class="mono">${esc(r)}</span>` : esc(r);
    }).join(" ")}</div>` : "";
    return `<article class="card ex" style="animation-delay:${Math.min(i, 12) * 25}ms">
      <div class="ev">${crops}<img class="thumb" src="/img/${esc(e.image)}" data-doc="${esc(e.doc_id)}" alt="document"></div>
      <div>
        <h3>${esc(e.title)} <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span></h3>
        <div class="meta"><span class="mono">${esc(e.doc_id)}</span><span>${esc(clientName(e.client_id))}</span>${e.confidence !== "high" ? `<span>confidence: ${esc(e.confidence)}</span>` : ""}</div>
        <p class="msg">${esc(e.message)}</p>
        <div class="meta">${prov}</div>
        <div class="cite">${cites}</div>
        ${rel}
        <button class="linkbtn" data-doc="${esc(e.doc_id)}">Open document</button>
      </div></article>`;
  }).join("");
  document.querySelectorAll("#exList [data-doc]").forEach((el) => (el.onclick = () => openDoc(el.dataset.doc)));
}

function renderDocs() {
  const docs = S.inbox.docs.filter((d) => S.client === "ALL" || d.client_id === S.client);
  const flagged = new Set((S.run?.exceptions || []).map((e) => e.doc_id));
  $("#docGrid").innerHTML = docs.map((d) => {
    const ex = (S.run?.exceptions || []).filter((e) => e.doc_id === d.doc_id).map((e) => RULE_SHORT[e.rule_id]);
    return `<div class="card doc ${flagged.has(d.doc_id) ? "flag" : ""}" data-doc="${esc(d.doc_id)}">
      <img src="/img/${esc(d.image)}" alt="" loading="lazy"><div class="dn">${esc(d.doc_id)}</div>
      <div class="ds ${ex.length ? "" : "muted"}">${ex.length ? esc(ex.join(", ")) : S.run ? "no exceptions" : ""}</div></div>`;
  }).join("");
  document.querySelectorAll("#docGrid .doc").forEach((el) => (el.onclick = () => openDoc(el.dataset.doc)));
}

function openDoc(docId) {
  const d = docById(docId) || S.inbox.docs.find((x) => x.doc_id === docId);
  if (!d) return;
  $("#viewTitle").textContent = docId;
  const img = $("#viewImg");
  img.src = `/img/${d.image}`;
  const exs = (S.run?.exceptions || []).filter((e) => e.doc_id === docId);
  const want = new Set(exs.flatMap((e) => e.evidence.map((v) => v.field)));
  const box = $("#imgBox");
  box.querySelectorAll(".hl").forEach((h) => h.remove());
  const draw = () => {
    if (!d.boxes || !d.image_size) return;
    const [W, H] = d.image_size;
    Object.entries(d.boxes).forEach(([f, b]) => {
      if (want.size && !want.has(f)) return;
      const el = document.createElement("div");
      el.className = "hl";
      el.style.left = (b[0] / W) * 100 + "%"; el.style.top = (b[1] / H) * 100 + "%";
      el.style.width = ((b[2] - b[0]) / W) * 100 + "%"; el.style.height = ((b[3] - b[1]) / H) * 100 + "%";
      el.innerHTML = `<em>${esc(f)}</em>`;
      box.appendChild(el);
    });
  };
  img.complete ? draw() : (img.onload = draw);
  $("#viewEx").innerHTML = exs.length
    ? exs.map((e) => `<div class="vex"><b>${esc(e.title)}</b><br>${esc(e.message)}</div>`).join("")
    : `<p class="muted">${S.run ? "No exceptions on this document." : "Run the batch to audit this document."}</p>`;
  const f = d.fields || {};
  const p = d.provenance || {};
  const rows = [["Type", "doc_type"], ["Invoice no.", "invoice_no"], ["Date", "date"], ["Period header", "invoice_period"],
    ["Seller", "seller_name"], ["Seller UBN", "seller_ubn"], ["Buyer", "buyer_name"], ["Buyer UBN", "buyer_ubn"],
    ["Items", "items"], ["Net", "net"], ["Tax", "tax"], ["Total", "total"], ["Note", "note"], ["Category (model)", "category"]];
  $("#viewFields").innerHTML = d.fields ? rows.map(([k, key]) => {
    let v = f[key];
    if (key === "items") v = (v || []).map((i) => `${i.desc} x${i.qty ?? "?"} = ${i.amount ?? "?"}`).join("; ");
    const pv = p[key] ? `<span class="prov ${GOOD.has(p[key]) ? "good" : "weak"}">${esc(p[key])}</span>` : "";
    return `<tr><td>${k}</td><td>${esc(v ?? "")} ${pv}</td></tr>`;
  }).join("") : `<tr><td class="muted">Not read yet.</td></tr>`;
  $("#viewer").showModal();
}

function setTab(t) {
  S.tab = t;
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
  ["exceptions", "documents", "log"].forEach((x) => ($("#tab-" + x).hidden = x !== t));
  if (t === "log") renderLog();
}

async function renderLog() {
  if (!S.runId) { $("#logHead").innerHTML = `<p class="muted">No run yet.</p>`; $("#logTable").innerHTML = ""; return; }
  const L = await j(`/api/audit-log?run=${encodeURIComponent(S.runId)}`);
  $("#logHead").innerHTML = `<div class="verify ${L.verified ? "ok" : "bad"}">
    <svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>${L.verified ? '<path d="M9 12l2 2 4-4"/>' : ""}</svg>
    ${L.verified ? `Hash chain verified: ${L.entries_verified} entries, none altered` : `Hash chain broken after entry ${L.entries_verified}`}</div>
    <p class="muted small">Every read and every exception is logged with the document's SHA-256, the model and the device. Each entry carries the hash of the previous one,
    so edits are detectable. Supports Art. 19 of the bookkeepers' personal-data safeguard regulation (usage records and system logs).</p>`;
  $("#logTable").innerHTML = `<tr><th>#</th><th>Time</th><th>Event</th><th>Document</th><th>Detail</th><th>Hash</th></tr>` +
    L.entries.map((e) => `<tr><td>${e.seq}</td><td>${esc((e.ts || "").slice(11, 19))}</td><td>${esc(e.event)}</td>
      <td class="mono">${esc(e.doc_id || "")}</td>
      <td>${esc(e.rule_id || (e.event === "doc_read" ? `${e.model || ""} ${e.device || ""}${e.cached ? " (cached)" : ""}` : e.event === "run_start" ? e.network : e.event === "run_end" ? `${e.n_exceptions} exceptions` : ""))}</td>
      <td class="h">${esc((e.hash || "").slice(0, 12))}</td></tr>`).join("");
}

async function startRun(live) {
  $("#replayBtn").disabled = $("#liveBtn").disabled = true;
  $("#progress").hidden = false;
  $("#barFill").style.width = "0";
  $("#progText").textContent = live ? "Loading the model on this machine..." : "Replaying cached reads...";
  $("#progSub").textContent = "";
  try {
    const job = await j("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ set: S.set, live }) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 350));
      const s = await j("/api/job/" + job.id);
      if (s.n) {
        $("#barFill").style.width = (100 * s.i) / s.n + "%";
        $("#progText").textContent = `${s.i} / ${s.n}  ${s.doc_id || ""}`;
        $("#progSub").textContent = s.cached ? "cached read" : s.last_s ? `read in ${s.last_s} s on ${s.device || "local device"}` : s.status;
        const d = S.inbox.docs.find((x) => x.doc_id === s.doc_id);
        if (d) $("#progThumb").src = `/img/${d.image}`;
      } else {
        $("#progSub").textContent = s.status;
      }
      if (s.status === "done") { await loadRun(s.run_id); status(); break; }
      if (s.status === "error") { $("#progText").textContent = "Error: " + s.error; break; }
    }
  } finally {
    $("#replayBtn").disabled = $("#liveBtn").disabled = false;
  }
}

init();
