"use strict";

const view = { selectedAttempt: "", selectedReviewHash: "", loading: false };
const html = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);

function notice(message = "") {
  const node = document.querySelector("#global-notice");
  node.hidden = !message;
  node.textContent = message;
}

async function rawJson(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const payload = await response.json().catch(() => ({}));
  return { response, payload };
}

let sessionBootstrap = null;
async function ensureSession() {
  if (sessionBootstrap) return sessionBootstrap;
  sessionBootstrap = (async () => {
    const current = await rawJson("/api/auth/session");
    if (current.response.ok) return;
    if (current.response.status !== 401) throw new Error("Dashboard session is unavailable.");
    const issued = await rawJson("/api/auth/local-session", { method: "POST" });
    if (!issued.response.ok) {
      if (issued.response.status === 403) throw new Error("This Dashboard origin is not trusted.");
      throw new Error("Local Dashboard session could not be established.");
    }
  })();
  try { await sessionBootstrap; } finally { sessionBootstrap = null; }
}

async function api(url, options = {}, retry = true) {
  const result = await rawJson(url, options);
  if (result.response.status === 401 && retry) {
    await ensureSession();
    return api(url, options, false);
  }
  if (!result.response.ok) {
    throw new Error(String(result.payload.detail || `Request failed (${result.response.status}).`));
  }
  return result.payload;
}

function statusClass(status) {
  if (["CONFIRMED"].includes(status)) return "success";
  if (["HUMAN_INTERVENTION_REQUIRED", "SUBMISSION_OUTCOME_UNKNOWN"].includes(status)) return "warning";
  if (["FAILED"].includes(status)) return "danger";
  return "neutral";
}

function renderAttempts(payload) {
  const executor = document.querySelector("#executor-status");
  executor.textContent = payload.executor_status === "AVAILABLE" ? "Local executor online" : "Executor unavailable";
  executor.className = `status-pill ${payload.executor_status === "AVAILABLE" ? "success" : "warning"}`;
  const items = payload.applications || [];
  const node = document.querySelector("#attempt-list");
  node.innerHTML = items.length ? items.map((item) => `
    <article class="application-card">
      <div class="item-row">
        <div>
          <h3>${html(item.title)} · ${html(item.company)}</h3>
          <p class="item-meta">${html(item.provider)} · external job ${html(item.external_job_id)}</p>
        </div>
        <span class="status-pill ${statusClass(item.status)}">${html(item.status)}</span>
      </div>
      <p><a href="${html(item.canonical_job_url)}" target="_blank" rel="noreferrer">Open canonical job posting</a></p>
      <button class="button secondary" data-attempt="${html(item.attempt_id)}">Review attempt</button>
    </article>`).join("") : `<div class="empty-state"><h3>No prepared attempts</h3><p>The local preparation command must create a formal attempt before any browser work can begin.</p></div>`;
  node.querySelectorAll("[data-attempt]").forEach((button) => button.addEventListener("click", () => loadAttempt(button.dataset.attempt)));
}

function definition(label, value) {
  return `<dl class="definition-item"><dt>${html(label)}</dt><dd>${html(value || "None")}</dd></dl>`;
}

function renderAnswers(bundle = {}, review = {}) {
  const sections = Array.isArray(bundle.sections) ? bundle.sections : [];
  const reviewFields = Array.isArray(review.review_fields) ? review.review_fields : [];
  const unresolved = Array.isArray(bundle.unresolved) ? bundle.unresolved : [];
  const node = document.querySelector("#answer-summary");
  const prepared = sections.length ? sections.map((section) => `
    <article class="attention-item">
      <h3>${html(section.label || section.key)}</h3>
      <div class="definition-grid">${(section.items || []).map((item) => definition(
        item.label || item.key,
        item.status === "MISSING" ? "Missing — execution must stop" : `${item.value} · source: ${item.source} · certainty: ${item.certainty}`,
      )).join("")}</div>
    </article>`).join("") : `<div class="empty-state compact"><h3>No answer projection</h3><p>This attempt is not approvable until the answer bundle is visible.</p></div>`;
  const exact = reviewFields.length ? `<article class="attention-item"><h3>Exact Workday Review readback</h3><div class="definition-grid">${reviewFields.map((item) => definition(item.label, `${item.value} · source: ${item.source} · certainty: ${item.certainty}`)).join("")}</div></article>` : "";
  const missing = unresolved.length ? `<article class="attention-item"><h3>Unresolved formal answers</h3><ul>${unresolved.map((item) => `<li>${html(item.key)} · ${html(item.reason)} · ${html(item.required_human_action)}${item.blocking ? " · blocking" : ""}</li>`).join("")}</ul></article>` : "";
  node.innerHTML = prepared + exact + missing;
}

