"use strict";

const state = {
  page: "home",
  applicationTab: "NEEDS_ATTENTION",
  overview: null,
  profile: null,
  jobs: null,
  applications: null,
  attention: null,
  loading: true,
  refreshing: false,
  automating: false,
  activeAttentionItem: null,
};

const labels = {
  profile: {
    EMPTY: "Not started",
    INCOMPLETE: "Needs information",
    READY: "Ready",
    CONFLICT: "Needs review",
    SYSTEM_ISSUE: "System issue",
  },
  job: {
    NOT_EVALUATED: "Not evaluated",
    EVALUATING: "Evaluating",
    HIGH_MATCH: "High match",
    READY_TO_PREPARE: "Ready to prepare",
    NEEDS_INPUT: "Needs input",
    NOT_A_MATCH: "Not a match",
    APPLICATION_CREATED: "Application created",
    SYSTEM_ISSUE: "System issue",
  },
  application: {
    SELECTED: "Selected",
    PREPARING: "Preparing",
    NEEDS_ATTENTION: "Needs your attention",
    READY: "Ready",
    SUBMITTED: "Submitted",
    SUBMISSION_UNCERTAIN: "Submission uncertain",
    SYSTEM_ISSUE: "System issue",
  },
  field: {
    first_name: "First name", last_name: "Last name", preferred_name: "Preferred name",
    email: "Email", phone: "Phone", location: "Location", address: "Address",
    city: "City", state: "State or province", postal_code: "Postal code",
    country: "Country", linkedin: "LinkedIn", github: "GitHub", portfolio: "Portfolio",
  },
};

