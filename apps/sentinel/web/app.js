"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (url) => (typeof url === "string" && /^https?:\/\//i.test(url) ? url : "");
const link = (url, label, cls = "") => (safeUrl(url) ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : "");
const linkIcon = (url, icon, label, cls = "") => (safeUrl(url) ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${icon}${esc(label)}</a>` : "");
// Generic, non-trademarked glyphs that signal "GitHub" (a branch/version-control mark) and
// "ServiceNow" (a cloud ticketing mark) wherever those two systems are linked from the UI.
const ICON_GITHUB = `<svg class="icon-inline" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="4" cy="3" r="1.6"/><circle cx="4" cy="13" r="1.6"/><circle cx="12" cy="6" r="1.6"/><path d="M4 4.6V11.4"/><path d="M4 8c0-2 1.8-3.6 4-3.6h2.6"/></svg>`;
const ICON_SERVICENOW = `<svg class="icon-inline" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 11.5h6.2a2.3 2.3 0 0 0 .3-4.6 3.4 3.4 0 0 0-6.5-1 2.6 2.6 0 0 0-2.6 2.6c0 .2 0 .4.1.6a2.1 2.1 0 0 0 .5 3.4Z"/><path d="M6.3 9.1l1.1 1.1 2.3-2.3"/></svg>`;
const fmtTime = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
};

let state = null;
let lastRendered = {};
let approved = {};
let busy = false;
let pollTimer = null;

/* ---------------------------------------------------------------- theme */
const root = document.documentElement;
try { const saved = localStorage.getItem("sentinel-theme"); if (saved) root.dataset.theme = saved; } catch (_) { /* storage blocked */ }
$("theme").addEventListener("click", () => {
  const dark = root.dataset.theme === "dark";
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("sentinel-theme", root.dataset.theme); } catch (_) { /* ignore */ }
});

/* -------------------------------------------------------- responsive navigation */
const sidebar = $("sidebar");
const menuToggle = $("menu-toggle");
const mobileScrim = $("mobile-scrim");
function closeMenu() {
  sidebar.classList.remove("open");
  mobileScrim.classList.remove("visible");
  menuToggle.setAttribute("aria-expanded", "false");
  menuToggle.setAttribute("aria-label", "Open navigation");
}
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenu(); });
menuToggle.addEventListener("click", () => {
  const open = sidebar.classList.toggle("open");
  mobileScrim.classList.toggle("visible", open);
  menuToggle.setAttribute("aria-expanded", String(open));
  menuToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
});
mobileScrim.addEventListener("click", closeMenu);
document.querySelectorAll(".side-nav a").forEach((item) => item.addEventListener("click", () => {
  document.querySelectorAll(".side-nav a").forEach((nav) => {
    nav.classList.toggle("active", nav === item);
    if (nav === item) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  });
  closeMenu();
}));

/* ------------------------------------------------------------------ api */
async function api(path, body) {
  const options = body === undefined
    ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json", "X-Sentinel": "1" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 7000);
}

async function act(button, path, body) {
  if (busy) return;
  busy = true;
  button?.classList.add("busy");
  if (button) button.disabled = true;
  try {
    await api(path, body);
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    busy = false;
    button?.classList.remove("busy");
    if (button) button.disabled = false;
    if (state) render(state, true);
  }
}