function renderAttempt(item) {
  view.selectedAttempt = item.attempt_id;
  view.selectedReviewHash = item.review_hash || "";
  document.querySelector("#attempt-review").hidden = false;
  document.querySelector("#review-title").textContent = `${item.title} · ${item.company}`;
  const status = document.querySelector("#attempt-status");
  status.textContent = item.status;
  status.className = `status-pill ${statusClass(item.status)}`;
  document.querySelector("#job-summary").innerHTML = [
    definition("Company", item.company), definition("Position", item.title),
    definition("ATS", item.provider), definition("External job ID", item.external_job_id),
    definition("Application attempt", item.attempt_id), definition("Application plan", item.application_plan_id),
    definition("Canonical URL", item.canonical_job_url), definition("Bundle hash", item.bundle_canonical_hash),
    definition("Assembly record", item.assembly_record_id),
    definition("Confirmation ID", item.confirmation_id),
    definition("Confirmed URL", item.success_url),
  ].join("");
  document.querySelector("#file-summary").innerHTML = [
    definition("Resume SHA-256", item.resume_sha256),
    definition("Cover letter SHA-256", item.cover_letter_sha256),
  ].join("");
  const review = item.review || {};
  renderAnswers(item.answer_bundle || {}, review);
  const declarations = Array.isArray(review.legal_declarations) ? review.legal_declarations : [];
  document.querySelector("#legal-summary").innerHTML = `
    <p><strong>External side effect:</strong> one final Submit click sends the displayed answers and local candidate files to ${html(item.company)} through Workday.</p>
    ${declarations.length ? `<ul>${declarations.map((value) => `<li>${html(value)}</li>`).join("")}</ul>` : "<p>No legal declaration text has been captured from the ATS Review yet.</p>"}`;
  document.querySelector("#attempt-timeline").innerHTML = (item.timeline || []).map((event) => `<li>${html(event.created_at)} · ${html(event.type)}</li>`).join("");
  const intervention = review.human_intervention || {};
  document.querySelector("#human-reason").textContent = intervention.reason ? `${intervention.reason} (${intervention.checkpoint || "Workday"})` : "Complete the visible browser step before continuing.";
  document.querySelector("#human-actions").hidden = item.status !== "HUMAN_INTERVENTION_REQUIRED";
  document.querySelector("#approval-actions").hidden = item.status !== "REVIEW_READY";
  document.querySelector("#approval-confirmation").checked = false;
  document.querySelector("#approve-application").disabled = true;
  document.querySelector("#attempt-review").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadAttempt(attemptId) {
  notice();
  try { renderAttempt(await api(`/api/real-applications/${encodeURIComponent(attemptId)}`)); }
  catch (error) { notice(error.message); }
}

async function loadAttempts() {
  if (view.loading) return;
  view.loading = true;
  notice();
  try {
    await ensureSession();
    const payload = await api("/api/real-applications");
    renderAttempts(payload);
    const header = document.querySelector("#header-status");
    header.className = "header-status is-ready";
    header.lastElementChild.textContent = "Control plane ready";
    if (view.selectedAttempt) await loadAttempt(view.selectedAttempt);
  } catch (error) {
    notice(error.message);
    const header = document.querySelector("#header-status");
    header.className = "header-status is-failed";
    header.lastElementChild.textContent = "Unavailable";
  } finally { view.loading = false; }
}

document.querySelector("#refresh-attempts").addEventListener("click", loadAttempts);
document.querySelector("#approval-confirmation").addEventListener("change", (event) => {
  document.querySelector("#approve-application").disabled = !event.target.checked;
});
document.querySelector("#approve-application").addEventListener("click", async () => {
  const button = document.querySelector("#approve-application");
  button.disabled = true;
  try {
    await api(`/api/real-applications/${encodeURIComponent(view.selectedAttempt)}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        review_hash: view.selectedReviewHash,
        external_side_effect_acknowledged: true,
      }),
    });
    await loadAttempt(view.selectedAttempt);
  } catch (error) { notice(error.message); button.disabled = false; }
});
document.querySelector("#continue-application").addEventListener("click", async () => {
  try {
    await api(`/api/real-applications/${encodeURIComponent(view.selectedAttempt)}/continue`, { method: "POST" });
    await loadAttempt(view.selectedAttempt);
  } catch (error) { notice(error.message); }
});

loadAttempts();
setInterval(loadAttempts, 5000);
