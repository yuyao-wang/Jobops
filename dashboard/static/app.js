"use strict";

const state = {
  page: "home",
  applicationTab: "NEEDS_ATTENTION",
  overview: null,
  profile: null,
  jobs: null,
  applications: null,
  reviewedApplications: null,
  attention: null,
  searchProfiles: null,
  prioritizationPolicy: null,
  preferenceDraft: null,
  jobFinder: null,
  loading: true,
  refreshing: false,
  refreshInvocation: null,
  refreshProgressKey: null,
  automating: false,
  automationInvocation: null,
  automationProgressKey: null,
  automationSnapshotKey: null,
  automationResult: null,
  automationStopping: false,
  automationStarting: false,
  automationStopIntent: false,
  automationStopSending: false,
  automationStopAcknowledged: false,
  automationStopRetryAt: 0,
  automationStopFocusPending: false,
  automationConnectionInterrupted: false,
  automationReconciling: true,
  automationGeneration: 0,
  activeAttentionItem: null,
  activeSubmissionReview: null,
  submissionInProgress: false,
  pendingJobClip: null,
};

const activeAutomationStatuses = new Set(["RUNNING", "STOPPING"]);
const automationStageOrder = [
  "PRIORITY_REFRESH",
  "APPLICATION_PLAN_CREATION",
  "APPLICATION_PREPARATION",
  "BUNDLE_ASSEMBLY",
  "APPLICATION_EXECUTION",
];
const automationStageLabels = {
  PRIORITY_REFRESH: "Evaluate job priority",
  APPLICATION_PLAN_CREATION: "Create application plan",
  APPLICATION_PREPARATION: "Prepare application materials",
  BUNDLE_ASSEMBLY: "Assemble verified application",
  APPLICATION_EXECUTION: "Advance application safely",
};
const automationPhaseLabels = {
  STARTING: "Starting automatic applications",
  PREFLIGHT: "Refreshing jobs and checking readiness",
  LOADING_QUEUE: "Loading eligible jobs",
  PROCESSING: "Processing applications one at a time",
  NEEDS_ATTENTION: "Paused for your attention",
  STOPPING: "Stopping safely",
  STOPPED: "Stopped safely",
  COMPLETED: "Automatic application work completed",
  FAILED: "Automatic application work stopped",
};

let automationPollPromise = null;
let automationPollGeneration = null;

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
  SET_JOB_PREFERENCES: ["Set your job preferences", "Describe and approve the policy Priority will use, then confirm at least one provider query.", "Set preferences", "profile"],
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

class DashboardHttpError extends Error {
  constructor(url, status, detail = "") {
    super(detail || `${url} returned ${status}`);
    this.name = "DashboardHttpError";
    this.url = url;
    this.status = status;
  }
}

async function rawJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : "";
    } catch (_) {
      detail = "";
    }
    throw new DashboardHttpError(url, response.status, detail);
  }
  return response.json();
}

let sessionBootstrapPromise = null;

async function bootstrapLocalSession() {
  if (!sessionBootstrapPromise) {
    sessionBootstrapPromise = rawJson("/api/auth/local-session", {
      method: "POST",
    }).finally(() => {
      sessionBootstrapPromise = null;
    });
  }
  return sessionBootstrapPromise;
}

async function ensureAuthenticated() {
  try {
    return await rawJson("/api/auth/session");
  } catch (error) {
    if (error instanceof DashboardHttpError && error.status === 401) {
      return bootstrapLocalSession();
    }
    throw error;
  }
}

async function requestJson(url, options = {}) {
  try {
    return await rawJson(url, options);
  } catch (error) {
    if (error instanceof DashboardHttpError && error.status === 401) {
      await bootstrapLocalSession();
      return rawJson(url, options);
    }
    throw error;
  }
}

function getJson(url) {
  return requestJson(url);
}

function postJson(url, body) {
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function authenticationFailureMessage(error) {
  if (!(error instanceof DashboardHttpError)) {
    return "JobOps could not reach the local production server.";
  }
  if (error.status === 403) {
    return "Open JobOps from its loopback server URL. Cross-origin authentication is blocked.";
  }
  if (error.status === 404) {
    return "The production authentication endpoint is missing. Start JobOps with python main.py server.";
  }
  if (error.status === 503) {
    return "Production authentication is not fully configured. Restart JobOps after fixing the server configuration.";
  }
  return "JobOps could not establish an authenticated local session.";
}

async function loadDashboard() {
  state.loading = true;
  setHeader("Loading", "");
  try {
    await ensureAuthenticated();
  } catch (error) {
    state.loading = false;
    state.overview = null;
    state.profile = null;
    state.jobs = null;
    state.applications = null;
    state.reviewedApplications = null;
    state.attention = null;
    state.searchProfiles = null;
    state.prioritizationPolicy = null;
    showNotice(authenticationFailureMessage(error));
    setHeader("Authentication required", "is-failed");
    renderAll();
    return;
  }
  const requests = {
    overview: getJson("/api/dashboard/overview"),
    profile: getJson("/api/dashboard/profile"),
    jobs: getJson("/api/dashboard/jobs"),
    applications: getJson("/api/dashboard/applications"),
    reviewedApplications: getJson("/api/reviewed-applications"),
    attention: getJson("/api/human-attention-inbox"),
    searchProfiles: getJson("/api/search-profiles"),
    prioritizationPolicy: getJson("/api/prioritization-policy"),
  };
  const keys = Object.keys(requests);
  const results = await Promise.allSettled(Object.values(requests));
  const failures = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") state[keys[index]] = result.value;
    else {
      state[keys[index]] = null;
      failures.push(result.reason);
    }
  });
  state.loading = false;
  if (failures.length) {
    const unavailable = failures.some((error) => error instanceof DashboardHttpError && error.status === 503);
    const detail = unavailable
      ? "Production read controllers are unavailable. Restart the fully composed server."
      : "System failures are not shown as empty data.";
    showNotice(`${failures.length} Dashboard section${failures.length === 1 ? "" : "s"} could not be loaded. ${detail}`);
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
function showNotice(text, tone = "danger") {
  const node = document.querySelector("#global-notice");
  const safeTone = ["info", "success", "warning", "danger"].includes(tone) ? tone : "danger";
  node.className = `notice is-${safeTone}`;
  node.textContent = text;
  node.hidden = false;
}
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
  if (location.origin !== "null") {
    history.replaceState(null, "", `#${page}`);
  }
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
  renderPrioritizationPolicy();
  if (state.automationResult) renderAutomationProgress(state.automationResult);
  bindDynamicActions();
}

function renderNextStep() {
  const needsApprovedPreferences = state.profile?.profile_state === "READY"
    && state.prioritizationPolicy?.status === "EMPTY";
  const next = needsApprovedPreferences
    ? "SET_JOB_PREFERENCES"
    : state.overview?.next_step || (state.loading ? null : "SYSTEM_ATTENTION");
  const copy = next
    ? (nextStepCopy[next] || nextStepCopy.SYSTEM_ATTENTION)
    : ["Loading your next step…", "Checking current records.", "Loading", ""];
  const title = document.querySelector("#next-step-title");
  const description = document.querySelector("#next-step-description");
  const button = document.querySelector("#next-step-action");
  if (!title || !description || !button) return;
  if (state.automating) {
    title.textContent = state.automationStopping
      ? "Stopping automatic applications safely"
      : "Automatic applications are running";
    description.textContent = state.automationStopping
      ? "JobOps will stop after the current application reaches a safe saved boundary."
      : "JobOps is advancing eligible jobs one at a time. You can view progress or stop the run.";
    button.textContent = "View application progress";
    button.disabled = false;
    button.dataset.action = "applications";
    return;
  }
  title.textContent = copy[0];
  description.textContent = copy[1];
  button.textContent = copy[2];
  button.disabled = !next
    || state.refreshing
    || state.automating
    || state.automationStarting
    || state.automationReconciling;
  button.dataset.action = copy[3];
}

