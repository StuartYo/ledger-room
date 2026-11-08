"use strict";
// The agent part of the overview, counted in one place so the strip and the line under it use the same definition.
// No DOM here: tests/test_overview_agent_counts.py runs these functions in node.

// docs: the run's documents (result.json "docs"); held: ids of documents still in the queue (any reason).
//   verified   the agent checked it and code verified every value it changed (a person has not rejected it)
//   cleared    verified and nothing else holds it: the "agent-checked, code-verified" part of the strip
//   stillHeld  verified, but held for another reason (a rule finding such as a date outside the period)
//   rejected   verified by code, then rejected by a person (back to its first reading)
function agentSplit(docs, held) {
  const ver = (docs || []).filter((d) => d && d.agent && d.agent.status === "verified");
  const kept = ver.filter((d) => !d.agent.overridden);
  const cleared = kept.filter((d) => !held.has(d.doc_id)).length;
  return { verified: ver.length, cleared, stillHeld: kept.length - cleared, rejected: ver.length - kept.length };
}

// The line under the strip. A: the run summary's agent block; tr: the page's t(key, vars).
// "The agent checked 7 held vouchers; code verified 2: 1 cleared, 1 still needs your decision for another reason"
// Empty when the agent step did not run and nothing was replayed.
function agentMetaText(A, sp, tr) {
  A = A || {};
  let head = "";
  if (A.ran) {
    if (!A.tried) return tr("metaAgentNone");
    head = tr("metaAgent", { m: A.model || "", n: A.tried, v: sp.verified });
  } else if (A.replay && A.tried) {
    head = tr("metaAgentReplay", { n: A.tried, v: sp.verified });
  } else {
    return "";
  }
  // second line: where the verified ones went (the page shows "\n" as a line break)
  const left = sp.verified - sp.rejected, parts = [];
  if (left > 0 && sp.stillHeld === 0) parts.push(tr("metaAgentAllClear"));
  else if (left > 0 && sp.cleared === 0) parts.push(tr("metaAgentNoneClear"));
  else if (left > 0) parts.push(tr("metaAgentSplit", { a: sp.cleared, b: sp.stillHeld }));
  if (sp.rejected) parts.push(tr("metaAgentRejected", { r: sp.rejected }));
  return parts.length ? head + "\n" + parts.join(tr("metaAgentSep")) : head;
}

if (typeof module !== "undefined") module.exports = { agentSplit, agentMetaText };