/* ------------------------------------------------------------- markdown */
function inline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\b(https?:\/\/[^\s<)]+)/g, (m) => `<a href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`);
}
function markdown(text) {
  const out = [];
  let list = false;
  for (const raw of String(text ?? "").split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(bullet[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (/^---+$/.test(line.trim())) out.push("<hr>");
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}

/* -------------------------------------------------------------- renders */
function setHtml(id, html) {
  if (lastRendered[id] === html) return;
  lastRendered[id] = html;
  $(id).innerHTML = html;
}

function renderPills(s) {
  const i = s.integrations;
  const pill = (label, kind, icon = "") => `<span class="pill ${kind}"><i></i>${icon}${esc(label)}</span>`;
  setHtml("pills", [
    pill(i.mode === "live" ? "Live evidence" : "Fixture evidence", i.mode === "live" ? "ok" : "warn"),
    pill(i.servicenow ? "ServiceNow configured" : "ServiceNow off", i.servicenow ? "warn" : "", ICON_SERVICENOW),
    pill(i.github ? "GitHub configured" : "GitHub off", i.github ? "warn" : "", ICON_GITHUB),
    pill(s.webhook.status === "listening" ? "Alert receiver ready" : "Alert receiver unavailable", s.webhook.status === "listening" ? "ok" : "bad"),
  ].join(""));
  const demo = $("demo-link");
  if (safeUrl(i.demo_app_url)) { demo.href = i.demo_app_url; demo.hidden = false; }
}

function renderStepper(s) {
  const ev = s.evidence || {};
  const hasEvidence = (ev.groups || []).some((g) => g.records.length);
  const verdict = ev.verdict;
  const repair = s.repair || {};
  const resolved = (s.ticket || {}).state === "Resolved" || repair.state === "resolved";
  const steps = [
    { name: "Alert", sub: s.source.title || "waiting", done: s.source.kind !== "idle", active: s.source.kind === "idle" },
    { name: "Evidence", sub: hasEvidence ? "collected" : "gathering", done: hasEvidence, active: s.source.kind !== "idle" && !hasEvidence },
    { name: "Verdict", sub: verdict ? verdict.label : "pending", done: !!verdict, bad: !!verdict && !verdict.supported && !verdict.insufficient },
    { name: "Fix", sub: ev.fix ? (ev.fix.all_passed ? "validated" : "rejected") : "pending", done: !!ev.fix && ev.fix.all_passed, bad: !!ev.fix && !ev.fix.all_passed },
    { name: "Human review", sub: repair.pr ? (repair.pr.merged ? "merged" : "PR #" + repair.pr.number) : "pending", done: !!(repair.pr && repair.pr.merged) },
    { name: "Recovered", sub: resolved ? "resolved" : "pending", done: resolved },
  ];
  let activeSet = false;
  const html = steps.map((step) => {
    let cls = "";
    if (step.bad) cls = "bad";
    else if (step.done) cls = "done";
    else if (!activeSet && s.source.kind !== "idle") { cls = "active"; activeSet = true; }
    else if (!activeSet && step.name === "Alert") { cls = "active"; activeSet = true; }
    return `<div class="step ${cls}"><span>${esc(step.name)}</span><small>${esc(step.sub)}</small></div>`;
  }).join("");
  setHtml("stepper", html);
}

function renderBanner(s) {
  const el = $("banner");
  let html = "";
  let cls = "banner";
  if (s.running) html = `<span class="spinner"></span>Sentinel is investigating. Evidence, the verdict and the fix appear here as they arrive.`;
  else if (s.source.state === "failed") { html = `Investigation failed: ${esc(s.source.error || "unknown error")}`; cls += " bad"; }
  else if ((s.ticket || {}).state === "Resolved") { html = "Incident resolved. The merged fix is live and checkout recovery was verified."; cls += " ok"; }
  if (html !== lastRendered.banner) {
    lastRendered.banner = html;
    el.className = cls;
    el.innerHTML = html;
    el.hidden = !html;
  }
}

function renderTicket(s) {
  const t = s.ticket;
  if (!t) {
    setHtml("ticket", s.running
      ? `<div class="skeleton"></div><div class="skeleton" style="width:70%"></div><p class="muted">The ticket opens as soon as the alert is accepted.</p>`
      : `<p class="empty">No ticket yet. Press Start incident in the checkout app, or investigate a sample alert.</p>`);
    return;
  }
  const kind = t.state === "Resolved" ? "ok" : "warn";
  const notes = (t.notes || []).map((note) => {
    const [first, ...rest] = String(note).split("\n");
    const m = first.match(/^(\d{4}-\d\d-\d\dT[\d:.+Z-]+):\s*(.*)$/);
    return `<li>${m ? `<time>${esc(fmtTime(m[1]))}</time>` : ""}${inline(m ? m[2] : first)}${rest.length ? `<pre>${esc(rest.join("\n"))}</pre>` : ""}</li>`;
  }).join("");
  setHtml("ticket", `
    <div class="ticket-head"><strong>${esc(t.number)}</strong><span class="tag ${kind}">${esc(t.state)}</span><span class="tag">${esc(t.priority)}</span></div>
    ${t.short_description ? `<p class="muted" style="margin-top:6px">${esc(t.short_description)}</p>` : ""}
    ${t.servicenow_url ? linkIcon(t.servicenow_url, ICON_SERVICENOW, `Open ${t.number} in ServiceNow`, "snow") : `<p class="muted" style="margin-top:10px">Local ticket. Set the ServiceNow variables in .env to mirror it.</p>`}
    <dl class="kv">
      <div><dt>Service</dt><dd>${esc(t.service)}</dd></div><div><dt>Assigned to</dt><dd>${esc(t.assignee || "-")}</dd></div>
      <div><dt>Group</dt><dd>${esc(t.group || "-")}</dd></div><div><dt>Caller</dt><dd>${esc(t.caller || "-")}</dd></div>
    </dl>
    ${notes ? `<details ${s.running ? "open" : ""}><summary>Activity (${(t.notes || []).length})</summary><ol class="timeline">${notes}</ol></details>` : ""}`);
}

function renderVerdict(s) {
  const v = (s.evidence || {}).verdict;
  $("verdict-card").hidden = !v;
  if (!v) return;
  const cls = v.supported ? "ok" : v.insufficient ? "" : "bad";
  const delta = v.delta_minutes == null ? "unknown" : `${v.delta_minutes} min`;
  setHtml("verdict", `
    <div class="verdict ${cls}"><b>${esc(v.label)}</b><span>${esc(v.reasoning)}</span></div>
    <div class="timing"><span>Incident: ${esc(fmtTime(v.incident_time) || "unknown")}</span><span>Deploy: ${esc(fmtTime(v.deploy_time) || "unknown")}</span><span>Gap: ${esc(delta)}</span></div>`);
}

function renderEvidence(s) {
  const ev = s.evidence;
  if (!ev) { setHtml("evidence", s.running ? `<div class="skeleton"></div><div class="skeleton" style="width:80%"></div>` : `<p class="empty">Evidence from metrics, logs, deployments and runbooks appears here with its source and status.</p>`); return; }
  const cards = ev.groups.map((group) => {
    const statusKind = /LIVE|MATCHED/.test(group.status) ? "ok" : /FIXTURE/.test(group.status) ? "warn" : "";
    const records = (group.records || []).map((r) => `
      <div class="rec"><p>${esc(r.summary)}</p>
      <p class="meta">${esc(r.source || "")}${r.id ? " · " + esc(String(r.id).slice(0, 40)) : ""}${r.event_time ? " · event " + esc(fmtTime(r.event_time)) : ""}</p>
      ${r.window ? `<p class="meta">window ${esc(r.window[0])} to ${esc(r.window[1])}</p>` : ""}${link(r.url, "Open source")}</div>`).join("");
    const count = (group.records || []).length;
    return `<details class="source" ${count > 0 && count <= 1 ? "open" : ""}><summary><h3>${esc(group.name)}</h3><span class="tag ${statusKind}">${esc(group.status || "NO EVIDENCE")} · ${count}</span></summary>
      <div class="source-body"><div class="rec-scroll">${records || `<p class="muted">${esc(group.note || "No verified records were published for this source.")}</p>`}</div></div></details>`;
  });
  if (ev.memory) cards.push(`<details class="source"><summary><h3>Incident memory</h3><span class="tag">LOCAL HISTORY</span></summary><div class="source-body"><div class="rec"><p>Prior incidents: ${esc(ev.memory.prior)}</p>${ev.memory.hotfix ? `<p class="meta">${esc(ev.memory.hotfix)}</p>` : ""}</div></div></details>`);
  setHtml("evidence", `<div class="sources">${cards.join("")}</div><p class="muted" style="margin-top:8px">Connector configuration does not guarantee a successful query; each status reflects recorded evidence.</p>`);
}

function renderDiff(diff) {
  if (!diff) return `<p class="muted">GitHub returned no textual diff.</p>`;
  const lines = diff.split("\n").map((line) => {
    const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff") || line.startsWith("index") ? "hunk"
      : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    return `<div class="${cls}">${esc(line) || " "}</div>`;
  });
  return `<div class="diff" role="region" aria-label="Pull request diff" tabindex="0">${lines.join("")}</div>`;
}

function renderRepair(s) {
  const r = s.repair || {};
  const fix = (s.evidence || {}).fix;
  let html = "";
  if (fix) {
    const checks = fix.checks.map((c) => `<li class="${c.status === "passed" ? "pass" : "fail"}"><span class="mk">${c.status === "passed" ? "✓" : "✕"}</span><span>${esc(c.name)}<small>${esc(c.detail)}</small></span></li>`).join("");
    html += `<div class="change"><strong>Proposed change by the FixAgent</strong><p style="margin-top:4px">${esc(fix.summary || "")}</p><p class="muted">File: <code>${esc(fix.path || "")}</code></p></div>
      <p class="muted" style="margin-top:12px">Sandbox validation ${fix.checks.filter((c) => c.status === "passed").length}/${fix.checks.length} passed. The model cannot skip these checks.</p><ul class="checks">${checks}</ul>`;
  }
  if (r.state === "none" || r.state === "locked" || r.state === "unconfigured" || r.state === "error") {
    html += `<p class="${r.state === "error" ? "banner bad" : "empty"}">${esc(r.message || "")}</p>`;
  } else if (r.state === "ready" && !r.pr) {
    html += `<p class="muted" style="margin:12px 0">Repository ${esc(r.repository)}. ${esc(r.message || "")}</p>
      <div class="actions"><button class="btn primary" data-act="create-pr">Prepare repair PR</button></div>`;
  } else if (r.pr) {
    const p = r.pr;
    const stateTag = p.merged ? `<span class="tag ok">Merged</span>` : `<span class="tag warn">${esc(p.state)}</span>`;
    const checks = (r.checks || []).map((c) => {
      const ok = c.conclusion === "success"; const bad = ["failure", "cancelled", "timed_out"].includes(c.conclusion);
      return `<li class="${ok ? "pass" : bad ? "fail" : ""}"><span class="mk">${ok ? "✓" : bad ? "✕" : "…"}</span><span>${esc(c.name)}<small>${esc(c.conclusion)} ${linkIcon(c.url, ICON_GITHUB, "view run", "github")}</small></span></li>`;
    }).join("");
    html += `<div class="pr-head"><strong>${linkIcon(p.url, ICON_GITHUB, `#${p.number} ${p.title}`, "github")}</strong>${stateTag}</div>
      <p class="muted">${esc(p.head)} → ${esc(p.base)} · commit ${esc(p.sha)}${r.issue ? ` · issue ${linkIcon(r.issue.url, ICON_GITHUB, "#" + r.issue.number, "github")} (${esc(r.issue.state)})` : ""}</p>
      ${r.scope_error ? `<p class="banner bad" style="margin-top:10px">Approval locked: ${esc(r.scope_error)}</p>` : ""}
      ${renderDiff(r.diff)}<ul class="checks">${checks || `<li><span class="mk">…</span><span>GitHub Actions checks are starting<small>they appear here when they report</small></span></li>`}</ul>`;
    if (r.state === "resolved") html += `<div class="done-card">PR merged, recovery verified, incident resolved.</div>`;
    else if (r.state === "merged") html += `<div class="actions"><button class="btn good" data-act="finalize" ${r.scope_error ? "disabled" : ""}>Verify recovery and update ticket</button></div>`;
    else {
      const key = `${(s.ticket || {}).number}:${p.sha}`;
      html += `<label class="approve"><input type="checkbox" id="approve" data-key="${esc(key)}" ${approved[key] ? "checked" : ""}><span>I reviewed this diff and the GitHub Actions results, and I approve merging this change.</span></label>
        <div class="actions"><button class="btn good" data-act="merge" id="merge" ${approved[key] && r.can_merge ? "" : "disabled"}>Approve and merge repair</button></div>`;
    }
  }
  setHtml("repair", html || `<p class="empty">The proposed fix and its review appear here.</p>`);
  document.querySelectorAll("#repair [data-act]").forEach((button) => {
    button.dataset.ticket = (s.ticket || {}).number || "";
    button.dataset.pr = r.pr ? String(r.pr.number) : "";
    button.dataset.sha = r.pr ? r.pr.sha : "";
  });
}

function renderChain(s) {
  const chain = s.dependency_chain || [];
  $("chain-card").hidden = chain.length <= 1;
  if (chain.length <= 1) return;
  setHtml("chain", `<ol class="timeline">${chain.map((hop, idx) => {
    const supported = hop.verdict === "SUPPORTED";
    const cls = supported ? "ok" : hop.verdict === "NOT_SUPPORTED" ? "bad" : "";
    return `<li><span class="tag ${cls}">${idx === 0 ? "Triggering service" : "Hop " + idx}</span> <strong>${esc(hop.service_name)}</strong>
      ${hop.ticket_number ? ` · ${esc(hop.ticket_number)}` : ""} · <span class="tag ${cls}">${esc(hop.verdict || "unknown")}</span>
      ${hop.reasoning ? `<p class="meta">${esc(hop.reasoning)}</p>` : ""}</li>`;
  }).join("")}</ol>`);
}

function renderPerf(s) {
  const p = s.perf;
  const el = $("perf-strip");
  if (!el || !p) return;
  const cache = p.confluence_cache || {};
  const search = cache.search || {};
  const parts = [];
  if (p.duration_ms) parts.push(`last run ${Math.round(p.duration_ms)} ms`);
  if (p.memory_peak_kb) parts.push(`peak ${Math.round(p.memory_peak_kb)} KB`);
  if (search.hits || search.misses) parts.push(`runbook cache ${search.hits || 0} hit / ${search.misses || 0} miss / ${search.evictions || 0} evicted`);
  el.textContent = parts.join(" · ");
}

function renderJobs(s) {
  if (!s.jobs.length) { setHtml("jobs", `<p class="empty">Waiting for an Alertmanager alert. Press Start incident in the checkout app.</p>`); return; }
  setHtml("jobs", s.jobs.map((j) => {
    const kind = j.state === "completed" ? "ok" : j.state === "failed" ? "bad" : "warn";
    return `<div class="job"><div class="job-top"><span>${esc(j.alert_name || "Alert")}</span><span class="tag ${kind}">${esc(String(j.state || "queued").toUpperCase())}</span></div>
      <span class="muted">${esc(j.service || "unknown service")} · ${esc(j.severity || "")} · ${esc(fmtTime(j.received_at))}</span>
      ${j.ticket_number ? `<span class="muted">Ticket ${esc(j.ticket_number)}</span>` : ""}${j.error ? `<span class="muted" style="color:var(--bad)">${esc(j.error)}</span>` : ""}</div>`;
  }).join(""));
}

function renderMessages(s) {
  const wasBottom = $("messages").scrollHeight - $("messages").scrollTop - $("messages").clientHeight < 80;
  const html = s.messages.length
    ? s.messages.map((m) => `<div class="msg ${m.role === "user" ? "user" : "assistant"}"><span class="who">${m.role === "user" ? "You" : "Sentinel · agent-generated"}</span>${markdown(m.content)}</div>`).join("")
      + (s.running ? `<div class="msg assistant"><span class="who">Sentinel</span><span class="spinner"></span> Commander is coordinating the agents. This takes about a minute.</div>` : "")
    : `<p class="empty">The conversation with the agents appears here.</p>`;
  const before = lastRendered.messages;
  setHtml("messages", html);
  if (before !== html && wasBottom) $("messages").scrollTop = $("messages").scrollHeight;
}

function render(s, force = false) {
  if (force) lastRendered = {};
  renderSummary(s);
  renderPills(s); renderStepper(s); renderBanner(s); renderTicket(s); renderVerdict(s); renderChain(s);
  renderEvidence(s); renderRepair(s); renderJobs(s); renderMessages(s); renderPerf(s);
  $("go").disabled = busy || s.running;
  $("send").disabled = busy || s.running;
}

function renderSummary(s) {
  const ticket = s.ticket || {};
  const source = s.source || {};
  const groups = (s.evidence || {}).groups || [];
  const sourceCount = groups.filter((g) => g.records && g.records.length).length;
  const recordCount = groups.reduce((count, group) => count + ((group.records || []).length), 0);
  const verdict = (s.evidence || {}).verdict;
  $("stat-incident").textContent = ticket.number || source.title || (s.running ? "Investigation running" : "No incident");
  $("stat-sources").textContent = recordCount ? `${recordCount} records · ${sourceCount} sources` : (s.running ? "Collecting evidence" : "Awaiting evidence");
  $("stat-verdict").textContent = verdict ? verdict.label : (s.running ? "Evaluating" : "Not evaluated");
  $("ticket-count").textContent = ticket.number ? "OPEN INCIDENT" : "LOCAL TICKET";
}

/* ---------------------------------------------------------------- events */
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-act]");
  if (!button) return;
  const action = button.dataset.act;
  const reviewed = { ticket_number: button.dataset.ticket, pr_number: Number(button.dataset.pr), sha: button.dataset.sha };
  if (action === "create-pr") act(button, "/api/pr/create", reviewed);
  else if (action === "finalize") act(button, "/api/pr/finalize", reviewed);
  else if (action === "merge") {
    const box = $("approve");
    if (box && box.checked) act(button, "/api/pr/merge", { ...reviewed, approved: true });
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id !== "approve") return;
  approved[event.target.dataset.key] = event.target.checked;
  const merge = $("merge");
  if (merge) merge.disabled = !(event.target.checked && state && state.repair && state.repair.can_merge);
});
$("start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act($("go"), "/api/investigate", { preset: $("preset").value });
});
$("new").addEventListener("click", () => { approved = {}; act($("new"), "/api/new", {}); });
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  act($("send"), "/api/investigate", { text, fresh: !(state && state.messages.length) });
});
$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) $("chat-form").requestSubmit();
});

/* ------------------------------------------------------------------ poll */
async function refresh() {
  try {
    state = await api("/api/state");
    $("banner").hidden = true;
    render(state);
  } catch (error) {
    if (!state) {
      const banner = $("banner");
      banner.className = "banner bad";
      banner.innerHTML = `Sentinel backend is unavailable. Open this app through <a href="http://localhost:8501/">http://localhost:8501/</a>.`;
      banner.hidden = false;
      setHtml("ticket", `<p class="empty">Waiting for the Sentinel backend.</p>`);
    }
  }
}
function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => { if (!document.hidden) await refresh(); schedule(); }, state && state.running ? 1500 : 3000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

(async function init() {
  try {
    const { presets } = await api("/api/presets");
    $("preset").innerHTML = presets.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
  } catch (_) { /* presets are optional */ }
  await refresh();
  schedule();
})();