const nextStepCopy = {
  SYSTEM_ATTENTION: ["JobOps needs attention", "A system or data integrity issue must be resolved before continuing.", "View system issues", "applications"],
  COMPLETE_PROFILE: ["Complete your profile", "Add or verify the required information used in applications.", "Review profile", "profile"],
  SET_JOB_PREFERENCES: ["Set your job preferences", "Choose the roles and locations you want JobOps to search.", "Set preferences", "profile"],
  REVIEW_ATTENTION: ["Review items needing your attention", "Your input is required before those applications can continue.", "Review items", "applications"],
  REFRESH_JOB_LIBRARY: ["Find matching jobs", "Refresh your job library using your enabled search preferences.", "Refresh job library", "refresh"],
  CONTINUE_AUTOMATION: ["Continue automatic applications", "Prepare and safely advance the applications that are ready.", "Continue applications", "automation"],
  VIEW_APPLICATIONS: ["Review your applications", "Your current application work is up to date.", "View applications", "applications"],
  ALL_CAUGHT_UP: ["You’re all caught up", "There is no action required right now.", "View jobs", "jobs"],
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

function invocation(prefix) {
  return `${prefix}-${Date.now()}-${crypto.getRandomValues(new Uint32Array(1))[0].toString(16)}`;
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function loadDashboard() {
  state.loading = true;
  setHeader("Loading", "");
  const requests = {
    overview: getJson("/api/dashboard/overview"),
    profile: getJson("/api/dashboard/profile"),
    jobs: getJson("/api/dashboard/jobs"),
    applications: getJson("/api/dashboard/applications"),
    attention: getJson("/api/human-attention-inbox"),
  };
  const keys = Object.keys(requests);
  const results = await Promise.allSettled(Object.values(requests));
  let failures = 0;
  results.forEach((result, index) => {
    if (result.status === "fulfilled") state[keys[index]] = result.value;
    else failures += 1;
  });
  state.loading = false;
  if (failures) {
    showNotice(`${failures} Dashboard section${failures === 1 ? "" : "s"} could not be loaded. System failures are not shown as empty data.`);
    setHeader("Needs attention", "is-failed");
  } else {
    hideNotice();
    setHeader("Up to date", "is-ready");
  }
  renderAll();
}

function setHeader(text, className) {
  const node = document.querySelector("#header-status");
  node.className = `header-status ${className}`.trim();
  node.querySelector("span:last-child").textContent = text;
}
function showNotice(text) { const node = document.querySelector("#global-notice"); node.textContent = text; node.hidden = false; }
function hideNotice() { document.querySelector("#global-notice").hidden = true; }

function navigate(page) {
  state.page = page;
  document.querySelectorAll("[data-page]").forEach((node) => {
    const active = node.dataset.page === page;
    node.hidden = !active;
    node.classList.toggle("is-active", active);
  });
  document.querySelectorAll(".nav-link").forEach((node) => {
    node.classList.toggle("is-active", node.dataset.nav === page);
  });
  history.replaceState(null, "", `#${page}`);
  document.querySelector("#main-content").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function emptyState(title, detail, action = "") {
  return `<div class="empty-state"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(detail)}</p>${action}</div>`;
}
function failureState(title = "This section could not be loaded") {
  return `<div class="empty-state"><h3>${escapeHtml(title)}</h3><p>This is a system issue, not an empty result. Try again or review system status.</p><button class="button secondary" data-reload>Try again</button></div>`;
}
function pill(status, text) {
  const tone = ["SUBMITTED", "READY", "READY_TO_PREPARE"].includes(status) ? "success"
    : ["NEEDS_ATTENTION", "NEEDS_INPUT", "SUBMISSION_UNCERTAIN"].includes(status) ? "warning"
    : status === "SYSTEM_ISSUE" ? "danger" : "neutral";
  return `<span class="status-pill ${tone}">${escapeHtml(text)}</span>`;
}

function renderAll() {
  renderNextStep();
  renderOnboarding();
  renderPipeline();
  renderAttention();
  renderMatches();
  renderRecentApplications();
  renderJobs();
  renderApplications();
  renderProfile();
  bindDynamicActions();
}

function renderNextStep() {
  const next = state.overview?.next_step || (state.loading ? null : "SYSTEM_ATTENTION");
  const copy = next ? nextStepCopy[next] : ["Loading your next step…", "Checking current records.", "Loading", ""];
  document.querySelector("#next-step-title").textContent = copy[0];
  document.querySelector("#next-step-description").textContent = copy[1];
  const button = document.querySelector("#next-step-action");
  button.textContent = copy[2];
  button.disabled = !next || state.refreshing || state.automating;
  button.dataset.action = copy[3];
}

function renderOnboarding() {
  const profile = state.profile;
  const jobs = state.jobs;
  const node = document.querySelector("#onboarding");
  if (!profile || !jobs) { node.hidden = true; return; }
  const profileComplete = profile.profile_state === "READY";
  const preferencesComplete = (profile.search_preference_summary?.enabled_profile_count || 0) > 0;
  const jobsComplete = (jobs.counts?.total || 0) > 0;
  node.hidden = profileComplete && preferencesComplete && jobsComplete;
  if (node.hidden) return;
  const steps = [
    ["Add your information", "Give JobOps the verified facts required to prepare applications.", profileComplete, "Profile", "profile"],
    ["Set roles and locations", "Choose the kinds of jobs you want to find.", preferencesComplete, "Job preferences", "profile"],
    ["Refresh your job library", "Find opportunities matching your enabled preferences.", jobsComplete, "Refresh jobs", "refresh"],
  ];
  document.querySelector("#onboarding-progress").textContent = `${steps.filter((step) => step[2]).length} of 3 complete`;
  document.querySelector("#onboarding-steps").innerHTML = steps.map((step) => `
    <li class="onboarding-step ${step[2] ? "is-complete" : ""}">
      <h3>${escapeHtml(step[0])}</h3><p>${escapeHtml(step[1])}</p>
      <button class="button ${step[2] ? "secondary" : "primary"}" data-action="${step[4]}" ${step[2] ? 'aria-label="Review completed step"' : ""}>${escapeHtml(step[2] ? "Review" : step[3])}</button>
    </li>`).join("");
}

function renderPipeline() {
  const profile = state.profile;
  const jobs = state.jobs;
  const applications = state.applications;
  if (!profile || !jobs || !applications) {
    document.querySelector("#pipeline-grid").innerHTML = failureState();
    return;
  }
  const counts = applications.counts || {};
  const stages = [
    ["01", "Profile", labels.profile[profile.profile_state] || "Unknown", `${profile.verified_required_field_count || 0} of ${profile.required_field_count || 0} required fields`, "profile"],
    ["02", "Job Library", jobs.library_state === "EMPTY" ? "No jobs yet" : "Ready", `${jobs.counts?.total ?? "—"} jobs`, "jobs"],
    ["03", "Applications", (counts.needs_attention || 0) ? "Needs attention" : "In progress", `${counts.total || 0} applications`, "applications"],
    ["04", "Submitted", (counts.submitted || 0) ? "Submissions confirmed" : "None submitted yet", `${counts.submitted || 0} submitted`, "applications"],
  ];
  document.querySelector("#pipeline-grid").innerHTML = stages.map((item) => `
    <button class="pipeline-card" data-nav="${item[4]}">
      <span class="pipeline-index">${item[0]}</span><strong>${escapeHtml(item[1])}</strong>
      <span>${escapeHtml(item[2])}</span><div class="item-meta">${escapeHtml(item[3])}</div>
    </button>`).join("");
}

function renderAttention() {
  const node = document.querySelector("#attention-list");
  if (!state.attention) { node.innerHTML = state.loading ? emptyState("Loading attention items", "Checking your current queue.") : failureState(); return; }
  if (state.attention.status === "FAILED") { node.innerHTML = failureState("Attention items could not be loaded"); return; }
  const items = state.attention.user_items || [];
  if (!items.length) { node.innerHTML = emptyState("Nothing needs your attention", "JobOps can continue without additional input from you."); return; }
  node.innerHTML = items.slice(0, 5).map((item) => `
    <article class="attention-item">
      <div class="item-row"><div><h3>${escapeHtml(item.attention_label)}</h3><div class="item-meta">Application ${escapeHtml(item.application_plan_id.slice(0, 12))}…</div></div>${pill("NEEDS_ATTENTION", "Action needed")}</div>
      <p>${escapeHtml(item.required_action)}</p>
      <button class="button secondary" data-attention-id="${escapeHtml(item.item_id)}">Review item</button>
      <details class="technical-details"><summary>Technical details</summary><div class="technical-panel">Kind: ${escapeHtml(item.attention_kind)}<br>Stage: ${escapeHtml(item.source_stage)}</div></details>
    </article>`).join("");
}

function matchMarkup(item) {
  const score = item.match_score == null ? item.priority_bucket || "Match" : `${Math.round(item.match_score)}%`;
  return `<article class="match-item">
    <div class="item-row"><div><h3>${escapeHtml(item.title)}</h3><div class="item-meta">${escapeHtml(item.company)} · ${escapeHtml(item.location || "Location not listed")}</div></div>${pill(item.application_status, score)}</div>
    ${(item.match_reasons || []).length ? `<ul class="reasons">${item.match_reasons.slice(0, 3).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : '<p class="item-meta">No verified match explanation is available.</p>'}
    <div class="item-row"><span>${pill(item.application_status, labels.job[item.application_status] || "Unknown")}</span><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">View job</a></div>
  </article>`;
}

function renderMatches() {
  const node = document.querySelector("#top-matches");
  if (!state.overview) { node.innerHTML = state.loading ? emptyState("Loading matches", "Reviewing the current job snapshot.") : failureState(); return; }
  const items = state.overview.top_matches || [];
  node.innerHTML = items.length ? items.slice(0, 5).map(matchMarkup).join("") : emptyState("No top matches yet", "Refresh your job library to find opportunities matching your profile.", '<button class="button primary" data-action="refresh">Refresh job library</button>');
}

function applicationMarkup(item) {
  const progress = (item.progress_steps || []).map((step) => `<div class="progress-step ${escapeHtml(step.state)}"><strong>${escapeHtml(step.stage[0] + step.stage.slice(1).toLowerCase())}</strong><br>${escapeHtml(step.state.replaceAll("_", " ").toLowerCase())}</div>`).join("");
  const attentionItem = (state.attention?.user_items || []).find((value) => value.application_plan_id === item.application_plan_id);
  return `<article class="application-card">
    <div class="item-row"><div><h3>${escapeHtml(item.title)}</h3><div class="item-meta">${escapeHtml(item.company)} · ${escapeHtml(item.location || "Location not listed")}</div></div>${pill(item.product_status, labels.application[item.product_status] || "Unknown")}</div>
    <p>${escapeHtml(item.safe_status_detail)}</p>
    <div class="progress" aria-label="Application progress">${progress}</div>
    ${attentionItem ? `<button class="button primary" data-attention-id="${escapeHtml(attentionItem.item_id)}">Review required action</button>` : ""}
    <details class="technical-details"><summary>Technical details</summary><div class="technical-panel">Plan: ${escapeHtml(item.application_plan_id)}<br>Job: ${escapeHtml(item.job_id)}</div></details>
  </article>`;
}

function renderRecentApplications() {
  const node = document.querySelector("#recent-applications");
  if (!state.overview) { node.innerHTML = state.loading ? emptyState("Loading applications", "Checking current progress.") : failureState(); return; }
  const items = state.overview.recent_applications || [];
  node.innerHTML = items.length ? items.slice(0, 5).map(applicationMarkup).join("") : emptyState("No applications yet", "High-fit jobs will appear here after JobOps creates an application plan.", '<button class="button secondary" data-nav="jobs">View jobs</button>');
}

function renderJobs() {
  const node = document.querySelector("#jobs-list");
  const countsNode = document.querySelector("#job-counts");
  if (!state.jobs) { node.innerHTML = state.loading ? emptyState("Loading jobs", "Reading your subject-scoped library.") : failureState(); countsNode.innerHTML = ""; return; }
  if (["FAILED", "INTEGRITY_FAILURE"].includes(state.jobs.read_status)) { node.innerHTML = failureState("Your job library could not be read safely"); countsNode.innerHTML = ""; return; }
  const counts = state.jobs.counts || {};
  countsNode.innerHTML = [
    ["All jobs", counts.total], ["High match", counts.high_match],
    ["Ready", counts.ready_to_prepare], ["Needs input", counts.needs_input],
  ].map(([label, value]) => `<div class="summary-item"><strong>${value ?? "—"}</strong><span>${label}</span></div>`).join("");
  document.querySelector("#jobs-refresh-detail").textContent = state.jobs.last_refreshed_at ? `Last refreshed ${new Date(state.jobs.last_refreshed_at).toLocaleString()}` : "Refresh history is not available from the current formal read contract.";
  const query = document.querySelector("#job-search").value.trim().toLowerCase();
  const status = document.querySelector("#job-status-filter").value;
  const items = (state.jobs.ordered_items || []).filter((item) => {
    const haystack = `${item.title} ${item.company} ${item.location}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!status || item.application_status === status);
  });
  if (!items.length) {
    node.innerHTML = (state.jobs.ordered_items || []).length
      ? emptyState("No jobs match these filters", "Clear or adjust the current filters.")
      : emptyState("No jobs yet", "Refresh your job library to find opportunities matching your profile.", '<button class="button primary" data-action="refresh">Refresh job library</button>');
    return;
  }
  node.innerHTML = `<div class="table-head"><span>Match</span><span>Role</span><span>Company</span><span>Location</span><span>Why it fits</span><span>Status</span><span>Next action</span></div>` + items.map((item) => `
    <div class="job-row">
      <div data-label="Match">${escapeHtml(item.match_score == null ? item.priority_bucket || "—" : `${Math.round(item.match_score)}%`)}</div>
      <div data-label="Role"><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a></div>
      <div data-label="Company">${escapeHtml(item.company)}</div>
      <div data-label="Location">${escapeHtml(item.location || "Not listed")}</div>
      <div data-label="Why it fits">${escapeHtml((item.match_reasons || [])[0] || "No verified explanation")}</div>
      <div data-label="Status">${pill(item.application_status, labels.job[item.application_status] || "Unknown")}</div>
      <div data-label="Next action"><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">View job</a></div>
    </div>`).join("");
}

