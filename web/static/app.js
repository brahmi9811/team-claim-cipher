// Live view for Claim Cipher. Plain JS, no build step.
// Data: initial REST load, then Server-Sent Events from web/api.py (change streams).
(() => {
  "use strict";

  // ---------- config ----------
  const params = new URLSearchParams(location.search);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  if (params.has("api")) store.set("cc_api", params.get("api") || null);
  const API = (store.get("cc_api") ?? (window.CC_CONFIG && window.CC_CONFIG.API_URL) ??
    (location.protocol === "file:" ? "http://localhost:8002" : "")).replace(/\/$/, "") ||
    (location.protocol === "file:" ? "http://localhost:8002" : "");

  const INSURERS = ["payer_a", "payer_b", "payer_c"];
  const INS_NAME = { payer_a: "Payer A", payer_b: "Payer B", payer_c: "Payer C" };
  const INS_SLOT = { payer_a: "s1", payer_b: "s2", payer_c: "s3" };
  const VERDICT = { legitimate: "Legitimate", wrongful_policy: "Wrongful · policy", wrongful_bulk: "Wrongful · bulk", needs_review: "Needs review" };
  const EVENT_TYPE = {
    rule_proposed: "Rule proposed", rule_promoted: "Rule promoted", rule_rejected: "Rule rejected", rule_retired: "Rule retired",
    profile_changed: "Profile changed", permission_changed: "Permission changed", guardrail_added: "Guardrail added",
    rollback: "Rolled back", phi_blocked: "PHI blocked",
  };
  const MAX_ROWS = 150;

  // ---------- state ----------
  const S = {
    adj: new Map(), appeals: new Map(), events: new Map(), profiles: new Map(),
    metrics: null, history: {}, filter: store.get("cc_filter") || "",
    fresh: new Set(),
  };

  // ---------- utils ----------
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x, d = 0) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
  const usd = (x) => (x == null ? "–" : x.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }));
  const usd2 = (x) => (x == null ? "–" : x.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 }));
  const tsOf = (d) => d && (d.adjudicated_at || d.ts || d.created_at || d.approved_at);
  const time = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
  const byTimeDesc = (a, b) => String(tsOf(b) || b._id).localeCompare(String(tsOf(a) || a._id));
  const visible = (d) => !S.filter || d.insurer === S.filter;
  const insTag = (ins) => `<span class="ins-name"><i class="dot ${INS_SLOT[ins] || ""}"></i>${esc(INS_NAME[ins] || ins)}</span>`;

  async function api(path, opts = {}) {
    const headers = { ...(opts.body ? { "Content-Type": "application/json" } : {}) };
    const token = store.get("cc_token");
    if (token) headers["X-Demo-Token"] = token;
    const res = await fetch(API + path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    if (res.status === 401 && opts.method === "POST") {
      const t = prompt("This backend is locked. Enter the demo token (DEMO_BUTTON_TOKEN):");
      if (t) { store.set("cc_token", t); return api(path, opts); }
    }
    const text = await res.text();
    let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `${res.status} ${res.statusText}`);
    return data;
  }

  let toastTimer;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("on"), 3500);
  }

  // Coalesce bursts of SSE updates into one render per frame. Browsers pause requestAnimationFrame
  // for pages they consider hidden (background tabs, some screen recorders, headless capture), so a
  // timer backs it up: whichever fires first renders.
  const dirty = new Set();
  let pending = false;
  function flush() {
    if (!pending) return;
    pending = false;
    if (dirty.has("claims")) renderClaims();
    if (dirty.has("appeals")) renderAppeals();
    if (dirty.has("events")) renderEvents();
    if (dirty.has("ladder")) renderLadder();
    if (dirty.has("score")) renderScore();
    dirty.clear();
  }
  function schedule(...what) {
    what.forEach((w) => dirty.add(w));
    if (pending) return;
    pending = true;
    requestAnimationFrame(flush);
    setTimeout(flush, 250);
  }

  function upsert(map, doc, isNew) {
    if (isNew && !map.has(doc._id)) { S.fresh.add(doc._id); setTimeout(() => S.fresh.delete(doc._id), 2500); }
    map.set(doc._id, doc);
    if (map.size > MAX_ROWS * 2) {
      const keep = [...map.values()].sort(byTimeDesc).slice(0, MAX_ROWS);
      map.clear(); keep.forEach((d) => map.set(d._id, d));
    }
  }

  // ---------- rendering: lists ----------
  function renderList(el, countEl, map, rowFn, emptyText) {
    const docs = [...map.values()].filter(visible).sort(byTimeDesc).slice(0, MAX_ROWS);
    countEl.textContent = docs.length ? `${docs.length}${docs.length === MAX_ROWS ? "+" : ""} shown` : "";
    const top = el.scrollTop;
    el.innerHTML = docs.length ? docs.map(rowFn).join("") : `<p class="empty">${esc(emptyText)}</p>`;
    el.scrollTop = top;
  }

  function claimRow(a) {
    const v = a.verdict;
    const status = a.status === "paid" ? `<span class="pill paid">Paid</span>` : `<span class="pill denied">Denied ${esc(a.carc || "")}</span>`;
    const verdict = a.status === "denied" ? (v ? `<span class="verdict ${esc(v.label)}">${esc(VERDICT[v.label] || v.label)}</span>` : `<span class="pill pending">judging</span>`) : "";
    const money = a.status === "paid" ? usd(a.paid_amount_usd) : "";
    const l2 = a.status === "denied"
      ? (v ? esc(v.reason) : esc(a.denial_text || ""))
      : `Paid in ${((a.latency_ms || 0) / 1000).toFixed(1)} s${a.attempt > 1 ? ` · attempt ${a.attempt} after fix` : ""}${a.holdout ? " · held-out" : ""}`;
    return `<div role="button" tabindex="0" class="row ${esc(a.insurer)} ${S.fresh.has(a._id) ? "new" : ""}" data-open="adj" data-id="${esc(a._id)}">
      <div class="l1"><span class="id">${esc(a.claim_id)}</span>${insTag(a.insurer)}${status}${verdict}<span class="right">${money}<span class="time">${time(a.adjudicated_at)}</span></span></div>
      <div class="l2">${l2}</div></div>`;
  }

  function appealStatus(p) {
    if (p.outcome === "overturned") return `<span class="pill overturned">Overturned ${usd(p.recovered_usd)}</span>`;
    if (p.outcome === "upheld") return `<span class="pill upheld">Upheld</span>`;
    if (p.status === "approved") return `<span class="pill approved">Approved · filing</span>`;
    if (p.status === "filed") return `<span class="pill pending">Filed</span>`;
    return p.mode === "auto_file" ? `<span class="pill pending">Filing</span>` : `<span class="pill draft">Draft · needs approval</span>`;
  }
  function needsApproval(p) { return !p.outcome && p.mode !== "auto_file" && !["approved", "filed"].includes(p.status); }

  function appealRow(p) {
    const approve = needsApproval(p) ? `<span class="approve" role="button" tabindex="0" data-approve="${esc(p._id)}">Approve</span>` : "";
    const ev = p.evidence || {};
    const bits = [
      (ev.clause_ids || []).length && `${ev.clause_ids.length} clause${ev.clause_ids.length > 1 ? "s" : ""}`,
      (ev.comparable_claim_ids || []).length && `${ev.comparable_claim_ids.length} paid comparables`,
      ev.pattern_stats && ev.pattern_stats.identical_denials_60s != null && `${ev.pattern_stats.identical_denials_60s} identical denials/60 s`,
    ].filter(Boolean).join(" · ");
    return `<div role="button" tabindex="0" class="row ${esc(p.insurer)} ${S.fresh.has(p._id) ? "new" : ""}" data-open="appeal" data-id="${esc(p._id)}">
      <div class="l1"><span class="id">${esc(p.claim_id)}</span>${insTag(p.insurer)}<span class="verdict ${esc(p.verdict)}">${esc(VERDICT[p.verdict] || p.verdict)}</span><span class="right">${appealStatus(p)}${approve}</span></div>
      <div class="l2">${esc(bits || "evidence pending")} · confidence ${pct(p.confidence)}</div></div>`;
  }

  // Only the leaves that changed. Works for both event shapes: {field: value} pairs, and whole
  // profile/rule documents (the orchestrator logs full before/after docs).
  const DIFF_SKIP = new Set(["_id", "_seq", "version", "updated_by", "last_event_id", "rule", "recent_outcomes", "last_used_at", "created_at", "replay", "hit_count", "evidence_ids", "condition", "fix"]);
  function leaves(obj, prefix = "", out = {}) {
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      for (const [k, v] of Object.entries(obj)) {
        if (!prefix && DIFF_SKIP.has(k)) continue;
        leaves(v, prefix ? `${prefix}.${k}` : k, out);
      }
    } else if (prefix) out[prefix] = obj;
    return out;
  }
  function flatDiff(before, after) {
    const b = leaves(before), a = leaves(after);
    const keys = [...new Set([...Object.keys(b), ...Object.keys(a)])].filter((k) => JSON.stringify(b[k]) !== JSON.stringify(a[k]));
    return keys.slice(0, 6).map((k) => `<span class="diff">${esc(k)}: ${k in b ? `<span class="b">${esc(JSON.stringify(b[k]))}</span> → ` : ""}<span class="a">${esc(JSON.stringify(a[k]))}</span></span>`).join(" ");
  }
  const ruleOf = (e) => (e.after && (e.after.rule || (e.after.condition ? e.after : null))) || (e.before && e.before.condition ? e.before : null);
  function ruleText(rule) {
    if (!rule || !rule.condition) return "";
    const conds = (rule.condition.all || rule.condition.any || []).map((c) => `${c.field} ${c.op}${c.value !== undefined ? " " + JSON.stringify(c.value) : ""}`);
    const fix = rule.fix ? `${rule.fix.action}${Object.keys(rule.fix).length > 1 ? " " + JSON.stringify(Object.fromEntries(Object.entries(rule.fix).filter(([k]) => k !== "action"))) : ""}` : "";
    return `IF ${conds.join(rule.condition.any ? " OR " : " AND ")} → ${fix}`;
  }

  function eventRow(e) {
    const rule = ruleOf(e);
    const change = rule ? `<span class="diff">${esc(ruleText(rule))}</span>` : flatDiff(e.before, e.after);
    return `<div role="button" tabindex="0" class="row ${esc(e.insurer)} ${S.fresh.has(e._id) ? "new" : ""}" data-open="event" data-id="${esc(e._id)}">
      <div class="l1"><b class="${e.reverted ? "reverted" : ""}">${esc(EVENT_TYPE[e.type] || e.type)}</b>${insTag(e.insurer)}${e.reverted ? `<span class="pill upheld">reverted</span>` : ""}<span class="right"><span class="time">${esc(e.actor || "")} · ${time(e.ts)}</span></span></div>
      <div class="l2">${change}</div>
      <div class="l2">${esc(e.reason || "")}</div></div>`;
  }

  const renderClaims = () => renderList($("claims"), $("c-claims"), S.adj, claimRow, "Waiting for claims…");
  const renderAppeals = () => renderList($("appeals"), $("c-appeals"), S.appeals, appealRow, "No appeals yet.");
  const renderEvents = () => renderList($("events"), $("c-events"), S.events, eventRow, "No harness changes yet.");

  function renderLadder() {
    $("ladder").innerHTML = INSURERS.map((ins) => {
      const p = S.profiles.get(ins);
      if (!p) return `<div><b>${INS_NAME[ins]}</b><span>no profile</span></div>`;
      const mode = (p.permissions && p.permissions.appeals) || "draft_only";
      return `<div><b><i class="dot ${INS_SLOT[ins]}"></i>${INS_NAME[ins]} · v${esc(p.version)}</b>
        <span>${mode === "auto_file" ? '<span class="pill auto">auto-file</span>' : '<span class="pill draft">draft-only</span>'}</span>
        <span>lead: ${esc((p.appeal_strategy || {}).lead_with || "–")} · min conf ${esc((p.judge || {}).min_confidence ?? "–")}</span></div>`;
    }).join("");
  }

  // ---------- rendering: scoreboard ----------
  function spark(ins, rows) {
    const W = 180, H = 48, P = 3;
    const pts = rows.filter((r) => r.acceptance_rate != null);
    if (pts.length < 2) return `<svg class="spark" viewBox="0 0 ${W} ${H}"></svg>`;
    const ys = pts.map((r) => r.acceptance_rate);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (hi - lo < 0.05) { const m = (hi + lo) / 2; lo = m - 0.025; hi = m + 0.025; }
    const x = (i) => P + (i / (pts.length - 1)) * (W - 2 * P);
    const y = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);
    const d = pts.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r.acceptance_rate).toFixed(1)}`).join("");
    const last = pts[pts.length - 1];
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" data-ins="${ins}" aria-label="${INS_NAME[ins]} acceptance over time">
      <line class="grid" x1="0" x2="${W}" y1="${H - P}" y2="${H - P}"/>
      <path class="line" d="${d}" style="stroke:var(--${INS_SLOT[ins]})"/>
      <line class="cross" x1="0" x2="0" y1="0" y2="${H}" style="display:none"/>
      <circle class="end" cx="${x(pts.length - 1)}" cy="${y(last.acceptance_rate)}" r="4" style="fill:var(--${INS_SLOT[ins]})"/>
    </svg>`;
  }

  function renderScore() {
    const m = S.metrics;
    if (!m) return;
    const t = m.totals || {};
    $("m-acc").textContent = pct(t.acceptance_rate);
    const gain = t.acceptance_rate != null && t.start_acceptance_rate != null ? t.acceptance_rate - t.start_acceptance_rate : null;
    $("m-acc-from").innerHTML = t.start_acceptance_rate != null
      ? `from ${pct(t.start_acceptance_rate)} at start ${gain != null && Math.abs(gain) >= 0.005 ? `<span class="${gain > 0 ? "up" : ""}">${gain > 0 ? "▲ +" : "▼ "}${(gain * 100).toFixed(0)} pts</span>` : ""}`
      : "across all insurers";
    $("m-rec").textContent = usd(t.recovered_usd);
    $("m-win").textContent = `appeal win rate ${pct(t.appeal_win_rate)}`;
    const phi = $("m-phi");
    phi.textContent = t.phi_leaks_to_llm == null ? "–" : String(t.phi_leaks_to_llm);
    phi.classList.toggle("bad", (t.phi_leaks_to_llm || 0) > 0);
    $("m-blocked").textContent = `${t.leaks_blocked ?? 0} leak${t.leaks_blocked === 1 ? "" : "s"} blocked by the firewall`;
    $("m-judge").textContent = `${pct(t.judge_precision)} / ${pct(t.judge_recall)}`;
    $("m-cost").textContent = usd2(t.cost_usd_per_claim);
    $("m-at").textContent = m.at ? `updated ${time(m.at)}` : "";

    const ins = INSURERS.filter((i) => !S.filter || i === S.filter);
    $("insurers").innerHTML = ins.map((i) => {
      const d = (m.insurers || {})[i] || {};
      const p = S.profiles.get(i);
      const perm = p && p.permissions && p.permissions.appeals === "auto_file" ? "auto-file" : "draft-only";
      return `<div class="ins">
        <h3><span><i class="dot ${INS_SLOT[i]}"></i>${INS_NAME[i]}</span><span class="perm">appeals: ${perm}</span></h3>
        <div class="nums">
          <span><b>${pct(d.acceptance_rate)}</b>acceptance</span>
          <span><b>${pct(d.appeal_win_rate)}</b>appeal wins</span>
          <span><b>${usd(d.recovered_usd)}</b>recovered</span>
        </div>
        <div>${spark(i, S.history[i] || [])}<div class="spark-cap">acceptance, ${S.history[i] && S.history[i].length ? `since ${time(S.history[i][0].ts)}` : "no history"}</div></div>
      </div>`;
    }).join("");
  }

  // Sparkline hover: crosshair + tooltip with the value under the pointer.
  const tip = $("tip");
  document.addEventListener("pointermove", (ev) => {
    const svg = ev.target.closest && ev.target.closest("svg.spark[data-ins]");
    if (!svg) { tip.style.display = "none"; document.querySelectorAll(".spark .cross").forEach((c) => (c.style.display = "none")); return; }
    const rows = (S.history[svg.dataset.ins] || []).filter((r) => r.acceptance_rate != null);
    if (!rows.length) return;
    const box = svg.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (ev.clientX - box.left) / box.width));
    const i = Math.round(f * (rows.length - 1));
    const cross = svg.querySelector(".cross");
    const cx = 3 + (i / Math.max(1, rows.length - 1)) * 174;
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.style.display = "";
    tip.innerHTML = `<b>${pct(rows[i].acceptance_rate, 1)}</b> acceptance · ${time(rows[i].ts)}`;
    tip.style.display = "block";
    tip.style.left = `${Math.min(ev.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${ev.clientY - 34}px`;
  });

  // ---------- drawer ("Why?") ----------
  const drawer = $("drawer"), scrim = $("scrim");
  function openDrawer(kicker, title, html) {
    $("d-kicker").textContent = kicker;
    $("d-title").textContent = title;
    $("d-body").innerHTML = html;
    drawer.classList.add("on"); scrim.classList.add("on");
    drawer.setAttribute("aria-hidden", "false");
    $("d-close").focus();
  }
  function closeDrawer() {
    drawer.classList.remove("on"); scrim.classList.remove("on"); drawer.setAttribute("aria-hidden", "true");
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  }
  // Deep links (#adj=adj_123, #event=evt_9, #appeal=apl_4) so the presenter can bookmark the demo moments.
  const OPENERS = { adj: (id) => showAdjudication(id), event: (id) => showEvent(id), appeal: (id) => showAppeal(id) };
  function openFromHash() {
    const m = location.hash.match(/^#(adj|event|appeal)=(.+)$/);
    if (m) OPENERS[m[1]](decodeURIComponent(m[2]));
  }
  window.addEventListener("hashchange", openFromHash);
  $("d-close").onclick = closeDrawer; scrim.onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  const sec = (h, body) => (body ? `<section><h4>${esc(h)}</h4>${body}</section>` : "");
  const kv = (pairs) => `<dl class="kv">${pairs.filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const linkAdj = (id, label) => `<button class="link" data-open="adj" data-id="${esc(id)}">${esc(label || id)}</button>`;

  async function showAdjudication(id) {
    openDrawer("Why?", "Loading…", "");
    let r;
    try { r = await api(`/adjudications/${encodeURIComponent(id)}`); } catch (e) { return openDrawer("Why?", "Not found", `<p>${esc(e.message)}</p>`); }
    const a = r.adjudication, v = a.verdict, ev = (v && v.evidence) || {}, c = r.claim || {};
    const head = kv([
      ["Insurer", insTag(a.insurer)],
      ["Status", a.status === "paid" ? `<span class="pill paid">Paid</span> ${usd(a.paid_amount_usd)}` : `<span class="pill denied">Denied</span> ${esc(a.carc || "")} ${esc(a.rarc || "")}`],
      ["Denial text", esc(a.denial_text)],
      ["Time to decision", a.latency_ms != null ? `${(a.latency_ms / 1000).toFixed(2)} s after submission` : null],
      ["Attempt", a.attempt],
      ["Held-out", a.holdout ? "yes (scored, never used for learning)" : null],
    ]);
    const verdict = v ? `<p class="reason"><span class="verdict ${esc(v.label)}">${esc(VERDICT[v.label] || v.label)}</span> ${esc(v.reason)}</p>
      <div style="margin-top:10px">${kv([["Confidence", `${pct(v.confidence)}<div class="meter"><i style="width:${(v.confidence || 0) * 100}%"></i></div>`], ["Model", esc(v.model)], ["Decided", time(v.created_at)]])}</div>` : (a.status === "denied" ? "<p>The Judge hasn't classified this denial yet.</p>" : "");
    const clauses = (r.clauses || []).map((cl) => `<div class="clause"><b>${esc(INS_NAME[cl.insurer] || cl.insurer)} policy v${esc(cl.version)}, clause ${esc(cl.clause_no)}: ${esc(cl.title)}${cl.current === false ? " (superseded)" : ""}</b>${esc(cl.clause_text)}</div>`).join("")
      || ((ev.clause_ids || []).length ? `<p>${ev.clause_ids.map(esc).join(", ")} (text not loaded)</p>` : "");
    const comps = (r.comparables || []).length ? `<table class="mini"><tr><th>Claim</th><th>Codes</th><th>Outcome</th><th>Paid</th></tr>${r.comparables.map((x) => `<tr><td>${linkAdj(x._id, x.claim_id)}</td><td>${esc(((x.claim || {}).lines || []).map((l) => l.hcpcs).join(", "))}</td><td>${x.status === "paid" ? '<span class="pill paid">Paid</span>' : `<span class="pill denied">Denied ${esc(x.carc || "")}</span>`}</td><td>${usd(x.paid_amount_usd)}</td></tr>`).join("")}</table>` : "";
    const ps = ev.pattern_stats ? kv(Object.entries(ev.pattern_stats).map(([k, val]) => [k.replace(/_/g, " "), esc(val)])) : "";
    const lines = (c.lines || []).length ? `<table class="mini"><tr><th>#</th><th>HCPCS</th><th>Mod</th><th>Units</th><th>Charge</th></tr>${c.lines.map((l) => `<tr><td>${esc(l.line_no)}</td><td>${esc(l.hcpcs)}</td><td>${esc((l.modifiers || []).join(", "))}</td><td>${esc(l.units)}</td><td>${usd(l.charge_usd)}</td></tr>`).join("")}</table>
      ${kv([["Diagnoses (ICD-10-CM)", esc((c.diagnosis_codes || []).join(", "))], ["Encounter", esc(c.encounter_type)], ["Prior auth on claim", c.prior_auth_id ? "yes" : "missing"], ["Total", usd(c.total_charge_usd)]])}
      <p class="spark-cap" style="text-align:left;margin:8px 0 0">Patient fields are encrypted in MongoDB and never sent to this page.</p>` : "";
    const sr = v && v.suggested_rule ? `<p class="diff">${esc(ruleText(v.suggested_rule))}</p>` : "";
    const appeal = r.appeal ? `${kv([["Appeal", `<button class="link" data-open="appeal" data-id="${esc(r.appeal._id)}">${esc(r.appeal._id)}</button>`], ["Status", appealStatus(r.appeal)]])}` : "";
    openDrawer(`Why? · ${a._id}`, `Claim ${a.claim_id}`,
      sec("Verdict", verdict) + sec("Denial", head) + sec("Policy clauses retrieved", clauses) + sec("Paid comparable claims", comps) +
      sec("Denial pattern", ps) + sec("Suggested prevention rule", sr) + sec("Appeal", appeal) + sec("Claim (tokenized view)", lines));
  }

  async function showEvent(id) {
    openDrawer("Why?", "Loading…", "");
    let r;
    try { r = await api(`/events/${encodeURIComponent(id)}`); } catch (e) { return openDrawer("Why?", "Not found", `<p>${esc(e.message)}</p>`); }
    const e = r.event;
    const rule = ruleOf(e);
    const change = `${kv([["Actor", esc(e.actor)], ["When", time(e.ts)], ["Insurer", insTag(e.insurer)], ["Reverted", e.reverted ? '<span class="pill upheld">yes</span>' : null]])}
      <div style="margin-top:10px">${flatDiff(e.before, e.after)}</div>${rule ? `<p class="diff" style="margin-top:8px">${esc(ruleText(rule))}</p>` : ""}`;
    const evd = r.evidence || {};
    const adjs = (evd.adjudications || []).length ? `<table class="mini"><tr><th>Claim</th><th>Outcome</th><th>Verdict</th></tr>${evd.adjudications.map((x) => `<tr><td>${linkAdj(x._id, x.claim_id)}</td><td>${x.status === "paid" ? '<span class="pill paid">Paid</span>' : `<span class="pill denied">Denied ${esc(x.carc || "")}</span>`}</td><td>${x.verdict ? `<span class="verdict ${esc(x.verdict.label)}">${esc(VERDICT[x.verdict.label] || x.verdict.label)}</span>` : "–"}</td></tr>`).join("")}</table>` : "";
    const apls = (evd.appeals || []).map((p) => `<div><button class="link" data-open="appeal" data-id="${esc(p._id)}">${esc(p._id)}</button> ${appealStatus(p)}</div>`).join("");
    const rules = (evd.rules || []).map((ru) => `<div class="clause"><b>${esc(ru._id)} · ${esc(ru.status)}</b><span class="diff">${esc(ruleText(ru))}</span>${ru.replay ? `<div>replay: prevents ${esc(ru.replay.prevented)}, false blocks ${esc(ru.replay.false_blocks)}, precision ${pct(ru.replay.precision)}</div>` : ""}</div>`).join("");
    const listedIds = (e.evidence_ids || []).length ? `<p class="spark-cap" style="text-align:left">evidence ids: ${e.evidence_ids.map(esc).join(", ")}</p>` : "";
    const earlier = (r.earlier_events || []).map((x) => `<div><button class="link" data-open="event" data-id="${esc(x._id)}">${esc(EVENT_TYPE[x.type] || x.type)}</button> <span class="time">${time(x.ts)}</span> ${x.reverted ? "(reverted)" : ""}</div>`).join("");
    openDrawer(`Why? · ${e._id}`, `${EVENT_TYPE[e.type] || e.type} · ${INS_NAME[e.insurer] || e.insurer || ""}`,
      sec("Reason", `<p class="reason">${esc(e.reason || "No reason recorded.")}</p>`) + sec("Change", change) +
      sec("Evidence: adjudications", adjs) + sec("Evidence: appeals", apls) + sec("Evidence: rules", rules) + (adjs || apls || rules ? "" : sec("Evidence", listedIds)) +
      sec(`Earlier changes for ${INS_NAME[e.insurer] || "this insurer"}`, earlier));
  }

  async function showAppeal(id) {
    openDrawer("Appeal", "Loading…", "");
    let p;
    try { p = await api(`/appeals/${encodeURIComponent(id)}`); } catch (e) { return openDrawer("Appeal", "Not found", `<p>${esc(e.message)}</p>`); }
    const ev = p.evidence || {};
    const adjLink = p.adjudication_id ? linkAdj(p.adjudication_id, "open the denial and verdict") : "";
    openDrawer(`Appeal · ${p._id}`, `Claim ${p.claim_id}`,
      sec("Status", `${kv([["Insurer", insTag(p.insurer)], ["Verdict", `<span class="verdict ${esc(p.verdict)}">${esc(VERDICT[p.verdict] || p.verdict)}</span> at ${pct(p.confidence)}`], ["Mode", esc(p.mode === "auto_file" ? "auto-file" : "draft-only (human approval)")], ["Outcome", appealStatus(p)], ["Recovered", p.recovered_usd ? usd(p.recovered_usd) : null], ["Approved", p.approved_at ? `${esc(p.approved_by || "")} at ${time(p.approved_at)}` : null], ["Why", adjLink]])}
        ${needsApproval(p) ? `<p><button class="btn" data-approve="${esc(p._id)}">Approve and file</button></p>` : ""}`) +
      sec("Evidence cited", kv([["Clauses", esc((ev.clause_ids || []).join(", "))], ["Paid comparables", esc((ev.comparable_claim_ids || []).join(", "))], ["Pattern", ev.pattern_stats ? esc(Object.entries(ev.pattern_stats).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")) : null]])) +
      sec("Letter (tokenized, as the LLM wrote it)", `<pre class="letter">${esc(p.letter_tokenized || "")}</pre><p class="spark-cap" style="text-align:left;margin-top:6px">Real patient values are filled in only when the final letter is rendered, outside the LLM.</p>`));
  }

  // ---------- actions ----------
  async function approve(id, el) {
    if (el) el.setAttribute("aria-disabled", "true");
    try {
      const doc = await api(`/appeals/${encodeURIComponent(id)}/approve`, { method: "POST" });
      upsert(S.appeals, doc); schedule("appeals");
      toast(`Appeal ${id} approved; the orchestrator will file it.`);
      if (drawer.classList.contains("on") && $("d-kicker").textContent.includes(id)) showAppeal(id);
    } catch (e) { toast(`Approve failed: ${e.message}`); }
  }

  document.addEventListener("click", (ev) => {
    const ap = ev.target.closest("[data-approve]");
    if (ap) { ev.stopPropagation(); ev.preventDefault(); approve(ap.dataset.approve, ap); return; }
    const op = ev.target.closest("[data-open]");
    if (!op) return;
    const { open, id } = op.dataset;
    history.replaceState(null, "", `#${open}=${encodeURIComponent(id)}`);
    OPENERS[open](id);
  });
  document.addEventListener("keydown", (ev) => {
    if ((ev.key !== "Enter" && ev.key !== " ") || !ev.target.matches) return;
    if (ev.target.matches("[data-approve]")) { ev.preventDefault(); ev.stopPropagation(); approve(ev.target.dataset.approve, ev.target); }
    else if (ev.target.matches("div[data-open]")) { ev.preventDefault(); ev.target.click(); }
  }, true);

  $("filter").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    S.filter = b.dataset.ins;
    store.set("cc_filter", S.filter || null);
    syncFilter();
    schedule("claims", "appeals", "events", "score");
  });
  function syncFilter() { $("filter").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.ins === S.filter)); }

  $("btn-policy").onclick = async () => {
    if (!confirm("Payer C publishes a new policy version and changes 3 hidden rules. Continue?")) return;
    const b = $("btn-policy"); b.disabled = true;
    try { const r = await api("/demo/policy-change", { method: "POST" }); toast(`Payer C policy changed: ${typeof r.simulator === "object" ? JSON.stringify(r.simulator) : r.simulator}`); }
    catch (e) { toast(`Policy change failed: ${e.message}`); }
    finally { b.disabled = false; }
  };
  $("btn-reset").onclick = async () => {
    if (prompt("This resets the demo state in MongoDB. Only do this if the live demo is broken.\nType RESET to continue:") !== "RESET") return;
    const b = $("btn-reset"); b.disabled = true; toast("Resetting…");
    try { const r = await api("/demo/reset", { method: "POST", body: JSON.stringify({ confirm: "RESET" }) }); toast(`Reset done in ${r.seconds} s`); await loadAll(); }
    catch (e) { toast(`Reset failed: ${e.message}`); }
    finally { b.disabled = false; }
  };

  // ---------- data ----------
  async function loadAll() {
    const [adj, appeals, events, profiles, metrics, history] = await Promise.all([
      api("/adjudications?limit=120"), api("/appeals?limit=120"), api("/events?limit=120"), api("/profiles"), api("/metrics/latest"), api("/metrics/history?points=240"),
    ]);
    S.adj.clear(); S.appeals.clear(); S.events.clear(); S.profiles.clear();
    adj.forEach((d) => S.adj.set(d._id, d));
    appeals.forEach((d) => S.appeals.set(d._id, d));
    events.forEach((d) => S.events.set(d._id, d));
    profiles.forEach((d) => S.profiles.set(d._id, d));
    S.metrics = metrics; S.history = history;
    schedule("claims", "appeals", "events", "ladder", "score");
  }

  function onMetrics(m) {
    S.metrics = m;
    for (const [ins, d] of Object.entries(m.insurers || {})) {
      const h = (S.history[ins] = S.history[ins] || []);
      const last = h[h.length - 1];
      if (d.ts && (!last || d.ts > last.ts)) { h.push({ ts: d.ts, acceptance_rate: d.acceptance_rate, appeal_win_rate: d.appeal_win_rate, recovered_usd: d.recovered_usd }); if (h.length > 480) h.shift(); }
    }
    schedule("score");
  }

  function setConn(state, text) { const c = $("conn"); c.dataset.state = state; c.querySelector("span").textContent = text; }

  let es, reloadTimer;
  function connect() {
    es = new EventSource(`${API}/stream`);
    es.addEventListener("hello", (e) => {
      const modes = Object.values(JSON.parse(e.data).streams || {});
      setConn("live", modes.includes("polling") ? "live (polling)" : "live");
    });
    es.onerror = () => setConn("down", "reconnecting…");
    const handler = (map, what) => (e) => {
      const msg = JSON.parse(e.data);
      if (msg.op === "delete") map.delete(msg.id); else upsert(map, msg.doc, msg.op === "insert");
      schedule(...what);
    };
    es.addEventListener("adjudication", handler(S.adj, ["claims"]));
    es.addEventListener("appeal", handler(S.appeals, ["appeals"]));
    es.addEventListener("harness_event", handler(S.events, ["events"]));
    es.addEventListener("profile", handler(S.profiles, ["ladder", "score"]));
    es.addEventListener("metrics", (e) => onMetrics(JSON.parse(e.data)));
    es.addEventListener("reset", () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => loadAll().catch(() => {}), 500); });
  }

  // Keep the relative times fresh without re-rendering constantly.
  setInterval(() => schedule("score"), 30000);

  syncFilter();
  loadAll().then(openFromHash).catch((e) => { setConn("down", "backend unreachable"); toast(`Can't reach ${API || location.origin}: ${e.message}`); });
  connect();
})();