function renderOnboarding() {
  const profile = state.profile;
  const jobs = state.jobs;
  const node = document.querySelector("#onboarding");
  if (!profile || !jobs) { node.hidden = true; return; }
  const profileComplete = profile.profile_state === "READY";
  const preferencesComplete = state.prioritizationPolicy?.status === "ACTIVE"
    && (profile.search_preference_summary?.enabled_profile_count || 0) > 0;
  const jobsComplete = (jobs.counts?.total || 0) > 0;
  node.hidden = profileComplete && preferencesComplete && jobsComplete;
  if (node.hidden) return;
  const steps = [
    ["Add your information", "Give JobOps the verified facts required to prepare applications.", profileComplete, "Profile", "profile"],
    ["Set priorities and sources", "Approve the policy used by Priority and confirm provider queries used for discovery.", preferencesComplete, "Job preferences", "profile"],
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
  node.innerHTML = items.slice(0, 5).map((item) => {
    const job = [
      ...(state.applications?.ordered_items || []),
      ...(state.jobs?.ordered_items || []),
    ].find((value) => value.job_id === item.job_id);
    const context = job
      ? `${job.title} · ${job.company}`
      : `Application ${item.application_plan_id.slice(0, 12)}…`;
    return `
    <article class="attention-item">
      <div class="item-row"><div><h3>${escapeHtml(item.attention_label)}</h3><div class="item-meta">${escapeHtml(context)}</div></div>${pill("NEEDS_ATTENTION", "Action needed")}</div>
      <p>${escapeHtml(item.required_action)}</p>
      <button class="button secondary" data-attention-id="${escapeHtml(item.item_id)}">Review item</button>
      <details class="technical-details"><summary>Technical details</summary><div class="technical-panel">Kind: ${escapeHtml(item.attention_kind)}<br>Stage: ${escapeHtml(item.source_stage)}<br>Answer: ${escapeHtml(item.canonical_answer_key || "Not applicable")}</div></details>
    </article>`;
  }).join("");
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
    ${item.next_action === "REVIEW_AND_SUBMIT" ? `<button class="button primary" data-review-plan="${escapeHtml(item.application_plan_id)}">Review and submit</button>` : ""}
    ${attentionItem ? `<button class="button primary" data-attention-id="${escapeHtml(attentionItem.item_id)}">Resolve required information</button>` : ""}
    <details class="technical-details"><summary>Technical details</summary><div class="technical-panel">Plan: ${escapeHtml(item.application_plan_id)}<br>Job: ${escapeHtml(item.job_id)}</div></details>
  </article>`;
}

function reviewedApplicationMarkup(item) {
  const progress = (item.progress_steps || []).map((step) => `<div class="progress-step ${escapeHtml(step.state)}"><strong>${escapeHtml(step.stage[0] + step.stage.slice(1).toLowerCase())}</strong><br>${escapeHtml(step.state.replaceAll("_", " ").toLowerCase())}</div>`).join("");
  return `<article class="application-card" data-reviewed-run-card="${escapeHtml(item.review_run_id)}">
    <div class="item-row"><div><h3>${escapeHtml(item.title)}</h3><div class="item-meta">${escapeHtml(item.company)} · ${escapeHtml(item.location || "Location not listed")}</div></div>${pill(item.product_status, labels.application[item.product_status] || "Unknown")}</div>
    <p>${escapeHtml(item.safe_status_detail)}</p>
    <div class="progress" aria-label="Application progress">${progress}</div>
    ${item.product_status === "READY" ? `<button class="button primary" data-review-run="${escapeHtml(item.review_run_id)}">Review and submit</button>` : ""}
    <details class="technical-details"><summary>Technical details</summary><div class="technical-panel">Run: ${escapeHtml(item.review_run_id)}<br>Job: ${escapeHtml(item.job_id)}<br>Priority: ${escapeHtml(item.priority || "Not listed")}</div></details>
  </article>`;
}

function renderRecentApplications() {
  const node = document.querySelector("#recent-applications");
  if (!state.overview) { node.innerHTML = state.loading ? emptyState("Loading applications", "Checking current progress.") : failureState(); return; }
  const items = state.overview.recent_applications || [];
  node.innerHTML = items.length ? items.slice(0, 5).map(applicationMarkup).join("") : emptyState("No applications yet", "High-fit jobs will appear here after JobOps creates an application plan.", '<button class="button secondary" data-nav="jobs">View jobs</button>');
}

function readableSource(value) {
  const known = {
    AUTHORIZED_WEB_SEARCH: "authorized web search",
    LINKEDIN_ALERT_EMAIL: "LinkedIn job alert",
    INDEED_ALERT_EMAIL: "Indeed job alert",
    EMPLOYER_OR_ATS_ALERT_EMAIL: "employer or ATS job alert",
    WEB_CLIPPER: "JobOps Web Clipper",
    PASTED_URL: "pasted URL",
    LINKEDIN_SEARCH_INDEX: "LinkedIn search index",
    INDEED_SEARCH_INDEX: "Indeed search index",
    GLASSDOOR_SEARCH_INDEX: "Glassdoor search index",
    EMPLOYER: "employer careers site",
    ATS: "ATS",
    UNKNOWN_WEB: "web source",
  };
  if (!value) return "source";
  return known[value] || String(value).replaceAll("_", " ").toLowerCase();
}

function readableDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toLocaleDateString();
}

function renderNeedsUserJobLeads() {
  const section = document.querySelector("#job-leads-review");
  const count = document.querySelector("#job-leads-review-count");
  const list = document.querySelector("#job-leads-review-list");
  if (!section || !count || !list || !state.jobs) return;
  const leads = state.jobs.needs_user_leads || [];
  if (!leads.length) {
    section.hidden = true;
    count.textContent = "";
    list.innerHTML = "";
    return;
  }
  section.hidden = false;
  const total = state.jobs.lead_summary?.needs_user ?? leads.length;
  count.textContent = `${total} ${total === 1 ? "lead" : "leads"}`;
  list.innerHTML = leads.map((lead) => {
    const source = readableSource(lead.source);
    const origin = readableSource(lead.origin);
    const discovered = readableDate(lead.discovered_at);
    const sourceDetail = source === origin ? source : `${origin} via ${source}`;
    return `<article class="lead-review-item">
      <div>
        <h3>${escapeHtml(lead.title_hint || "Unconfirmed job lead")}</h3>
        <p class="item-meta">${escapeHtml(lead.company_hint || "Company not confirmed")} · ${escapeHtml(lead.location_hint || "Location not confirmed")}</p>
        <p class="item-meta">${escapeHtml(sourceDetail)}${discovered ? ` · Discovered ${escapeHtml(discovered)}` : ""}</p>
        <p>${escapeHtml(lead.reason || "This lead needs your review before it can become a verified job.")}</p>
      </div>
      <div class="lead-review-actions">
        <a class="button secondary" href="${escapeHtml(lead.source_url)}" target="_blank" rel="noopener noreferrer">Open source</a>
        <form class="lead-resolution-form" data-lead-resolution-form data-lead-id="${escapeHtml(lead.lead_id)}">
          <label>
            <span>Official employer or ATS URL</span>
            <input name="official_job_url" type="url" inputmode="url" autocomplete="off" spellcheck="false" required placeholder="https://company.example/careers/job/…">
          </label>
          <button class="button primary" type="submit">Verify and add</button>
          <p class="capability-note" data-lead-resolution-status role="status"></p>
        </form>
      </div>
    </article>`;
  }).join("");
}

async function resolveJobLead(form) {
  const leadId = form.dataset.leadId;
  const input = form.elements.official_job_url;
  const button = form.querySelector('button[type="submit"]');
  const status = form.querySelector("[data-lead-resolution-status]");
  const officialJobUrl = input.value.trim();
  if (!leadId || !officialJobUrl) {
    status.textContent = "Paste the final public employer or ATS job URL.";
    return;
  }
  button.disabled = true;
  input.disabled = true;
  status.textContent = "Verifying the official posting…";
  try {
    const result = await postJson(`/api/job-leads/${encodeURIComponent(leadId)}/resolve`, {
      official_job_url: officialJobUrl,
      invocation_id: invocation("resolve-job-lead"),
    });
    if (["FAILED", "HUMAN_INTERVENTION_REQUIRED"].includes(result.status)) {
      throw new Error(result.message || result.reason || "The official posting could not be verified");
    }
    await loadDashboard();
    navigate("jobs");
    showNotice("The official posting was verified and added to your job library.");
  } catch (error) {
    status.textContent = `Could not add this job: ${error.message}`;
  } finally {
    button.disabled = false;
    input.disabled = false;
  }
}

function renderJobs() {
  const node = document.querySelector("#jobs-list");
  const countsNode = document.querySelector("#job-counts");
  if (!state.jobs) {
    const leadSection = document.querySelector("#job-leads-review");
    if (leadSection) leadSection.hidden = true;
    node.innerHTML = state.loading ? emptyState("Loading jobs", "Reading your subject-scoped library.") : failureState(); countsNode.innerHTML = ""; return;
  }
  if (["FAILED", "INTEGRITY_FAILURE"].includes(state.jobs.read_status)) {
    const leadSection = document.querySelector("#job-leads-review");
    if (leadSection) leadSection.hidden = true;
    node.innerHTML = failureState("Your job library could not be read safely"); countsNode.innerHTML = ""; return;
  }
  renderNeedsUserJobLeads();
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
      <div data-label="Role"><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="job-provenance">${[
        item.discovered_via ? `Discovered via ${readableSource(item.discovered_via)}` : null,
        readableDate(item.source_verified_at) ? `Verified on ${readableDate(item.source_verified_at)}` : null,
        item.authoritative_source ? `Source: ${readableSource(item.authoritative_source)}` : null,
      ].filter(Boolean).map(escapeHtml).join(" · ")}</span></div>
      <div data-label="Company">${escapeHtml(item.company)}</div>
      <div data-label="Location">${escapeHtml(item.location || "Not listed")}</div>
      <div data-label="Why it fits">${escapeHtml((item.match_reasons || [])[0] || "No verified explanation")}</div>
      <div data-label="Status">${pill(item.application_status, labels.job[item.application_status] || "Unknown")}</div>
      <div data-label="Next action">${item.next_action === "RETRY_PRIORITY"
        ? '<button class="button secondary" data-action="refresh">Retry Priority</button>'
        : `<a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">View job</a>`}</div>
    </div>`).join("");
}

function renderApplications() {
  const node = document.querySelector("#applications-list");
  document.querySelectorAll("[data-application-tab]").forEach((button) => {
    if (button.getAttribute("role") === "tab") button.setAttribute("aria-selected", String(button.dataset.applicationTab === state.applicationTab));
  });
  if (!state.applications) { node.innerHTML = state.loading ? emptyState("Loading applications", "Reading current application records.") : failureState(); return; }
  if (["FAILED", "INTEGRITY_FAILURE"].includes(state.applications.read_status)) { node.innerHTML = failureState("Applications could not be read safely"); return; }
  const canonicalItems = (state.applications.ordered_items || []).filter((item) => {
    if (state.applicationTab === "PREPARING") return ["SELECTED", "PREPARING"].includes(item.product_status);
    return item.product_status === state.applicationTab;
  });
  const compatibilityItems = state.reviewedApplications?.status === "SUCCEEDED"
    ? (state.reviewedApplications.items || []).filter((item) => item.product_status === state.applicationTab)
    : [];
  const tabLabel = labels.application[state.applicationTab] || state.applicationTab;
  const markup = [
    ...compatibilityItems.map(reviewedApplicationMarkup),
    ...canonicalItems.map(applicationMarkup),
  ].join("");
  const compatibilityFailure = state.reviewedApplications && state.reviewedApplications.status !== "SUCCEEDED"
    ? failureState("Reviewed compatibility applications could not be read safely")
    : "";
  node.innerHTML = markup || compatibilityFailure || emptyState(`No ${tabLabel.toLowerCase()} applications`, "Applications will move here when their formal status changes.");
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
  document.querySelector("#review-summary").innerHTML = state.profile.capabilities?.review_capability === "UNAVAILABLE"
    ? emptyState("Review queue unavailable", "The production Candidate Fact review capability is not connected yet. No synthetic count is shown.")
    : `<p>${state.profile.review_summary?.pending_proposals ?? 0} proposals waiting for review.</p>`;
}

function renderPrioritizationPolicy() {
  const profileNode = document.querySelector("#preference-summary");
  const jobsNode = document.querySelector("#jobs-preference-summary");
  const response = state.prioritizationPolicy;
  if (!response) {
    if (profileNode) profileNode.innerHTML = failureState("Job preferences could not be loaded");
    if (jobsNode) jobsNode.innerHTML = failureState("Current job preferences could not be loaded");
    return;
  }
  if (response.status === "FAILED") {
    const failure = failureState("The approved job-preference policy could not be read safely");
    if (profileNode) profileNode.innerHTML = failure;
    if (jobsNode) jobsNode.innerHTML = failure;
    return;
  }
  const policy = response.policy;
  if (!policy) {
    if (profileNode) profileNode.innerHTML = emptyState("No approved priority preferences", "Describe your preferences, review the AI summary, and approve it before Priority ranks jobs.");
    if (jobsNode) jobsNode.innerHTML = emptyState("No current job preferences", "Add and approve job preferences from your Profile.");
    return;
  }
  const hard = policy.hard_constraints || [];
  const soft = policy.soft_preferences || [];
  const heading = `<div class="preference-policy-heading"><div><strong>Active policy v${policy.policy_version}</strong><div class="item-meta">Approved ${new Date(policy.approved_at).toLocaleString()}</div></div>${pill("READY", "Used by discovery and Priority")}</div>`;
  const hardMarkup = hard.length ? `<h3>Hard constraints</h3><ul class="preference-list">${hard.map((item) => `<li><strong>${escapeHtml(item.constraint_type.replaceAll("_", " ").toLowerCase())}</strong>: ${escapeHtml(item.normalized_value)}</li>`).join("")}</ul>` : "";
  if (profileNode) {
    profileNode.innerHTML = `${heading}${hardMarkup}${soft.length ? `
      <form class="preference-editor" data-preference-editor data-policy-version="${policy.policy_version}">
        ${soft.map((item) => `<div class="preference-edit-row" data-preference-row data-preference-id="${escapeHtml(item.preference_id)}">
          <label><span>${escapeHtml(item.category === "ROLE" ? "Role or title phrase" : item.category.replaceAll("_", " ").toLowerCase())}</span><input data-preference-statement maxlength="2000" value="${escapeHtml(item.statement)}"></label>
          <label><span>Importance</span><select data-preference-importance>
            <option value="" ${!item.importance ? "selected" : ""}>not specified</option>
            ${["HIGH", "MEDIUM", "LOW"].map((value) => `<option value="${value}" ${item.importance === value ? "selected" : ""}>${value.toLowerCase()}</option>`).join("")}
          </select></label>
        </div>`).join("")}
        <div class="button-row"><button class="button primary" type="submit" data-save-preferences>Save preferences</button></div>
        <p class="capability-note" data-preference-save-status role="status"></p>
      </form>` : '<p>No editable preferences are active. Add preferences with AI first.</p>'}`;
  }
  if (jobsNode) {
    const description = soft.length === 1
      ? "1 approved preference currently controls discovery and Priority."
      : `${soft.length} approved preferences currently control discovery and Priority.`;
    jobsNode.innerHTML = `${heading}<p>${description}</p>${hardMarkup}${soft.length ? `<h3>Preferences</h3><ul class="preference-list">${soft.map((item) => `<li><strong>${escapeHtml(item.category.toLowerCase())}${item.importance ? ` · ${escapeHtml(item.importance.toLowerCase())}` : ""}</strong>: ${escapeHtml(item.statement)}</li>`).join("")}</ul>` : '<p>No soft preferences are active.</p>'}`;
  }
}

async function savePreferenceItems(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("[data-save-preferences]");
  const status = form.querySelector("[data-preference-save-status]");
  const preferences = [...form.querySelectorAll("[data-preference-row]")].map((row) => ({
    preference_id: row.dataset.preferenceId,
    statement: row.querySelector("[data-preference-statement]").value.trim(),
    importance: row.querySelector("[data-preference-importance]").value,
  }));
  if (preferences.some((item) => !item.statement)) {
    status.textContent = "Every preference needs a value.";
    return;
  }
  button.disabled = true;
  status.textContent = "Saving exact preference changes…";
  try {
    const result = await postJson("/api/prioritization-policy/preferences", {
      expected_policy_version: Number(form.dataset.policyVersion),
      preferences,
    });
    if (result.status !== "SUCCEEDED" || !result.policy) throw new Error(result.message || "Preference update failed");
    await loadDashboard();
    showNotice(`Preference policy v${result.policy.policy_version} is active. Refresh the job library to discover and re-evaluate jobs with these conditions.`);
  } catch (error) {
    status.textContent = `Could not save preferences: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function resetJobFinder() {
  state.jobFinder = {
    conversationId: invocation("job-finder"),
    messages: [],
    history: [],
    response: null,
    busy: false,
  };
  document.querySelector("#job-finder-input").value = "";
  renderJobFinder();
}

function renderJobFinder() {
  const finder = state.jobFinder;
  if (!finder) return;
  const transcript = document.querySelector("#job-finder-transcript");
  const results = document.querySelector("#job-finder-results");
  const status = document.querySelector("#job-finder-status");
  const input = document.querySelector("#job-finder-input");
  const send = document.querySelector("#send-job-finder-message");
  transcript.hidden = finder.history.length === 0;
  transcript.innerHTML = finder.history.length
    ? finder.history.map((turn) => `<div class="job-finder-turn ${turn.role}"><strong>${turn.role === "user" ? "You" : "JobOps"}</strong><p>${escapeHtml(turn.text)}</p></div>`).join("")
    : "";
  results.innerHTML = "";
  const response = finder.response;
  const canClarify = !response || (
    response.status === "NEEDS_USER"
    && !response.candidate_set_id
    && !response.pending_intake_id
    && finder.messages.length < 2
  );
  input.disabled = finder.busy || !canClarify;
  send.disabled = finder.busy || !canClarify;
  send.textContent = finder.busy
    ? "Looking…"
    : finder.messages.length === 0
      ? "Add position"
      : "Send clarification";
  if (!response) {
    status.textContent = "";
    return;
  }
  status.textContent = response.prompt || "Review the result below.";
  if ((response.candidates || []).length) {
    results.innerHTML = `
      <div class="review-panel">
        <h3>Choose the job you meant</h3>
        <div class="job-finder-candidates">
          ${response.candidates.map((candidate) => `
            <button class="job-finder-candidate" type="button" data-job-candidate="${escapeHtml(candidate.candidate_id)}">
              <strong>${escapeHtml(candidate.title)}</strong>
              <span>${escapeHtml(candidate.company)}${candidate.location ? ` · ${escapeHtml(candidate.location)}` : ""}</span>
              <small>${escapeHtml(candidate.source_platform)}</small>
            </button>`).join("")}
        </div>
      </div>`;
    results.querySelectorAll("[data-job-candidate]").forEach((node) => {
      node.addEventListener("click", () => selectJobFinderCandidate(node.dataset.jobCandidate));
    });
    return;
  }
  if (response.pending_intake_id && (response.actions || []).length) {
    const summary = response.summary || {};
    results.innerHTML = `
      <div class="review-panel">
        <h3>Review the job before adding it</h3>
        <p><strong>${escapeHtml(summary.title || "Job")}</strong><br>${escapeHtml(summary.company || "")}${summary.location ? ` · ${escapeHtml(summary.location)}` : ""}</p>
        <p class="item-meta">Source: ${escapeHtml(summary.source_platform || "public job source")}. AI output cannot authorize this write.</p>
        <div class="button-row">
          ${response.actions.includes("ADD_JOB") ? '<button class="button primary" type="button" data-job-action="ADD_JOB">Add to job list</button>' : ""}
          ${response.actions.includes("REQUEST_APPLICATION") ? '<button class="button secondary" type="button" data-job-action="REQUEST_APPLICATION">Add + record application intent</button>' : ""}
        </div>
      </div>`;
    results.querySelectorAll("[data-job-action]").forEach((node) => {
      node.addEventListener("click", () => resolveJobFinderIntake(node.dataset.jobAction));
    });
    return;
  }
  if (response.status === "COMPLETED") {
    results.innerHTML = `<div class="review-panel"><h3>Job list updated</h3><p>${escapeHtml(response.prompt)}</p></div>`;
  }
}

async function sendJobFinderMessage() {
  const finder = state.jobFinder;
  const input = document.querySelector("#job-finder-input");
  const message = input.value.trim();
  if (!finder || !message || finder.messages.length >= 2 || finder.busy) {
    document.querySelector("#job-finder-status").textContent = finder?.messages.length >= 2
      ? "One clarification was already used. Start a new request with a company and title, or one public URL."
      : "Enter a job clue first.";
    return;
  }
  finder.messages.push(message);
  finder.history.push({ role: "user", text: message });
  finder.busy = true;
  input.value = "";
  renderJobFinder();
  try {
    const result = await postJson("/api/job-finder/message", {
      conversation_id: finder.conversationId,
      messages: finder.messages,
    });
    finder.response = result;
    finder.history.push({ role: "assistant", text: result.prompt || "Review the result below." });
  } catch (error) {
    finder.response = { status: "FAILED", prompt: `The job finder could not continue: ${error.message}` };
    finder.history.push({ role: "assistant", text: finder.response.prompt });
  } finally {
    finder.busy = false;
    renderJobFinder();
  }
}

async function selectJobFinderCandidate(candidateId) {
  const finder = state.jobFinder;
  if (!finder || finder.busy || !finder.response?.candidate_set_id) return;
  finder.busy = true;
  renderJobFinder();
  try {
    const result = await postJson("/api/job-finder/select", {
      conversation_id: finder.conversationId,
      candidate_set_id: finder.response.candidate_set_id,
      candidate_id: candidateId,
    });
    finder.response = result;
    finder.history.push({ role: "assistant", text: result.prompt || "Review the selected job." });
  } catch (error) {
    finder.response = { status: "FAILED", prompt: `The selected job could not be read: ${error.message}` };
    finder.history.push({ role: "assistant", text: finder.response.prompt });
  } finally {
    finder.busy = false;
    renderJobFinder();
  }
}

async function resolveJobFinderIntake(action) {
  const finder = state.jobFinder;
  if (!finder || finder.busy || !finder.response?.pending_intake_id) return;
  finder.busy = true;
  renderJobFinder();
  try {
    const result = await postJson("/api/job-finder/resolve", {
      conversation_id: finder.conversationId,
      pending_intake_id: finder.response.pending_intake_id,
      action,
    });
    finder.response = result;
    finder.history.push({ role: "assistant", text: result.prompt || "Job intake finished." });
    if (result.status === "COMPLETED") {
      await loadDashboard();
    }
  } catch (error) {
    finder.response = { status: "FAILED", prompt: `The job was not added: ${error.message}` };
    finder.history.push({ role: "assistant", text: finder.response.prompt });
  } finally {
    finder.busy = false;
    renderJobFinder();
  }
}

function openPreferenceDialog() {
  const policy = state.prioritizationPolicy?.policy;
  document.querySelector("#preference-input").value = policy?.raw_preference_text || "";
  document.querySelector("#preference-dialog-status").textContent = "The NLP interpreter creates a review draft only; it cannot rank jobs or take application actions.";
  state.preferenceDraft = null;
  renderPreferenceDraft();
  document.querySelector("#preference-dialog").showModal();
}

function renderPreferenceDraft() {
  const node = document.querySelector("#preference-draft");
  const confirmation = document.querySelector("#hard-constraint-confirmation");
  const approve = document.querySelector("#approve-preferences");
  const draft = state.preferenceDraft;
  document.querySelector("#confirm-hard-constraints").checked = false;
  if (!draft) {
    node.hidden = true;
    node.innerHTML = "";
    confirmation.hidden = true;
    approve.hidden = true;
    return;
  }
  node.hidden = false;
  const hard = draft.hard_constraints || [];
  const soft = draft.soft_preferences || [];
  const ambiguities = draft.ambiguities || [];
  node.innerHTML = `
    <div class="review-panel">
      <h3>NLP summary — review before approval</h3>
      ${hard.length ? `<h4>Hard constraints</h4><ul class="preference-list">${hard.map((item) => `<li><strong>${escapeHtml(item.constraint_type.replaceAll("_", " ").toLowerCase())}</strong>: ${escapeHtml(item.normalized_value)}<div class="item-meta">From: “${escapeHtml(item.source_excerpt)}”</div></li>`).join("")}</ul>` : "<p>No hard constraints extracted.</p>"}
      ${soft.length ? `<h4>Soft preferences</h4><ul class="preference-list">${soft.map((item) => `<li><strong>${escapeHtml(item.category.toLowerCase())}${item.importance ? ` · ${escapeHtml(item.importance.toLowerCase())}` : ""}</strong>: ${escapeHtml(item.statement)}<div class="item-meta">From: “${escapeHtml(item.source_excerpt)}”</div></li>`).join("")}</ul>` : "<p>No soft preferences extracted.</p>"}
      ${ambiguities.length ? `<div class="capability-note"><strong>Clarification required</strong><ul>${ambiguities.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><p>Revise the text and summarize again. This draft cannot be approved.</p></div>` : ""}
    </div>`;
  const ready = draft.status === "READY_FOR_APPROVAL" && !ambiguities.length;
  confirmation.hidden = !ready || !hard.length;
  approve.hidden = !ready;
}

async function interpretPreferences() {
  const rawText = document.querySelector("#preference-input").value.trim();
  const status = document.querySelector("#preference-dialog-status");
  const button = document.querySelector("#interpret-preferences");
  if (!rawText) {
    status.textContent = "Describe at least one job preference first.";
    return;
  }
  button.disabled = true;
  status.textContent = "Interpreting one review-only NLP draft…";
  try {
    const result = await postJson("/api/prioritization-policy/draft", { raw_preference_text: rawText });
    if (result.status === "FAILED" || !result.draft) {
      throw new Error(result.reason === "INTERPRETER_FAILED"
        ? "The configured AI backend could not interpret these preferences. Check the typed AI configuration status and try again."
        : result.message || "Preference interpretation failed");
    }
    state.preferenceDraft = result.draft;
    renderPreferenceDraft();
    status.textContent = result.draft.status === "NEEDS_CLARIFICATION"
      ? "The draft found ambiguity. Revise your text and summarize again."
      : "Review every extracted item, then explicitly approve it.";
  } catch (error) {
    state.preferenceDraft = null;
    renderPreferenceDraft();
    status.textContent = `Could not interpret preferences: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function approvePreferences() {
  const draft = state.preferenceDraft;
  const status = document.querySelector("#preference-dialog-status");
  if (!draft) return;
  const hasHardConstraints = (draft.hard_constraints || []).length > 0;
  const confirmed = document.querySelector("#confirm-hard-constraints").checked;
  if (hasHardConstraints && !confirmed) {
    status.textContent = "Explicitly confirm every hard constraint before approval.";
    return;
  }
  const button = document.querySelector("#approve-preferences");
  button.disabled = true;
  status.textContent = "Approving this exact reviewed policy…";
  try {
    const result = await postJson("/api/prioritization-policy/approve", {
      draft_id: draft.draft_id,
      confirm_hard_constraints: confirmed,
    });
    if (result.status !== "SUCCEEDED" || !result.policy) {
      throw new Error(result.message || "Policy approval failed");
    }
    document.querySelector("#preference-dialog").close();
    state.preferenceDraft = null;
    await loadDashboard();
    navigate("profile");
    showNotice(`Job preference policy v${result.policy.policy_version} is active. Refresh the job library to create current Priority decisions.`);
  } catch (error) {
    status.textContent = `Could not approve preferences: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function refreshJobs() {
  if (state.refreshing) return;
  state.refreshing = true;
  state.refreshProgressKey = null;
  updateRunningButtons();
  try {
    const invocationId = invocation("dashboard-refresh");
    const started = await postJson("/api/job-library/refresh", {
      invocation_id: invocationId,
    });
    if (started.status === "FAILED") throw new Error(started.message || "Refresh failed");
    state.refreshInvocation = started.invocation_id || invocationId;
    showNotice("Searching configured providers and discovery channels, resolving official postings, updating the local job library, and refreshing Priority… You can keep using this page.", "info");
    const result = started.status === "RUNNING"
      ? await waitForRefreshCompletion()
      : started;
    await loadDashboard();
    showRefreshResult(result);
  } catch (error) {
    showNotice(`Job library refresh failed: ${error.message}`);
  } finally {
    state.refreshing = false;
    state.refreshInvocation = null;
    updateRunningButtons();
  }
}

function pause(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForRefreshCompletion() {
  while (true) {
    await pause(1000);
    const result = await getJson("/api/job-library/refresh/status");
    const summary = result.summary || {};
    const sourceProgressKey = (result.source_results || []).map((item) => [
      item.result_type || "PROVIDER_QUERY",
      item.provider || "",
      item.acquisition_source || "",
      item.requests || 0,
      item.completed || 0,
      item.search_hits || 0,
      item.public_reads || 0,
      item.leads_resolved || 0,
      item.leads_needing_review || 0,
      item.lead_failures || 0,
      item.truncated ? 1 : 0,
    ].join(",")).join("|");
    const progressKey = [
      result.phase || "",
      summary.completed_profiles || 0,
      summary.candidates_processed || 0,
      summary.jobs_created || 0,
      summary.jobs_updated || 0,
      summary.lead_requests_completed || 0,
      summary.leads_discovered || 0,
      summary.leads_resolved || 0,
      summary.leads_needing_review || 0,
      summary.lead_public_reads || 0,
      summary.lead_failures || 0,
      summary.lead_search_truncated ? 1 : 0,
      sourceProgressKey,
    ].join(":");
    if (progressKey !== state.refreshProgressKey) {
      state.refreshProgressKey = progressKey;
      if ((summary.candidates_processed || 0) > 0 || (summary.leads_resolved || 0) > 0) {
        await loadJobsSnapshot();
      }
    }
    if (result.status !== "RUNNING") return result;
  }
}

async function loadJobsSnapshot() {
  try {
    state.jobs = await getJson("/api/dashboard/jobs");
    renderJobs();
    bindDynamicActions();
  } catch (error) {
    showNotice(`Jobs were found, but the current job-list snapshot could not be loaded: ${error.message}`);
  }
}

async function resumeRefreshIfRunning() {
  if (state.refreshing) return;
  try {
    const current = await getJson("/api/job-library/refresh/status");
    if (current.status !== "RUNNING") return;
    state.refreshing = true;
    state.refreshInvocation = current.invocation_id;
    state.refreshProgressKey = null;
    updateRunningButtons();
    showNotice("A job-library refresh is still running. Results will appear here when it completes.", "info");
    const result = await waitForRefreshCompletion();
    await loadDashboard();
    showRefreshResult(result);
  } catch (error) {
    showNotice(`Could not read the job-library refresh status: ${error.message}`);
  } finally {
    state.refreshing = false;
    state.refreshInvocation = null;
    updateRunningButtons();
  }
}

function showRefreshResult(result) {
  const summary = result.summary || {};
  const failedQueries = Math.max(0, (summary.completed_profiles || 0) - (summary.searched_profiles || 0));
  const searchSummary = `${summary.completed_profiles || 0} of ${summary.enabled_profiles || 0} provider requests returned; ${summary.searched_profiles || 0} valid responses, ${failedQueries} failures; ${summary.profiles_with_matches || 0} returned matching titles and ${summary.zero_result_profiles || 0} returned zero; ${summary.candidates_found || 0} filtered matches became ${summary.unique_candidates || 0} unique job URLs`;
  const leadSummary = summary.lead_refresh_ran
    ? `${summary.leads_discovered || 0} leads discovered from enabled discovery channels; ${summary.leads_unique || 0} unique, ${summary.leads_deduplicated || 0} duplicates removed; ${summary.leads_resolved || 0} resolved to verified official postings, ${summary.leads_needing_review || 0} need review${summary.lead_failures ? `, ${summary.lead_failures} failed` : ""}${summary.lead_search_truncated ? "; configured discovery limit reached" : ""}`
    : "";
  const prioritySummary = `${summary.priorities_refreshed || 0} of ${summary.priorities_requested || 0} selected Priority decisions refreshed${summary.priorities_failed ? `; ${summary.priorities_failed} failed` : ""}`;
  const librarySummary = `${summary.jobs_created || 0} new, ${summary.jobs_updated || 0} updated, ${summary.jobs_unchanged || 0} already current`;
  const sourceFailures = result.source_failures || [];
  const priorityFailures = result.priority_failures || [];
  const sourceStageFailed = failedQueries > 0
    || (summary.jobs_failed || 0) > 0
    || (summary.jobs_skipped || 0) > 0
    || (summary.lead_failures || 0) > 0
    || sourceFailures.length > 0;
  const priorityStageFailed = (summary.priorities_failed || 0) > 0
    || priorityFailures.length > 0;
  const sourceFailureDetail = sourceFailures.slice(0, 3).join(" ");
  const priorityFailureDetail = priorityFailures.slice(0, 3).map((item) => (
    `${item.count || 1} × ${item.message || "Priority evaluation failed."}`
  )).join(" ");
  if (result.last_completed_refresh_time) {
    document.querySelector("#jobs-refresh-detail").textContent = `Last refreshed ${new Date(result.last_completed_refresh_time).toLocaleString()}. ${searchSummary}.${leadSummary ? ` ${leadSummary}.` : ""}`;
  }
  if (result.status === "PARTIAL_FAILURE") {
    if (priorityStageFailed && !sourceStageFailed) {
      showNotice(`Source search and job-library update completed: ${searchSummary};${leadSummary ? ` ${leadSummary};` : ""} ${librarySummary}. Priority needs attention: ${prioritySummary}.${priorityFailureDetail ? ` ${priorityFailureDetail}` : ""}`, "warning");
      setHeader("Priority needs attention", "is-warning");
      return;
    }
    const failureDetail = [sourceFailureDetail, priorityFailureDetail].filter(Boolean).join(" ");
    showNotice(`Job library refresh completed with partial failures: ${searchSummary};${leadSummary ? ` ${leadSummary};` : ""} ${librarySummary}; ${prioritySummary}.${failureDetail ? ` ${failureDetail}` : " Review the refresh details below."}`, "warning");
    setHeader("Needs attention", "is-warning");
    return;
  }
  if (result.status === "FAILED") {
    const failureDetail = [sourceFailureDetail, priorityFailureDetail].filter(Boolean).join(" ");
    showNotice(`Job library refresh failed: ${searchSummary}; ${librarySummary}.${failureDetail ? ` ${failureDetail}` : ""}`, "danger");
    setHeader("Refresh failed", "is-failed");
    return;
  }
  if (result.status === "NOOP") {
    showNotice("No enabled provider search or lead discovery source was found, so the job library was not changed.", "info");
    return;
  }
  if (result.status === "RUNNING") {
    showNotice("A job-library refresh is already running. This click did not start a second refresh.", "info");
    return;
  }
  const providerChanges = (summary.jobs_created || 0) + (summary.jobs_updated || 0);
  const resolvedLeadJobs = summary.leads_resolved || 0;
  showNotice(providerChanges || resolvedLeadJobs
    ? `Job library refreshed: ${summary.jobs_created || 0} provider-feed jobs added, ${summary.jobs_updated || 0} provider-feed jobs updated, ${summary.jobs_unchanged || 0} already current, and ${resolvedLeadJobs} discovery leads resolved to verified jobs; ${searchSummary};${leadSummary ? ` ${leadSummary};` : ""} ${prioritySummary}.`
    : `Job library refresh completed with no new or changed verified jobs; ${librarySummary}; ${searchSummary};${leadSummary ? ` ${leadSummary};` : ""} ${prioritySummary}.`, "success");
}

function automationCount(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

function automationStatusMessage(result) {
  const status = String(result?.status || "FAILED");
  if (typeof result?.message === "string" && result.message.trim()) {
    return result.message.trim();
  }
  const cycles = automationCount(result?.cycles_completed);
  const total = automationCount(result?.total_jobs);
  const current = automationCount(result?.current_job_index);
  const progress = total
    ? `Working on application ${Math.min(current || cycles + 1, total)} of ${total}.`
    : "Checking the next eligible job.";
  const messages = {
    IDLE: "Automatic applications have not started.",
    RUNNING: `${progress} Completed work is saved after every safe boundary.`,
    STOPPING: "Stopping safely after the current application reaches a saved boundary.",
    STOPPED: "Automatic applications stopped safely. Completed work remains saved, and you can continue later.",
    COMPLETED: `Automatic applications completed after ${cycles} cycle${cycles === 1 ? "" : "s"}.`,
    PARTIAL_FAILURE: "Automatic applications stopped safely before every eligible job could advance. Review the items that need attention.",
    FAILED: "Automatic applications could not continue safely. Review the current system issue before trying again.",
    NOOP: "There are no jobs ready for automatic application work.",
    UNCHANGED: "This automation invocation was already completed; no duplicate work was performed.",
  };
  return messages[status] || "Automatic application status is unavailable.";
}

function automationStageTone(status) {
  if (["COMPLETED", "UNCHANGED", "NOOP"].includes(status)) return "is-complete";
  if (["RUNNING", "CURRENT"].includes(status)) return "is-current";
  if (["PARTIAL_FAILURE", "DEFERRED", "BLOCKED", "STOPPED"].includes(status)) return "is-warning";
  if (status === "FAILED") return "is-failed";
  return "is-pending";
}

function renderAutomationProgress(result) {
  const panel = document.querySelector("#automation-progress");
  if (!panel || !result) return;
  const status = String(result.status || "FAILED");
  if (status === "IDLE" && !state.automationInvocation) {
    panel.hidden = true;
    return;
  }
  const summary = result.summary || {};
  const phase = String(result.phase || "").toUpperCase();
  const phaseIndex = automationStageOrder.indexOf(phase);
  const reported = new Map((result.stages || []).map((stage) => [String(stage.stage || "").toUpperCase(), stage]));
  panel.hidden = false;
  panel.setAttribute("aria-busy", String(activeAutomationStatuses.has(status)));
  const title = document.querySelector("#automation-progress-title");
  const statusNode = document.querySelector("#automation-progress-status");
  const message = document.querySelector("#automation-progress-message");
  const currentJob = automationCount(result.current_job_index);
  const totalJobs = automationCount(result.total_jobs);
  const libraryJobs = Array.isArray(state.jobs?.ordered_items)
    ? state.jobs.ordered_items.length
    : 0;
  title.textContent = activeAutomationStatuses.has(status)
    ? automationStageLabels[phase] || automationPhaseLabels[phase] || "Automatic applications in progress"
    : "Last automatic application result";
  statusNode.className = `status-pill ${["COMPLETED", "NOOP", "UNCHANGED"].includes(status) ? "success" : ["FAILED", "PARTIAL_FAILURE"].includes(status) ? "danger" : ["STOPPING", "STOPPED"].includes(status) ? "warning" : "neutral"}`;
  statusNode.textContent = status.replaceAll("_", " ").toLowerCase();
  message.textContent = automationStatusMessage(result);
  document.querySelector("#automation-progress-summary").innerHTML = [
    [automationCount(result.cycles_completed), "Cycles completed"],
    [totalJobs ? `${Math.min(currentJob, totalJobs)}/${totalJobs}` : "—", "Eligible queue position"],
    [libraryJobs || "—", "Jobs in library"],
    [automationCount(summary.plans_created), "Plans created"],
    [automationCount(summary.preparation_completed), "Applications prepared"],
    [automationCount(summary.execution_completed), "Applications advanced"],
  ].map(([value, label]) => `<div class="summary-item"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
  document.querySelector("#automation-stage-list").innerHTML = automationStageOrder.map((stageName, index) => {
    const stage = reported.get(stageName) || {};
    let stageStatus = String(stage.status || "PENDING").toUpperCase();
    if (!reported.has(stageName) && activeAutomationStatuses.has(status) && phaseIndex >= 0) {
      stageStatus = index < phaseIndex ? "COMPLETED" : index === phaseIndex ? "RUNNING" : "PENDING";
    }
    const facts = [
      automationCount(stage.completed) ? `${automationCount(stage.completed)} completed` : null,
      automationCount(stage.deferred) ? `${automationCount(stage.deferred)} deferred` : null,
      automationCount(stage.failed) ? `${automationCount(stage.failed)} failed` : null,
      automationCount(stage.uncertain) ? `${automationCount(stage.uncertain)} uncertain` : null,
    ].filter(Boolean).join(" · ");
    return `<li class="automation-stage ${automationStageTone(stageStatus)}"><span class="automation-stage-marker" aria-hidden="true"></span><span><strong>${escapeHtml(automationStageLabels[stageName])}</strong><small>${escapeHtml(facts || stageStatus.replaceAll("_", " ").toLowerCase())}</small></span></li>`;
  }).join("");
}

function automationProgressKey(result) {
  return JSON.stringify([
    result.invocation_id || "",
    result.status || "",
    result.phase || "",
    result.message || "",
    Boolean(result.stop_requested),
    automationCount(result.cycles_completed),
    automationCount(result.total_jobs),
    automationCount(result.current_job_index),
    result.stages || [],
    result.stage_failures || [],
    result.summary || {},
  ]);
}

function automationSnapshotKey(result) {
  const summary = result.summary || {};
  return [
    automationCount(result.cycles_completed),
    automationCount(result.current_job_index),
    automationCount(summary.plans_created),
    automationCount(summary.preparation_completed),
    automationCount(summary.bundles_assembled),
    automationCount(summary.execution_completed),
    automationCount(summary.execution_uncertain),
  ].join(":");
}

function adoptAutomationResult(result, generation = state.automationGeneration) {
  if (generation !== state.automationGeneration) return state.automationResult;
  if (!result || typeof result !== "object") throw new Error("Automation returned an invalid status.");
  const stopHadFocus = document.activeElement?.id === "stop-automation";
  const previous = state.automationResult;
  const previousStatus = String(previous?.status || "IDLE");
  const incomingInvocation = typeof result.invocation_id === "string"
    ? result.invocation_id
    : "";
  const sameInvocation = Boolean(
    incomingInvocation
    && incomingInvocation !== "none"
    && incomingInvocation === previous?.invocation_id
  );
  let normalized = result;
  let status = String(normalized.status || "FAILED");
  if (
    sameInvocation
    && !activeAutomationStatuses.has(previousStatus)
    && previousStatus !== "IDLE"
    && activeAutomationStatuses.has(status)
  ) {
    return previous;
  }
  if (Boolean(normalized.stop_requested)) state.automationStopIntent = true;
  if (state.automationStopIntent && activeAutomationStatuses.has(status) && status !== "STOPPING") {
    normalized = {
      ...normalized,
      status: "STOPPING",
      stop_requested: true,
      message: previousStatus === "STOPPING" && previous?.message
        ? previous.message
        : "Stop requested. Waiting for the current application to reach a safe saved boundary.",
    };
    status = "STOPPING";
  }
  if (incomingInvocation && incomingInvocation !== "none") {
    state.automationInvocation = incomingInvocation;
  }
  state.automationResult = normalized;
  state.automating = activeAutomationStatuses.has(status);
  state.automationStopping = status === "STOPPING"
    || (state.automating && Boolean(normalized.stop_requested));
  state.automationConnectionInterrupted = false;
  state.automationProgressKey = automationProgressKey(normalized);
  if (!state.automating) {
    state.automationStopIntent = false;
    state.automationStopSending = false;
    state.automationStopAcknowledged = false;
    state.automationStopRetryAt = 0;
  }
  renderAutomationProgress(normalized);
  updateRunningButtons();
  if (!state.automating && (stopHadFocus || state.automationStopFocusPending)) {
    const title = document.querySelector("#automation-progress-title");
    if (title) {
      title.tabIndex = -1;
      title.focus({ preventScroll: true });
    }
    state.automationStopFocusPending = false;
  }
  return normalized;
}

function showAutomationResult(result) {
  renderAutomationProgress(result);
  const status = String(result?.status || "FAILED");
  if (activeAutomationStatuses.has(status) || status === "IDLE") return;
  showNotice(automationStatusMessage(result));
  const heading = {
    COMPLETED: ["Automation complete", ""],
    NOOP: ["No applications ready", ""],
    UNCHANGED: ["Automation already complete", ""],
    STOPPED: ["Automation stopped", ""],
    PARTIAL_FAILURE: ["Needs attention", "is-failed"],
    FAILED: ["Needs attention", "is-failed"],
  }[status] || ["Automation updated", ""];
  setHeader(heading[0], heading[1]);
}

function automationRetryDelay(failureCount) {
  return Math.min(5000, 500 * (2 ** Math.min(Math.max(failureCount - 1, 0), 3)));
}

function showAutomationConnectionIssue(detail = "Status connection interrupted; retrying automatically.") {
  state.automationConnectionInterrupted = true;
  const message = document.querySelector("#automation-progress-message");
  if (!message) return;
  const current = automationStatusMessage(state.automationResult);
  message.textContent = `${current} ${detail}`;
}

async function requestAutomationStop(generation) {
  if (
    generation !== state.automationGeneration
    || !state.automationStopIntent
    || state.automationStopSending
    || state.automationStopAcknowledged
    || !state.automationInvocation
    || Date.now() < state.automationStopRetryAt
  ) return state.automationResult;
  state.automationStopSending = true;
  updateRunningButtons();
  try {
    const result = await postJson("/api/automation-cycle/stop", {
      invocation_id: state.automationInvocation,
    });
    if (generation !== state.automationGeneration) return state.automationResult;
    state.automationStopAcknowledged = true;
    state.automationStopRetryAt = 0;
    return adoptAutomationResult(result, generation);
  } catch (error) {
    if (generation !== state.automationGeneration) return state.automationResult;
    state.automationStopRetryAt = Date.now() + 1500;
    showAutomationConnectionIssue(
      `The stop response was interrupted (${error.message}); JobOps will retry the stop request while checking server status.`
    );
    return state.automationResult;
  } finally {
    if (generation === state.automationGeneration) {
      state.automationStopSending = false;
      updateRunningButtons();
    }
  }
}

async function waitForAutomationCompletion(generation = state.automationGeneration) {
  if (automationPollPromise && automationPollGeneration === generation) {
    return automationPollPromise;
  }
  const currentPromise = (async () => {
    let pollFailures = 0;
    while (generation === state.automationGeneration) {
      await pause(pollFailures ? automationRetryDelay(pollFailures) : 750);
      if (generation !== state.automationGeneration) return state.automationResult;
      if (!state.automating && state.automationResult) return state.automationResult;
      let result;
      try {
        result = await getJson("/api/automation-cycle/status");
        if (!result || typeof result !== "object") {
          throw new Error("Automation returned an invalid status.");
        }
      } catch (error) {
        pollFailures += 1;
        if (generation !== state.automationGeneration) return state.automationResult;
        showAutomationConnectionIssue(
          `Status connection interrupted (${error.message}); retrying automatically. The Stop control remains effective when the server reconnects.`
        );
        continue;
      }
      pollFailures = 0;
      const progressKey = automationProgressKey(result);
      const snapshotKey = automationSnapshotKey(result);
      const snapshotChanged = snapshotKey !== state.automationSnapshotKey;
      const connectionRecovered = state.automationConnectionInterrupted;
      if (progressKey !== state.automationProgressKey || connectionRecovered) {
        adoptAutomationResult(result, generation);
      }
      if (generation !== state.automationGeneration) return state.automationResult;
      if (
        state.automationStopIntent
        && state.automating
        && !state.automationStopSending
        && !state.automationStopAcknowledged
        && Date.now() >= state.automationStopRetryAt
      ) {
        await requestAutomationStop(generation);
      }
      if (generation !== state.automationGeneration) return state.automationResult;
      const observed = state.automationResult || result;
      if (snapshotChanged) {
        state.automationSnapshotKey = snapshotKey;
        if (activeAutomationStatuses.has(String(observed.status || ""))) {
          await loadDashboard();
          if (generation !== state.automationGeneration) return state.automationResult;
          renderAutomationProgress(observed);
          updateRunningButtons();
        }
      }
      if (!activeAutomationStatuses.has(String(observed.status || ""))) return observed;
    }
    return state.automationResult;
  })();
  automationPollPromise = currentPromise;
  automationPollGeneration = generation;
  try {
    return await currentPromise;
  } finally {
    if (automationPollPromise === currentPromise) {
      automationPollPromise = null;
      automationPollGeneration = null;
    }
  }
}

function failedAutomationStart(invocationId, error = null) {
  return {
    status: "FAILED",
    invocation_id: invocationId,
    message: error
      ? `Automatic applications did not start: ${error.message}`
      : "Automatic applications did not start, and no server-side run is active.",
    stages: state.automationResult?.stages || [],
    summary: state.automationResult?.summary || {},
  };
}

async function runAutomation() {
  if (state.automating || state.automationStarting || state.automationReconciling) return;
  const generation = ++state.automationGeneration;
  const invocationId = invocation("dashboard-automation");
  state.automationInvocation = invocationId;
  state.automationProgressKey = null;
  state.automationSnapshotKey = null;
  state.automationStopping = false;
  state.automationStarting = true;
  state.automationStopIntent = false;
  state.automationStopSending = false;
  state.automationStopAcknowledged = false;
  state.automationStopRetryAt = 0;
  state.automationStopFocusPending = false;
  state.automationConnectionInterrupted = false;
  navigate("applications");
  adoptAutomationResult({
    status: "RUNNING",
    invocation_id: invocationId,
    phase: "STARTING",
    stop_requested: false,
    cycles_completed: 0,
    total_jobs: 0,
    current_job_index: 0,
    message: "Starting automatic applications…",
    stages: [],
    summary: {},
  }, generation);
  let startError = null;
  try {
    let started = null;
    try {
      const response = await postJson("/api/automation-cycle/run", {
        invocation_id: invocationId,
        approve_gate_a: true,
      });
      if (generation !== state.automationGeneration) return;
      started = adoptAutomationResult(response, generation);
    } catch (error) {
      startError = error;
      if (generation !== state.automationGeneration) return;
      showAutomationConnectionIssue(
        `The start response was interrupted (${error.message}); checking server status before allowing another run.`
      );
    } finally {
      if (generation === state.automationGeneration) {
        state.automationStarting = false;
        updateRunningButtons();
      }
    }
    if (
      generation === state.automationGeneration
      && state.automationStopIntent
      && state.automating
    ) {
      await requestAutomationStop(generation);
    }
    let result = started || state.automationResult;
    if (activeAutomationStatuses.has(String(result?.status || ""))) {
      result = await waitForAutomationCompletion(generation);
    }
    if (generation !== state.automationGeneration || !result) return;
    if (String(result.status || "") === "IDLE") {
      result = adoptAutomationResult(failedAutomationStart(invocationId, startError), generation);
    } else {
      result = adoptAutomationResult(result, generation);
    }
    state.automationReconciling = true;
    updateRunningButtons();
    await loadDashboard();
    if (generation !== state.automationGeneration) return;
    showAutomationResult(result);
  } catch (error) {
    if (generation !== state.automationGeneration) return;
    if (state.automating) {
      showAutomationConnectionIssue(
        `The page lost an automation update (${error.message}); server status remains authoritative and polling will continue.`
      );
      return;
    }
    const failed = {
      status: "FAILED",
      invocation_id: state.automationInvocation || invocationId,
      message: `Automatic applications could not continue: ${error.message}`,
      stages: state.automationResult?.stages || [],
      summary: state.automationResult?.summary || {},
    };
    adoptAutomationResult(failed, generation);
    showAutomationResult(failed);
  } finally {
    if (generation === state.automationGeneration) {
      state.automationStarting = false;
      state.automationReconciling = false;
      if (!state.automating) state.automationStopping = false;
      updateRunningButtons();
    }
  }
}

async function stopAutomation() {
  if (
    (!state.automating && !state.automationStarting)
    || state.automationStopping
    || !state.automationInvocation
  ) return;
  const generation = state.automationGeneration;
  state.automationStopIntent = true;
  state.automationStopFocusPending = true;
  state.automationStopping = true;
  const stopping = {
    ...(state.automationResult || {}),
    status: "STOPPING",
    invocation_id: state.automationInvocation,
    stop_requested: true,
    message: "Stop requested. JobOps will finish the current application at a safe saved boundary.",
  };
  adoptAutomationResult(stopping, generation);
  if (state.automationStarting) return;
  await requestAutomationStop(generation);
}

function updateRunningButtons() {
  const refresh = document.querySelector("#refresh-jobs");
  if (refresh) {
    refresh.disabled = state.refreshing;
    refresh.textContent = state.refreshing ? "Refreshing…" : "Refresh job library";
  }
  const automation = document.querySelector("#run-automation");
  if (automation) {
    automation.disabled = state.automating
      || state.automationStarting
      || state.automationReconciling;
    automation.textContent = state.automationStarting
      ? "Starting automatic applications…"
      : state.automating
        ? "Automatic applications running…"
        : state.automationReconciling
          ? "Checking automation status…"
          : "Continue automatic applications";
  }
  const stop = document.querySelector("#stop-automation");
  if (stop) {
    stop.hidden = !state.automating;
    stop.disabled = state.automationStopping || state.automationStopSending;
    stop.textContent = state.automationStopping ? "Stopping safely…" : "Stop after current application";
  }
  if (document.querySelector("#next-step-action")) renderNextStep();
}

async function readAutomationStatusForReconciliation(generation) {
  let failures = 0;
  while (generation === state.automationGeneration) {
    try {
      const current = await getJson("/api/automation-cycle/status");
      if (!current || typeof current !== "object") {
        throw new Error("Automation returned an invalid status.");
      }
      if (failures) {
        showNotice(String(current.status || "") === "IDLE"
          ? "Connection restored. Automatic applications are not running."
          : "Connection restored. Automatic application status is current again.");
      }
      return current;
    } catch (error) {
      failures += 1;
      if (generation !== state.automationGeneration) return null;
      showNotice(
        `Checking automatic application status was interrupted (${error.message}); retrying before enabling Start.`
      );
      await pause(automationRetryDelay(failures));
    }
  }
  return null;
}

async function resumeAutomationIfRunning() {
  if (
    state.automating
    || state.automationStarting
    || (state.automationReconciling && state.automationGeneration > 0)
  ) return;
  const generation = ++state.automationGeneration;
  state.automationReconciling = true;
  updateRunningButtons();
  try {
    const current = await readAutomationStatusForReconciliation(generation);
    if (generation !== state.automationGeneration || !current) return;
    if (current.status === "IDLE") return;
    state.automationProgressKey = automationProgressKey(current);
    state.automationSnapshotKey = automationSnapshotKey(current);
    let result = adoptAutomationResult(current, generation);
    if (!activeAutomationStatuses.has(String(result.status || ""))) {
      showAutomationResult(result);
      return;
    }
    result = await waitForAutomationCompletion(generation);
    if (generation !== state.automationGeneration || !result) return;
    result = adoptAutomationResult(result, generation);
    await loadDashboard();
    if (generation !== state.automationGeneration) return;
    showAutomationResult(result);
  } catch (error) {
    if (generation === state.automationGeneration) {
      showNotice(`Could not read automatic application status: ${error.message}`);
    }
  } finally {
    if (generation === state.automationGeneration) {
      state.automationReconciling = false;
      if (!state.automating) state.automationStopping = false;
      updateRunningButtons();
    }
  }
}

function consumeJobClipperHandoff() {
  const prefix = "#jobops-clip=";
  if (!location.hash.startsWith(prefix)) return null;
  const encoded = location.hash.slice(prefix.length);
  history.replaceState(null, "", `${location.pathname}${location.search}`);
  if (!encoded || encoded.length > 12000 || !/^[A-Za-z0-9_-]+$/.test(encoded)) return null;
  try {
    const standard = encoded.replaceAll("-", "+").replaceAll("_", "/");
    const padded = standard + "=".repeat((4 - (standard.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    const parsedUrl = new URL(payload.page_url);
    const pageTitle = String(payload.page_title || "").trim();
    const selectedText = String(payload.selected_text || "").trim();
    if (
      !["http:", "https:"].includes(parsedUrl.protocol)
      || parsedUrl.username
      || parsedUrl.password
      || payload.page_url.length > 2048
      || !pageTitle
      || pageTitle.length > 500
      || selectedText.length > 2000
    ) return null;
    return {
      page_url: payload.page_url,
      page_title: pageTitle,
      selected_text: selectedText || null,
    };
  } catch (_) {
    return null;
  }
}

function openJobClipperDialog(payload) {
  state.pendingJobClip = payload;
  document.querySelector("#job-clipper-title").textContent = payload.page_title;
  document.querySelector("#job-clipper-url").textContent = payload.page_url;
  const selectionRow = document.querySelector("#job-clipper-selection-row");
  selectionRow.hidden = !payload.selected_text;
  document.querySelector("#job-clipper-selection").textContent = payload.selected_text || "";
  document.querySelector("#job-clipper-status").textContent = "Review the page details, then save this one page explicitly.";
  document.querySelector("#save-job-clip").disabled = false;
  document.querySelector("#job-clipper-dialog").showModal();
}

function cancelJobClip() {
  state.pendingJobClip = null;
  document.querySelector("#job-clipper-dialog").close();
}

async function saveJobClip() {
  const payload = state.pendingJobClip;
  const button = document.querySelector("#save-job-clip");
  const status = document.querySelector("#job-clipper-status");
  if (!payload) return;
  button.disabled = true;
  status.textContent = "Saving this current page…";
  try {
    const result = await postJson("/api/job-leads/capture", {
      ...payload,
      invocation_id: invocation("web-clipper"),
      user_gesture: true,
    });
    if (result.status === "FAILED") throw new Error(result.reason || "Current-page capture failed");
    state.pendingJobClip = null;
    document.querySelector("#job-clipper-dialog").close();
    await loadDashboard();
    navigate("jobs");
    if (result.lead_status === "RESOLVED") {
      showNotice("The current page was verified as an official posting and added to your job library.");
    } else {
      showNotice("The current page was saved as an unverified lead. Open it yourself and provide the final employer or ATS URL when requested.");
    }
  } catch (error) {
    status.textContent = `Could not save this page: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function performAction(action) {
  if (action === "refresh") return refreshJobs();
  if (action === "automation") return runAutomation();
  if (action) navigate(action);
}

function bindDynamicActions() {
  document.querySelectorAll("[data-reload]").forEach((node) => node.onclick = loadDashboard);
  document.querySelectorAll("[data-action]").forEach((node) => node.onclick = () => performAction(node.dataset.action));
  document.querySelectorAll("[data-open-preferences]").forEach((node) => node.onclick = openPreferenceDialog);
  document.querySelectorAll("[data-preference-editor]").forEach((node) => node.onsubmit = savePreferenceItems);
  document.querySelectorAll("[data-attention-id]").forEach((node) => node.onclick = () => openAttentionItem(node.dataset.attentionId));
  document.querySelectorAll("[data-review-plan]").forEach((node) => node.onclick = () => openSubmissionReview(node.dataset.reviewPlan));
  document.querySelectorAll("[data-review-run]").forEach((node) => node.onclick = () => openSubmissionReview(node.dataset.reviewRun, "COMPATIBILITY_RUN"));
  document.querySelectorAll("[data-lead-resolution-form]").forEach((form) => {
    form.onsubmit = (event) => {
      event.preventDefault();
      resolveJobLead(form);
    };
  });
}

function openAttentionItem(itemId) {
  const item = [...(state.attention?.user_items || []), ...(state.attention?.operator_items || [])].find((value) => value.item_id === itemId);
  if (!item) {
    showNotice("The selected attention item is no longer current.");
    return;
  }
  state.activeAttentionItem = item;
  const job = [
    ...(state.applications?.ordered_items || []),
    ...(state.jobs?.ordered_items || []),
  ].find((value) => value.job_id === item.job_id);
  document.querySelector("#attention-dialog-action").textContent = item.required_action;
  const answerLabels = {
    preferred_name: "What preferred name should this application use?",
    location: "What is your current location?",
    state: "What province or state should this application use?",
    full_time_experience: "How many years of full-time experience should be reported?",
    office_attendance: "Can you meet the stated in-office attendance requirement?",
    company_familiarity: "How familiar are you with this company or its product?",
    job_discovery_source: "How did you hear about this job?",
    work_authorization: "Are you legally authorized to work in this location?",
    work_authorization_detail: "Which work-authorization option accurately describes your status?",
    sponsorship: "Will you need employer sponsorship now or within the stated period?",
    relocation: "Are you willing to relocate if this role requires it?",
    salary: "What compensation expectation should be reported?",
    start_date: "When would you be available to start?",
    accommodation: "Do you want to request an application accommodation?",
    attestation: "Please review and personally confirm the application statement.",
    consent: "Please review and personally confirm the requested consent.",
    signature: "Please review and personally provide the required signature.",
  };
  const stageQuestions = {
    BASE_RESUME_SELECTION: "Select which approved resume this application should use.",
    BASE_LATEX_SELECTION: "Select the valid document version for this application.",
    APPLICATION_ANSWERS: "Provide the missing application answer shown above.",
  };
  document.querySelector("#attention-dialog-question").textContent = (
    answerLabels[item.canonical_answer_key]
    || stageQuestions[item.source_stage]
    || "Review the required action and provide the exact missing information."
  );
  document.querySelector("#attention-dialog-title").textContent = job
    ? `${item.attention_label}: ${job.title} at ${job.company}`
    : item.attention_label;
  document.querySelector("#attention-response").value = "";
  document.querySelector("#attention-dialog-status").textContent = "";
  const generic = ["PROVIDE_FACT", "MAKE_CHOICE", "ATTEST"].includes(item.resolution_capability);
  document.querySelector("#attention-response-area").hidden = !generic;
  const specialized = document.querySelector("#attention-specialized-message");
  const removeUnsupported = document.querySelector("#remove-unsupported-claim");
  const canRemoveUnsupported = item.resolution_capability === "CORRECT_MATERIAL";
  specialized.hidden = generic;
  specialized.textContent = generic
    ? ""
    : canRemoveUnsupported
      ? "JobOps can safely remove the unsupported statement and regenerate the bound material without adding new candidate facts."
      : "This item requires a specialized correction or replacement capability. The guided Dashboard will not fake or bypass that workflow.";
  removeUnsupported.hidden = !canRemoveUnsupported;
  removeUnsupported.disabled = false;
  document.querySelector("#attention-dialog").showModal();
}

async function removeUnsupportedClaimAndRetry() {
  const item = state.activeAttentionItem;
  const status = document.querySelector("#attention-dialog-status");
  const button = document.querySelector("#remove-unsupported-claim");
  if (!item || item.resolution_capability !== "CORRECT_MATERIAL") {
    status.textContent = "This correction is no longer available.";
    return;
  }
  button.disabled = true;
  status.textContent = "Removing the unsupported statement and regenerating the application material…";
  try {
    const result = await postJson(
      `/api/human-attention-inbox/${encodeURIComponent(item.item_id)}/correct-unsupported-claim`,
      { action: "REMOVE_UNSUPPORTED_CLAIM", instruction: null },
    );
    if (["FAILED", "INVALID_CORRECTION", "TARGET_UNAVAILABLE", "TARGET_STALE", "UNSUPPORTED_TARGET"].includes(result.status)) {
      throw new Error(result.message || "Material correction failed");
    }
    document.querySelector("#attention-dialog").close();
    await loadDashboard();
    showNotice("Unsupported statement removed; the application material was regenerated without adding new facts.", "success");
  } catch (error) {
    status.textContent = `Could not correct this material: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function closeSubmissionReview() {
  if (state.submissionInProgress) return;
  state.activeSubmissionReview = null;
  document.querySelector("#submission-review-dialog").close();
}

function setSubmissionReviewValue(id, value) {
  document.querySelector(id).textContent = value == null || value === "" ? "Not listed" : String(value);
}

async function openSubmissionReview(reviewId, reviewSource = "APPLICATION_PLAN") {
  const dialog = document.querySelector("#submission-review-dialog");
  const status = document.querySelector("#submission-review-status");
  const button = document.querySelector("#confirm-application-submission");
  state.activeSubmissionReview = null;
  state.submissionInProgress = false;
  status.textContent = "Loading the current reviewed application…";
  button.disabled = true;
  button.textContent = "Confirm and submit";
  if (!dialog.open) dialog.showModal();
  try {
    const review = reviewSource === "COMPATIBILITY_RUN"
      ? await postJson(`/api/reviewed-applications/${encodeURIComponent(reviewId)}/refresh-review`, {})
      : await getJson(`/api/application-reviews/${encodeURIComponent(reviewId)}`);
    if (review.status !== "READY" || !review.review_token) {
      throw new Error(review.message || "This review is no longer available.");
    }
    state.activeSubmissionReview = { ...review, review_source: reviewSource };
    setSubmissionReviewValue("#submission-review-company", review.company);
    setSubmissionReviewValue("#submission-review-title", review.title);
    setSubmissionReviewValue("#submission-review-location", review.location);
    setSubmissionReviewValue("#submission-review-ats", review.ats_type || review.routed_adapter);
    const uploaded = Number.isInteger(review.uploaded_file_count) ? `; ${review.uploaded_file_count} uploaded files` : "";
    setSubmissionReviewValue("#submission-review-materials", `${review.resume_included ? "Resume included" : "Resume missing"}; ${review.cover_letter_included ? "cover letter included" : "cover letter missing"}${uploaded}`);
    setSubmissionReviewValue("#submission-review-answers", `${review.prepared_answer_count} prepared answers; ${review.unresolved_control_count} unresolved controls`);
    setSubmissionReviewValue("#submission-review-time", readableDate(review.reviewed_at) || review.reviewed_at);
    setSubmissionReviewValue("#submission-review-fingerprint", review.review_fingerprint);
    status.textContent = review.message;
    button.disabled = false;
  } catch (error) {
    status.textContent = `Could not load the current review: ${error.message}`;
  }
}

async function confirmApplicationSubmission() {
  const review = state.activeSubmissionReview;
  const status = document.querySelector("#submission-review-status");
  const button = document.querySelector("#confirm-application-submission");
  if (!review || state.submissionInProgress) return;
  state.submissionInProgress = true;
  button.disabled = true;
  button.textContent = "Submitting…";
  status.textContent = "Submitting this application and verifying evidence…";
  try {
    const reviewId = review.review_source === "COMPATIBILITY_RUN"
      ? review.review_run_id
      : review.application_plan_id;
    const endpoint = review.review_source === "COMPATIBILITY_RUN"
      ? `/api/reviewed-applications/${encodeURIComponent(reviewId)}/submit`
      : `/api/application-reviews/${encodeURIComponent(reviewId)}/submit`;
    const result = await postJson(
      endpoint,
      { review_token: review.review_token, confirmed: true },
    );
    if (result.status === "SUBMITTED") {
      state.activeSubmissionReview = null;
      document.querySelector("#submission-review-dialog").close();
      await loadDashboard();
      state.applicationTab = "SUBMITTED";
      navigate("applications");
      renderApplications();
      bindDynamicActions();
      showNotice("Application submitted and verified.");
      return;
    }
    if (result.status === "SUBMISSION_UNCERTAIN") {
      state.activeSubmissionReview = null;
      document.querySelector("#submission-review-dialog").close();
      await loadDashboard();
      state.applicationTab = "SUBMISSION_UNCERTAIN";
      navigate("applications");
      renderApplications();
      bindDynamicActions();
      showNotice("Submission evidence is uncertain. JobOps will not retry this application automatically.");
      return;
    }
    if (["BLOCKED", "STALE_REVIEW"].includes(result.status)) {
      status.textContent = "The application was re-read before submission. Loading the exact current review…";
      await loadDashboard();
      await openSubmissionReview(reviewId, review.review_source);
      return;
    }
    status.textContent = result.message || "Submission stopped safely.";
  } catch (error) {
    status.textContent = `Could not submit this application: ${error.message}`;
  } finally {
    state.submissionInProgress = false;
    button.textContent = "Confirm and submit";
    if (state.activeSubmissionReview) button.disabled = false;
  }
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
    bindDynamicActions();
  }));
  document.querySelectorAll("[data-application-tab]").forEach((node) => node.addEventListener("click", () => {
    state.applicationTab = node.dataset.applicationTab;
    navigate("applications");
    renderApplications();
    bindDynamicActions();
  }));
  document.querySelector("#next-step-action").addEventListener("click", (event) => performAction(event.currentTarget.dataset.action));
  document.querySelector("#refresh-jobs").addEventListener("click", refreshJobs);
  document.querySelector("#run-automation").addEventListener("click", runAutomation);
  document.querySelector("#stop-automation").addEventListener("click", stopAutomation);
  document.querySelector("#job-search").addEventListener("input", () => {
    renderJobs();
    bindDynamicActions();
  });
  document.querySelector("#job-status-filter").addEventListener("change", () => {
    renderJobs();
    bindDynamicActions();
  });
  document.querySelector("#reset-job-finder").addEventListener("click", resetJobFinder);
  document.querySelector("#send-job-finder-message").addEventListener("click", sendJobFinderMessage);
  document.querySelector("#job-finder-input").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendJobFinderMessage();
  });
  document.querySelector("#open-preference-dialog").addEventListener("click", openPreferenceDialog);
  document.querySelector("#close-preference-dialog").addEventListener("click", () => document.querySelector("#preference-dialog").close());
  document.querySelector("#interpret-preferences").addEventListener("click", interpretPreferences);
  document.querySelector("#approve-preferences").addEventListener("click", approvePreferences);
  document.querySelector("#delete-local-data").addEventListener("click", () => {
    document.querySelector("#delete-confirmation").hidden = false;
  });
  document.querySelector("#cancel-delete").addEventListener("click", () => {
    document.querySelector("#delete-confirmation").hidden = true;
  });
  document.querySelector("#submit-attention-response").addEventListener("click", submitAttentionResponse);
  document.querySelector("#remove-unsupported-claim").addEventListener("click", removeUnsupportedClaimAndRetry);
  document.querySelector("#close-submission-review").addEventListener("click", closeSubmissionReview);
  document.querySelector("#confirm-application-submission").addEventListener("click", confirmApplicationSubmission);
  document.querySelector("#cancel-job-clip").addEventListener("click", cancelJobClip);
  document.querySelector("#save-job-clip").addEventListener("click", saveJobClip);
}

document.addEventListener("DOMContentLoaded", () => {
  const jobClip = consumeJobClipperHandoff();
  bindStaticActions();
  resetJobFinder();
  const initial = location.hash.slice(1);
  navigate(["home", "jobs", "applications", "profile", "settings"].includes(initial) ? initial : "home");
  loadDashboard().then(() => {
    if (jobClip) openJobClipperDialog(jobClip);
    return Promise.allSettled([
      resumeRefreshIfRunning(),
      resumeAutomationIfRunning(),
    ]);
  });
});