function renderApplications() {
  const node = document.querySelector("#applications-list");
  document.querySelectorAll("[data-application-tab]").forEach((button) => {
    if (button.getAttribute("role") === "tab") button.setAttribute("aria-selected", String(button.dataset.applicationTab === state.applicationTab));
  });
  if (!state.applications) { node.innerHTML = state.loading ? emptyState("Loading applications", "Reading current application records.") : failureState(); return; }
  if (["FAILED", "INTEGRITY_FAILURE"].includes(state.applications.read_status)) { node.innerHTML = failureState("Applications could not be read safely"); return; }
  const items = (state.applications.ordered_items || []).filter((item) => {
    if (state.applicationTab === "PREPARING") return ["SELECTED", "PREPARING"].includes(item.product_status);
    return item.product_status === state.applicationTab;
  });
  const tabLabel = labels.application[state.applicationTab] || state.applicationTab;
  node.innerHTML = items.length ? items.map(applicationMarkup).join("") : emptyState(`No ${tabLabel.toLowerCase()} applications`, "Applications will move here when their formal status changes.");
}

function renderProfile() {
  if (!state.profile) {
    document.querySelector("#profile-overview-content").innerHTML = state.loading ? "<p>Loading profile…</p>" : failureState();
    return;
  }
  if (["FAILED", "INTEGRITY_FAILURE"].includes(state.profile.read_status)) {
    document.querySelector("#profile-overview-content").innerHTML = failureState("Your verified profile could not be read safely");
    return;
  }
  const profileLabel = labels.profile[state.profile.profile_state] || "Unknown";
  document.querySelector("#profile-state").textContent = profileLabel;
  document.querySelector("#profile-overview-content").innerHTML = `<p><strong>${state.profile.verified_required_field_count} of ${state.profile.required_field_count}</strong> required fields are verified.</p>${state.profile.missing_required_fields?.length ? `<p>Still needed: ${state.profile.missing_required_fields.map((key) => labels.field[key] || key).join(", ")}.</p>` : "<p>Your required identity information is ready.</p>"}`;
  const sources = state.profile.source_summary || {};
  document.querySelector("#profile-source-summary").innerHTML = `<div class="summary-strip"><div class="summary-item"><strong>${sources.total_sources ?? 0}</strong><span>Total sources</span></div><div class="summary-item"><strong>${sources.file_source_count ?? 0}</strong><span>Files</span></div><div class="summary-item"><strong>${sources.url_source_count ?? 0}</strong><span>URLs</span></div><div class="summary-item"><strong>${sources.user_statement_count ?? 0}</strong><span>Statements</span></div></div>`;
  document.querySelector("#identity-fields").innerHTML = (state.profile.identity_fields || []).map((field) => `<dl class="definition-item"><dt>${escapeHtml(labels.field[field.field_key] || field.field_key)}</dt><dd>${escapeHtml(field.display_value || "Not provided")}</dd><dd class="item-meta">${escapeHtml(field.value_state === "PRESENT" ? "Verified current value" : field.value_state.toLowerCase())}</dd></dl>`).join("");
  const preferences = state.profile.search_preference_summary || {};
  document.querySelector("#preference-summary").innerHTML = preferences.enabled_profile_count
    ? `<p><strong>${preferences.enabled_profile_count}</strong> enabled search profile${preferences.enabled_profile_count === 1 ? "" : "s"}.</p><p>Roles: ${escapeHtml((preferences.target_roles || []).join(", ") || "Not specified")}<br>Locations: ${escapeHtml((preferences.target_locations || []).join(", ") || "Not specified")}</p>`
    : emptyState("No job preferences yet", "Enable a formal Search Profile to choose roles and locations.");
  document.querySelector("#review-summary").innerHTML = state.profile.capabilities?.review_capability === "UNAVAILABLE"
    ? emptyState("Review queue unavailable", "The production Candidate Fact review capability is not connected yet. No synthetic count is shown.")
    : `<p>${state.profile.review_summary?.pending_proposals ?? 0} proposals waiting for review.</p>`;
}

async function refreshJobs() {
  if (state.refreshing) return;
  state.refreshing = true;
  updateRunningButtons();
  try {
    const result = await postJson("/api/job-library/refresh", {
      invocation_id: invocation("dashboard-refresh"),
      max_reprioritizations: 1,
    });
    if (result.status === "FAILED") throw new Error(result.message || "Refresh failed");
    await loadDashboard();
  } catch (error) {
    showNotice(`Job library refresh failed: ${error.message}`);
  } finally {
    state.refreshing = false;
    updateRunningButtons();
  }
}

async function runAutomation() {
  if (state.automating) return;
  state.automating = true;
  updateRunningButtons();
  try {
    const result = await postJson("/api/automation-cycle/run", { invocation_id: invocation("dashboard-automation") });
    if (result.status === "FAILED") throw new Error(result.message || "Automation failed");
    await loadDashboard();
  } catch (error) {
    showNotice(`Automatic applications could not continue: ${error.message}`);
  } finally {
    state.automating = false;
    updateRunningButtons();
  }
}

function updateRunningButtons() {
  const refresh = document.querySelector("#refresh-jobs");
  refresh.disabled = state.refreshing;
  refresh.textContent = state.refreshing ? "Refreshing…" : "Refresh job library";
  const automation = document.querySelector("#run-automation");
  automation.disabled = state.automating;
  automation.textContent = state.automating ? "Working…" : "Continue automatic applications";
  renderNextStep();
}

function performAction(action) {
  if (action === "refresh") return refreshJobs();
  if (action === "automation") return runAutomation();
  if (action) navigate(action);
}

function bindDynamicActions() {
  document.querySelectorAll("[data-reload]").forEach((node) => node.onclick = loadDashboard);
  document.querySelectorAll("[data-action]").forEach((node) => node.onclick = () => performAction(node.dataset.action));
  document.querySelectorAll("[data-attention-id]").forEach((node) => node.onclick = () => openAttentionItem(node.dataset.attentionId));
}

function openAttentionItem(itemId) {
  const item = [...(state.attention?.user_items || []), ...(state.attention?.operator_items || [])].find((value) => value.item_id === itemId);
  if (!item) {
    showNotice("The selected attention item is no longer current.");
    return;
  }
  state.activeAttentionItem = item;
  document.querySelector("#attention-dialog-action").textContent = item.required_action;
  document.querySelector("#attention-response").value = "";
  document.querySelector("#attention-dialog-status").textContent = "";
  const generic = ["PROVIDE_FACT", "MAKE_CHOICE", "ATTEST"].includes(item.resolution_capability);
  document.querySelector("#attention-response-area").hidden = !generic;
  const specialized = document.querySelector("#attention-specialized-message");
  specialized.hidden = generic;
  specialized.textContent = generic ? "" : "This item requires a specialized correction or replacement capability. The guided Dashboard will not fake or bypass that workflow.";
  document.querySelector("#attention-dialog").showModal();
}

async function submitAttentionResponse() {
  const item = state.activeAttentionItem;
  const message = document.querySelector("#attention-response").value.trim();
  const status = document.querySelector("#attention-dialog-status");
  if (!item || !message) {
    status.textContent = "Please provide a clear response.";
    return;
  }
  const button = document.querySelector("#submit-attention-response");
  button.disabled = true;
  status.textContent = "Submitting…";
  try {
    const endpoint = item.resolution_capability === "MAKE_CHOICE"
      ? "resolve-version-choice"
      : "resolve";
    const result = await postJson(`/api/human-attention-inbox/${encodeURIComponent(item.item_id)}/${endpoint}`, { message });
    if (["FAILED", "INTEGRITY_FAILURE"].includes(result.status)) throw new Error(result.message || "Resolution failed");
    document.querySelector("#attention-dialog").close();
    await loadDashboard();
  } catch (error) {
    status.textContent = `Could not submit the response: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function bindStaticActions() {
  document.querySelectorAll("[data-nav]").forEach((node) => node.addEventListener("click", (event) => {
    event.preventDefault();
    if (node.dataset.applicationTab) state.applicationTab = node.dataset.applicationTab;
    navigate(node.dataset.nav);
    renderApplications();
  }));
  document.querySelectorAll("[data-application-tab]").forEach((node) => node.addEventListener("click", () => {
    state.applicationTab = node.dataset.applicationTab;
    navigate("applications");
    renderApplications();
  }));
  document.querySelector("#next-step-action").addEventListener("click", (event) => performAction(event.currentTarget.dataset.action));
  document.querySelector("#refresh-jobs").addEventListener("click", refreshJobs);
  document.querySelector("#run-automation").addEventListener("click", runAutomation);
  document.querySelector("#job-search").addEventListener("input", renderJobs);
  document.querySelector("#job-status-filter").addEventListener("change", renderJobs);
  document.querySelector("#delete-local-data").addEventListener("click", () => {
    document.querySelector("#delete-confirmation").hidden = false;
  });
  document.querySelector("#cancel-delete").addEventListener("click", () => {
    document.querySelector("#delete-confirmation").hidden = true;
  });
  document.querySelector("#submit-attention-response").addEventListener("click", submitAttentionResponse);
}

document.addEventListener("DOMContentLoaded", () => {
  bindStaticActions();
  const initial = location.hash.slice(1);
  navigate(["home", "jobs", "applications", "profile", "settings"].includes(initial) ? initial : "home");
  loadDashboard();
});
