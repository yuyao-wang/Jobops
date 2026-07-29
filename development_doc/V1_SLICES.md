# Jobops V1 Delivery Slices

V1 是覆盖六个业务域的端到端版本，不是多个独立 MVP。本文记录当前
已定义的最小可验收 Slice。

状态：

- `[完成]`：已实现并有测试证据
- `IN PROGRESS`：当前正在实现
- `[部分]`：已有基础实现，但尚未满足该 Slice 的最终边界
- `[计划]`：尚未实现
- `[实验]`：不属于当前 V1 正式支持面

## 统一边界

业务调用方只使用两个 provider-neutral 入口：

```text
async read_public_job(ReadJobRequest) -> ReadJobResult

run_discovery(JobDiscoveryRequest) -> JobDiscoveryResponse
```

第一个入口只读取并返回 `SourceJobObservation`；第二个入口才负责正式
标准化、去重、revision 和持久化。

Conversational Intake 只能依赖 `PublicJobReader Port`、`JobSearchPort`
和 `JobDiscoveryPort`，不能依赖具体 Connector 或存储实现。

## Slice 总览与依赖

```text
D1 Typed Job Discovery Entry                           [完成]

C1 Greenhouse Public Job Read                          [完成]
  ↓
C2a Separate Source Platform and ATS Type              [完成]
  ↓
C2 Lever Public Job Read                               [完成]
  ↓
C3 Generic JSON-LD Job Read                            [完成]
  ↓
I1 Conversational URL Intake                           [完成]
  ↓
I2 Conversational add/apply Resolution                 [完成]
  ↓
I2b Accepted Application Intent Persistence            [完成]
  ↓
S1a Known Greenhouse Board Candidate Search            [完成]
  ↓
S1b Conversational Named Job Search                    [完成]
  ↓
S2 Candidate Selection                                 [完成]

P1a Editable Prioritization Policy                      [完成]
  ↓
P1a2 Preparation Admission Policy Contract             [完成]
  ↓
P1b AI Priority Proposal                               [完成]
  ↓
P1b2 Single-call Real Priority Agent Adapter           [完成]
  ↓
P1c Validation Gate and PriorityDecision               [完成]
  ↓
P1d1 Single-job Priority Proposal Orchestrator         [完成]
  ↓
P1d2 Current Priority Queue Read Model                 [完成]
  ↓
P1d3 Selective Batch Reprioritization Orchestrator     [完成]
  ↓
P1d4 Runnable Application Queue Read Model             [完成]
  ↓
P2a1 Automation-first ApplicationPlan                  [完成]
  ↓
P2a2 Trusted Resume Candidate Registry                 [完成]
  ↓
P2a3 Automatic Base Resume Selection                   [完成]
  ↓
P2a4a Hash-bound Source Resume Projection              [完成]
  ↓
P2a4b Subject-specific CandidateEvidence Snapshot      [完成]
  ↓
P2a4c Evidence-bound Resume Tailoring Draft            [完成]
  ↓
P2a5 Evidence-bound Resume Fact QA                     [完成]
  ↓
P2a6a Trusted LaTeX Resume Version Registry            [完成]
  ↓
P2a6b Automatic Base LaTeX Version Selection           [完成]
  ↓
P2a6c TailoredDraft → LaTeX Version Construction       [完成]
  ↓
P2a7 Sandboxed LaTeX Compilation                       [完成]
  ↓
P2a8a Resume Visual QA                                 [完成]
  ↓
P2a8b Bounded Resume Layout Revision                   [完成]
  ↓
P2a9 Prepared Resume Material Publication              [完成]
  ↓
P2b1 Plan-scoped Material Manifest Assembly            [完成]

C3
  ↓
DOM Recipe Slice                                       [尚未定义]
  ↓
F1 Bounded Agent Extraction Fallback                   [实验 / blocked]
```

Additional dependencies:

```text
D1 ───────────────→ I2
C3 ───────────────→ S2
I1 ───────────────→ S1
PublicJobReader ──→ S2 selected-candidate reread
```

F1 cannot be scheduled while the architecture requires DOM Recipe before Agent
fallback but no DOM Recipe Slice has been defined.

## D1 — Typed Job Discovery Entry `[完成]`

```text
run_discovery(JobDiscoveryRequest) -> JobDiscoveryResponse
```

Owns validation, normalization, canonical identity, content hash, upsert,
revision, `JobPosting` and `DiscoveryRun`. It does not fetch URLs, search,
interpret conversation or execute applications.

## C1 — Greenhouse Public Job Read `[完成]`

### 独立能力

Given one supported Greenhouse public job URL, the business caller invokes only:

```text
async read_public_job(ReadJobRequest) -> ReadJobResult
```

The unified entry recognizes Greenhouse internally and returns one typed
`SourceJobObservation` or typed failure. The caller does not import or construct
`GreenhousePublicJobReader`.

### 当前已有

- Provider-neutral request, result, observation, provenance and failure types.
- Concrete read-only `GreenhousePublicJobReader`.
- Fake-HTTP fixtures and contract/boundary tests.

C1 is complete: `read_public_job(...)` is the provider-neutral façade and
Greenhouse is its only deterministic branch. The concrete reader remains a
connector-test and legacy-compatibility surface.

### 精确实现范围

- Add one static provider-neutral façade with Greenhouse as its only supported
  implementation.
- Keep the existing `ReadJobRequest`, `ReadJobResult` and
  `SourceJobObservation` contract unchanged unless an invariant defect is found.
- Reuse the existing Greenhouse URL parser, response parser and fake transport.
- Make business-facing tests call the unified entry.
- Return `UNSUPPORTED_URL` without network access for every non-Greenhouse URL.
- Keep the concrete reader available only as an implementation/unit-test detail.

### 实际文件

Production:

- Added `source_connectors/public_reader.py`.
- Modified `source_connectors/__init__.py` to export the unified entry.
- `source_connectors/greenhouse.py` was not modified.

Tests and documentation:

- Extended `tests/test_greenhouse_source_connector.py` with façade acceptance,
  routing and dependency-boundary tests using fake HTTP.
- Reused `tests/fixtures/source_connectors/greenhouse_job.json`.
- Updated `CONTRACTS_AND_TESTS.md`, `ARCHITECTURE.md`,
  `ARCHITECTURE_MAP.md` and this verified status after C1 passed.

No new framework, registry, plugin system, service, database or generic router.

### 明确不做

- Lever, JSON-LD, DOM Recipe or Agent fallback.
- Conversational Intake, search or candidate selection.
- `run_discovery()` calls or any persistence.
- Browser, ATS Adapter, application execution or Priority.
- Board-wide Greenhouse collection.

### 验收测试

1. Business code can import one provider-neutral `read_public_job` entry.
2. A supported Greenhouse URL returns `SUCCEEDED` and the existing typed
   observation.
3. The façade preserves source fields, system fields and provenance.
4. All existing Greenhouse failure and retry semantics remain unchanged.
5. Non-Greenhouse URLs return `UNSUPPORTED_URL` without HTTP.
6. The façade and reader write no Private Home, CSV, tracker, `JobPosting` or
   `DiscoveryRun`.
7. Neither layer imports `run_discovery`, ATS Adapters, browser code or models.
8. Tests inject fake HTTP; no test accesses the real network.
9. There is no platform parameter in `ReadJobRequest`.
10. A caller fake can control the unified read result without knowing a concrete
    provider.

## C2a — Separate Source Platform and ATS Type `[完成]`

`SourcePlatform` identifies the observation source; `AtsType` identifies the
application system. C2a added only `SourcePlatform.LEVER`,
`AtsType.{GREENHOUSE, LEVER, UNKNOWN}` and the distinct observation field type;
serialized values and all other reader contracts remained unchanged.

## C2 — Lever Public Job Read `[完成]`

Keep the public entry and contract unchanged. Add one deterministic Lever
translation behind it. One Slice adds only Lever URL recognition, public API
reading, field provenance and typed failures.

It does not add search, Intake, persistence, a dynamic registry or fallback.

Depends on C1.

Implemented URL forms are
`http(s)://jobs.lever.co/{company}/{job-id}` and the same path with `/apply`,
optional trailing slash, query or fragment. The reader uses the single-posting
Postings API, maps the company board token with request provenance, and returns
typed failures without fallback. C1 and C2 have 77 focused fake-HTTP tests.

## C3 — Generic JSON-LD Job Read `[完成]`

When no deterministic Connector applies, perform one bounded public page fetch
and parse `application/ld+json` whose validated type is `JobPosting`.

It adds only generic structured-data reading. It does not render JavaScript,
explore links, run a browser, call a model, select among ambiguous postings or
persist a job.

Depends on C2 and the unified Public Job Reader.

Implemented bounds: public HTTP(S) only, DNS/IP validation on the initial URL
and every redirect, at most three redirects, 10-second timeout, 2 MB response,
HTML/XHTML only, no cookies/authentication/JavaScript/browser, and exactly one
`JobPosting` across object, array or `@graph`. Known Connector failures never
fall through to JSON-LD.

## I1 — Conversational URL Intake `[完成]`

The user provides one URL. Intake extracts the URL, calls only the unified
Public Job Reader, retains the typed observation for the current interaction,
and asks the user to choose `add` or `apply`.

A bare URL has no default action. I1 does not create a proposal or call
`run_discovery()`.

Depends on C3.

Implemented as one application-service boundary: zero URLs requests more
information, multiple URLs request selection without reading, and one URL calls
only `read_public_job(...)`. Success stores a caller-TTL, process-local
`WAITING_FOR_ACTION` pending intake and returns the two typed actions
`ADD_JOB` and `REQUEST_APPLICATION`. It does not process either action.

## I2 — Conversational add/apply Resolution `[完成]`

After an explicit user choice, Intake binds that choice to the previously read
observation, creates one typed `JobIntakeProposal`, and calls the
`JobDiscoveryPort` exactly once.

`REQUEST_APPLICATION` still stops after Discovery in this domain; it does not
call Priority, `ApplicationPlan` or ATS execution here.

Depends on I1 and D1.

Implemented as `resolve_pending_intake(...)` with an injected callable
Discovery port. It validates pending ownership, expiry, state, observation and
the fixed action enum before constructing the existing D1 contracts. A
process-local lock claims the pending intake before the call. A typed response
completes it and is replayed for the same action; a different later action is a
conflict. An exception before a typed response releases the claim for explicit
retry. The exact observation, including provenance, remains attached to the
completed pending intake.

## I2b — Accepted Application Intent Persistence `[完成]`

After an accepted I2 Discovery result supplies a formal `job_id` and
`discovery_run_id`, Intake writes one immutable, subject-specific
`AcceptedJobIntent` through an injected typed repository. The explicit
`subject_id` comes from `ResolvePendingIntakeRequest`; it is never inferred
from conversation, job, filesystem or machine identity.

Records bind subject, job, `ADD_JOB` / `REQUEST_APPLICATION`, intake proposal,
Discovery run and contract version. Stable identity excludes time so an exact
I2 replay returns the existing record without another Discovery call or
timestamp. A later `REQUEST_APPLICATION` becomes current; a later `ADD_JOB`
does not cancel it. Historical jobs without records remain explicit
`NOT_FOUND`.

Discovery rejection writes nothing. If Discovery succeeds but intent storage
fails, I2 returns typed partial failure and retains enough process-local state
for an explicit persistence retry without repeating Discovery. This Slice
does not create Priority, a runnable queue, `ApplicationPlan`, submission
intent or execution authorization.

## S1a / S1b — Named Job Search `[完成]`

The user supplies company and job-title clues. Intake makes one bounded
`JobSearchPort` call and returns zero, one or multiple lightweight candidates.

It adds only search and typed candidate results. It does not persist, reread a
full job, choose among multiple candidates or loop autonomously.

Depends on I1.

## S2 — Candidate Selection `[完成]`

The user explicitly selects a candidate when selection is required. Intake
binds the selection to the candidate set, rereads the selected URL through the
unified Public Job Reader, then follows I2 into Discovery.

Search result metadata cannot bypass the full public read and cannot become a
`JobPosting` directly.

Depends on S1, C3 and I2.

## P1a — Editable Prioritization Policy `[完成]`

```text
natural-language strategy
→ injected PrioritizationPolicyInterpreterPort
→ typed, expiring PrioritizationPolicyDraft
→ user-reviewed content
→ immutable versioned PrioritizationPolicy
→ active policy in Private Home
```

P1a adds only policy interpretation, review, approval, content-hash
idempotency, version history and active-policy retrieval. Drafts are
process-local; approved policy is durable. It does not read a `JobPosting`,
produce a `PriorityProposal`/`PriorityDecision`, calculate Priority, mutate a
queue or call application code.

## P1a2 — Preparation Admission Policy Contract `[完成]`

```text
Policy Draft
→ user-reviewed preparation admission
→ immutable versioned PrioritizationPolicy snapshot
```

Every new draft contains an ordinary-code default: P0/P1/P2 are directly
eligible for Application Preparation and P3 requires a separate explicit
promotion fact. The user may edit the two disjoint typed P0–P3 sets before
approval. `NEEDS_USER` and `EXCLUDED` are never configurable as eligible.
Admission is part of canonical policy content, hash, persistence and versioning,
so a changed rule creates a new ACTIVE policy and naturally makes prior
orchestration bindings stale.

This contract expresses preparation eligibility only. It does not replace the
required `REQUEST_APPLICATION` intent, create a promotion record, build the
runnable queue, prepare materials, authorize execution or submit an
application. Persisted policies predating this contract fail closed and require
a separate explicit migration before reapproval; defaults are not injected
into old approved snapshots. P1a2 does not implement that migration.

## P1b — AI Priority Proposal `[完成]`

```text
approved policy + JobPosting + verified CandidateSummary + explicit now
→ deterministic PriorityContext
→ one injected PriorityAgentPort call
→ ordinary-code binding / evidence / invariant validation
→ typed PriorityProposal
```

P1b defines the minimal CandidateSummary snapshot contract rather than guessing
a mapping from private vault values without fact IDs/categories. It computes
job age and posted-at state without scores, binds adapter-owned model metadata,
and validates every evidence reference. The proposal evaluates soft
preferences and trade-offs but remains untrusted advice. It is not persisted
and cannot create a decision, mutate a queue or start downstream work. A real
model adapter is not part of this Slice.

## P1b2 — Single-call Real Priority Agent Adapter `[完成]`

```text
PriorityContext
→ tool-free OpenAI Responses API adapter
→ one strict JSON Schema response
→ existing P1b evidence / qualification validation
→ PriorityProposal
```

P1b2 reuses the existing `OpenAIAPIBackend`; system rules and untrusted
PriorityContext data are separate input messages, no tools/functions are
registered, and there is no automatic retry or model switching. Adapter-owned
agent/prompt/model metadata cannot come from model output. Routine tests use a
fake provider client; an opt-in synthetic smoke script is the only live-call
path. P1b2 does not create a `PriorityDecision`, persist a Proposal, or enter
application work.

## P1c — Validation Gate and PriorityDecision `[完成]`

```text
PriorityProposal + current job / policy / candidate bindings
→ deterministic approved hard-constraint evaluation
→ Agent/Gate finding reconciliation
→ immutable priority-gate-v2 PriorityDecision
→ atomic, idempotent Private Home persistence
```

The Gate never calls the Agent. Clear deterministic results override the
Proposal, unresolved constraints become `NEEDS_USER`, and an unsupported Agent
exclusion conflicts instead of being guessed into P0–P3. The Decision binds
job revision/hash, policy ID/version/hash, candidate summary version/hash,
source Proposal ID/hash and agent/prompt/model versions. It creates neither an
active-decision pointer nor queue state. No fixed global score maps to P0–P3.
Proposal and Decision also retain explicit evidence coverage for work
authorization, citizenship/residency, student status and security clearance.
Student-only eligibility lowers priority or requests confirmation by default;
only an approved policy hard constraint can make it `EXCLUDED`.

## P1d1 — Single-job Priority Proposal Orchestrator `[完成]`

```text
explicit subject_id + persisted job_id + explicit now
→ typed current JobPosting read
→ ACTIVE PrioritizationPolicy lookup
→ trusted CandidateSummary projection
→ stable pre-Agent input-binding claim
→ create_priority_proposal() exactly once
→ finalize_priority_proposal() exactly once
→ completed Proposal / Decision reference
```

`orchestrate_single_job_priority(SingleJobPriorityCommand, ...)` is the bounded
application entry point for one persisted job. The input binding includes the
job revision/hash, policy ID/version/hash, CandidateSummary version/hash,
Agent/prompt/model metadata, explicit evaluation time, Gate version and
orchestration version.

The binding claim is stored atomically before the Agent call. A completed
identical binding returns `UNCHANGED` with its existing typed Proposal and
Decision and performs no Agent call, Gate execution or Decision write. An
in-progress or failed matching binding returns a typed incomplete result rather
than silently rerunning the Agent.

The production CandidateSummary provider accepts only explicit, verified,
prioritization-safe `prioritization_facts` from the current CandidateVault
`facts.json`; it does not read legacy `profile.yaml` or infer facts from other
profile fields. P1d1 does not batch jobs, create a current-decision index,
order a queue or start application work.

## P1d2 — Current Priority Queue Read Model `[完成]`

```text
explicit subject_id + explicit now
→ typed current JobPosting list
→ ACTIVE PrioritizationPolicy + trusted CandidateSummary
→ P1d1 expected binding for every job
→ read existing orchestration history
→ CURRENT / STALE / MISSING / INCOMPLETE
```

`build_current_priority_queue(CurrentPriorityQueueCommand, ...)` is a typed,
read-only application entry. It reuses P1d1's complete input-binding contract
and reads only existing orchestration, Proposal and Decision records. It never
claims a binding, calls the Priority Agent, creates a Proposal, executes the
Gate or writes a Decision.

`CURRENT` requires an exact completed binding and returns its existing typed
Proposal and Decision. `STALE` reports only binding differences that can be
proved from job, policy, CandidateSummary, Agent/prompt/model, evaluation-time,
Gate or orchestration version fields; historical artifacts never occupy the
current Proposal/Decision fields. `MISSING` means no completed history exists,
while an exact non-completed lifecycle is `INCOMPLETE`.

The view groups `CURRENT`, `STALE`, `MISSING`, then `INCOMPLETE`. Current items
use the persisted Decision only: P0, P1, P2, P3, NEEDS_USER, EXCLUDED; ties use
`validated_at` and `job_id`. This is not the runnable application queue.

## P1d3 — Selective Batch Reprioritization Orchestrator `[完成]`

```text
bounded subject_id + now + optional job allowlist / max_jobs
→ P1d2 queue snapshot exactly once
→ select only STALE / MISSING
→ call P1d1 once per selected job, serially
→ typed CREATED / UNCHANGED / skipped / failed aggregation
```

`selectively_reprioritize_jobs(SelectiveBatchReprioritizationCommand, ...)`
depends only on injected P1d2 and P1d1 application callables. Explicit job IDs
preserve caller order after deterministic first-occurrence deduplication;
`max_jobs` truncates that allowlist. Without a non-empty allowlist, `max_jobs`
is required and selection follows P1d2 order.

`CURRENT` and `INCOMPLETE` are returned as typed skips. An explicit job ID not
present in the snapshot is `NOT_FOUND`. Only `STALE` and `MISSING` invoke P1d1.
Calls are serial, use the exact supplied `now`, have no automatic retry and
continue after a typed per-job failure. P1d3 has no batch store or second
idempotency model: repeated work is prevented by a fresh P1d2 read and P1d1's
existing atomic binding claim.

The result distinguishes `NOOP`, `COMPLETED`, `PARTIAL_FAILURE` and `FAILED`
and preserves every typed P1d1 result. P1d3 does not calculate bindings, call
the Agent/Proposal/Gate directly, save artifacts or create an application
queue.

## P1d4 — Runnable Application Queue Read Model `[完成]`

```text
P1d2 typed snapshot + its exact ACTIVE policy
+ subject-specific accepted intent
→ deterministic preparation-admission projection
→ RUNNABLE / typed blocked items
```

`build_runnable_application_queue(RunnableApplicationQueueCommand, ...)` calls
P1d2 exactly once and consumes the exact `policy_snapshot` returned by that
read. It never performs a second ACTIVE-policy lookup. A job is `RUNNABLE` only
when it is CURRENT, has a current qualified Decision, has an authoritative
`REQUEST_APPLICATION` intent, is directly admitted by the snapshot's
`PreparationAdmissionPolicy`, and remains in an available V1 lifecycle state.

STALE, MISSING and INCOMPLETE are `BLOCKED_NOT_CURRENT`; NEEDS_USER and EXCLUDED
remain explicit blocks. An admission priority requiring promotion is
`BLOCKED_PROMOTION_REQUIRED`, while an unadmitted priority is
`BLOCKED_PRIORITY`. ADD_JOB and a verified absence of intent are both
`BLOCKED_NO_APPLICATION_INTENT`; intent integrity failure fails the whole read
model. Output preserves P1d2 order and performs no claim, write,
reprioritization, preparation or execution.

## P1d — Reprioritization and Queue `PLANNED`

```text
policy, job revision, candidate version or time changes
→ reprioritization
→ queue ordering
```

P1d2 projects current/stale/missing/incomplete state, P1d3 can explicitly
recompute a bounded subset of stale/missing jobs, and P1d4 exposes the
read-only admission view for Application Preparation. Active-decision pointers,
automatic scheduling, promotion records and preparation execution remain
planned. Reprioritization does not rewrite historical decisions; P1d1 creates
decisions with new immutable bindings.

## P2a1 — Automation-first ApplicationPlan `[完成]`

```text
subject_id + selected RUNNABLE job + optional job-scoped instructions
→ P1d4 snapshot exactly once
→ immutable ApplicationPlan
→ atomic Private Home persistence
```

`create_application_plan(CreateApplicationPlanCommand, ...)` creates a plan
only from the selected P1d4 `RUNNABLE` item. It does not read Priority, policy
or accepted-intent repositories directly. The plan binds job revision/content,
formal Decision, exact policy version/hash, accepted REQUEST_APPLICATION
intent, priority, contract version and the exact user-instruction hash.

The plan fixes `AUTOMATION_FIRST` and `DEFER_ITEM_AND_CONTINUE`: later
preparation should complete safe work asynchronously and defer only the current
job when human attention is genuinely required. Its declared stages are Resume
Preparation, Cover Letter, Application Answers, Fact QA, Visual QA, Material
Assembly and Gate A. P2a1 does not execute any stage or create a Human Attention
Queue.

Plan identity excludes creation time. An identical binding returns the existing
immutable record with `UNCHANGED` and preserves its original `created_at`;
changed job, Decision, policy, intent or exact user instructions creates a new
plan. A plan authorizes preparation only, never browser execution or submission.

## P2a2 — Trusted Resume Candidate Registry `[完成]`

```text
explicit subject + managed resume artifact + trusted safe summary
→ validate actual PDF/DOCX bytes
→ compute artifact and summary hashes
→ subject-scoped immutable ResumeCandidate
→ typed get / list_selectable
```

`register_resume_candidate(RegisterResumeCandidateCommand, ...)` accepts only
an explicitly named artifact below Private Home `documents/master/`; it never
scans that directory or imports loose `default_resume`, `resume_variants` or
fallback paths. The artifact is copied to a subject-scoped preparation path,
and every read verifies its actual bytes against the persisted hash.

Candidate identity binds subject, artifact hash/type, display name, exact
selection-safe summary and its authenticated trust metadata, status and
contract version. Time is excluded, so identical replay returns `UNCHANGED`;
summary, artifact or contract content changes cannot overwrite the old record.
`list_selectable(subject_id)` is stable and fails closed if any record or
artifact for that subject is corrupt.

P2a2 performs no resume selection, JD read, Agent/model call, tailoring,
material preparation, browser or ATS operation.

## P2a3 — Automatic Base Resume Selection `[完成]`

```text
ApplicationPlan
→ exact typed JobPosting revision/hash check
→ subject-scoped ResumeCandidateProvider
→ 0 defer / 1 deterministic / many one bounded Agent call
→ immutable ResumeSelectionDecision
```

`select_base_resume(SelectBaseResumeCommand, ...)` never calls P1d1–P1d4 or
rejudges runnable state. It loads the persisted plan, verifies explicit subject
ownership and fail-closes unless the current typed JobPosting ID, revision and
content hash exactly match the plan.

Candidate inputs come only from P2a2. Zero candidates return
`DEFERRED_NO_RESUME`; one candidate requires no Agent. Multiple candidates
permit one tool-free Agent call containing trusted JD data, selection-safe
candidate projections and exact plan-scoped instructions. The returned resume
ID, candidate contract version and artifact hash are revalidated; refusal,
ambiguity or mismatch returns `DEFERRED_NEEDS_HUMAN` without retry or Decision.

The pre-Agent selection binding covers Plan/Job bindings, candidate-set hash,
selection contract and configured Agent/prompt/model versions. An existing
completed binding returns `UNCHANGED`, preserves `selected_at` and makes zero
additional Agent calls. Decisions are immutable, subject-scoped and
content-hash validated. Selection neither changes resume bytes nor implies
tailoring, material approval, Human Attention state, execution or submission.

## P2a4a — Hash-bound Source Resume Projection `[完成]`

```text
subject_id + resume_id
→ typed ResumeCandidate read and managed-byte hash verification
→ deterministic PDF-line or DOCX-structure parsing
→ immutable SourceResumeProjection
```

`create_source_resume_projection(CreateSourceResumeProjectionCommand, ...)`
reads only the explicitly registered P2a2 candidate and re-hashes the managed
artifact before parsing. PDF uses the existing `pdfplumber` dependency and
stores one normalized source line per page/line locator. DOCX uses standard
ZIP/XML parsing and stores body paragraph or table/row/cell/paragraph
locators. Heading and list recognition are deterministic and parser-versioned;
unrecognized content remains an ordinary paragraph or table-cell block.

Section, block and bullet IDs bind artifact hash, exact structural locator,
projection contract and parser version. Projection identity additionally binds
subject and resume ownership. Time, path and mtime are excluded. Identical
replay returns the immutable record with `UNCHANGED`; artifact or parser
version changes create a distinct record without overwriting history.

Image-only or encrypted PDFs are `UNSUPPORTED`; damaged or structurally
unreadable documents are `UNREADABLE`. The Slice performs no OCR, inference,
CandidateEvidence generation, tailoring, rendering, Agent/model call, browser
or ATS work.

## P2a4b — Subject-specific CandidateEvidence Snapshot `[完成]`

```text
subject_id + ApplicationPlan
→ latest immutable ResumeSelectionDecision for the Plan
→ selected ResumeCandidate
→ latest immutable SourceResumeProjection for the artifact
→ CandidateEvidenceSnapshot
```

`create_candidate_evidence_snapshot(...)` validates the complete
Plan/Selection/Candidate/Projection binding and converts each non-empty source
block into one ordered evidence item without summarizing or inferring facts.
Items preserve exact text, section/block/bullet IDs and typed source locators.
They are conservatively classified as `PERSONAL`, scoped only to
`RESUME_TAILORING`, and marked
`USER_PROVIDED_DOCUMENT_STATEMENT` rather than independently verified.

Evidence IDs bind subject, projection ID/hash, source block or bullet anchor
and evidence contract version. Snapshot identity additionally binds the Plan,
Selection, source resume/artifact and ordered item hashes. Runtime timestamps,
paths and mtime do not create identities. Identical input returns the existing
immutable snapshot with `UNCHANGED`; an empty compatible projection returns
`DEFERRED_NO_EVIDENCE`.

The read path chooses the latest complete immutable Selection for a Plan and
the latest complete immutable Projection for the selected artifact by domain
timestamp with stable ID tie-break. Any corrupt record fails the complete
read. P2a4b reads no CandidateSummary, CandidateVault profile, JD or legacy
profile and performs no Agent/model, tailoring, QA, rendering or execution.

## P2a4c — Evidence-bound Resume Tailoring Draft `[完成]`

```text
ApplicationPlan + SelectionDecision + JobPosting
+ SourceResumeProjection + CandidateEvidenceSnapshot
→ ResumeTailoringAgentPort (at most once per new binding)
→ deterministic validation
→ TailoredResumeDraft
```

`tailor_resume(TailorResumeCommand, ...)` validates the complete
Plan/Job/Selection/Candidate/Projection/EvidenceSnapshot binding and fails
closed on any subject, job revision, artifact hash or association mismatch
before the Agent is reached. The Agent receives only the trusted typed JD,
the SourceResumeProjection, `RESUME_TAILORING`-scoped evidence from the bound
snapshot, the Plan's verbatim user preparation instructions and the static
versioned Agent policy. The policy fixes
`Action Verb + Details + Outcome = Skill Statement`, bans weak verbs and
fabrication, and fixes instruction priority as facts > user instructions >
JD alignment > default style. User instructions never modify the policy.

The Agent must return a typed structured result covering every source block
exactly once with change type `UNCHANGED | REWRITTEN | REORDERED | OMITTED`.
Deterministic validation verifies: evidence IDs exist in the bound snapshot
with a permitted scope; source section/block/bullet references exist and
match; JD alignment references are verbatim substrings of the JD; new
numbers and proper-noun tokens in rewritten bullets appear in cited
evidence; JD leading verbs are used only with evidence support; weak leading
verbs are rejected; source text quoted in user instructions cannot be
`OMITTED`. `UNCHANGED`/`REORDERED` bullets must equal the source text.

The Agent reporting insufficient evidence returns
`DEFERRED_INSUFFICIENT_EVIDENCE`; any illegal, unknown-reference or
unverifiable output returns `DEFERRED_NEEDS_HUMAN` without auto-retry.
Both defer only the current job and create no draft.

Draft identity binds Plan, Selection, Job revision/content hash, artifact,
Projection, EvidenceSnapshot hash, user instruction hash and
Agent/prompt/model/policy/contract versions; time is excluded. A completed
binding replays as `UNCHANGED` with zero Agent calls; changed inputs create
a distinct immutable draft without overwriting history. The Slice performs
no final rendering, Fact QA, Visual QA, cover letter, application answers,
Human Attention Queue, batch, browser or ATS work.

## P2a5 — Evidence-bound Resume Fact QA `[完成]`

```text
TailoredResumeDraft + CandidateEvidenceSnapshot + SourceResumeProjection
→ deterministic checks
→ bounded ResumeFactQAAgentPort (only when semantic judgment is needed)
→ ResumeFactQAResult
```

`run_resume_fact_qa(RunResumeFactQACommand, ...)` is an independent fact gate.
It never treats a draft as trustworthy because P2a4c accepted it: every
checkable fact is re-derived here, and the module deliberately shares no
validator with the tailoring Slice. A subject, plan, job revision, artifact,
projection, evidence or content-hash mismatch returns
`BLOCKED_BINDING_MISMATCH` with zero Agent calls and zero writes.

Deterministic checks run first and cover reference existence, evidence scope,
source coverage and duplication, verbatim `UNCHANGED`/`REORDERED` text, at
least one usable evidence reference per rewritten bullet, and every number,
date, company, title, degree and tool name appearing in cited evidence. Any
blocking deterministic finding returns `BLOCKED_UNSUPPORTED_CLAIM` without
calling the Agent. A JD alignment reference missing from the bound job
description is recorded as an `ADVISORY` finding: it is a provenance defect,
not a false claim about the candidate, so it does not block.

Only when the deterministic pass is clean and rewritten bullets exist is the
bounded Agent called, at most once. It receives only the rewritten bullets
and the tailoring-scoped evidence — no JD, no projection, no profile — and
may judge only evidence support: unsupported action verbs, overstated
ownership, overstated maturity, unsupported impact, unsupported causality
and out-of-scope claims. It returns findings and a verdict; it cannot edit
the draft, propose replacement text or call tools. Ordinary code revalidates
that every Agent finding references a reviewed bullet and in-scope evidence.

Verdicts are `PASSED`, `BLOCKED` or `DEFERRED`. An unknown reference, an
illegal or contradictory output, or an `UNCERTAIN` verdict returns
`DEFERRED_NEEDS_HUMAN` without auto-retry, records why, and pauses only the
current job. QA identity binds the draft ID and content hash, projection,
evidence snapshot and QA/Agent/prompt/model/policy versions; time is
excluded, so a completed binding replays as `UNCHANGED` with zero Agent calls
regardless of verdict. `PASSED` covers facts only — not layout, visual
quality, material approval or submission authority. The Slice never modifies
the draft, repairs claims, renders documents, or touches Visual QA, the
browser, an ATS or the Application Engine.

## P2a6a — Trusted LaTeX Resume Version Registry `[完成]`

```text
explicitly supplied .tex source
→ deterministic capability validation + SHA-256 over actual bytes
→ managed subject-isolated artifact
→ immutable ResumeLatexVersion with lineage
```

`register_resume_latex_version(RegisterResumeLatexVersionCommand, ...)`
accepts one explicitly supplied UTF-8 LaTeX source — either inline text or a
`.tex` path already inside Private Home — and never scans a directory or
imports a file on its own. Bytes are copied into
`state/preparation/resume-latex-versions/sources/<subject-key>/<sha256>.tex`,
so the original input path is irrelevant afterwards. The SHA-256 is always
computed from the managed bytes; no caller-declared hash is trusted.

Registration performs a minimal deterministic capability scan and rejects
shell escape (`\write18`, `\ShellEscape`), external program execution
(`shellesc`, `\directlua`), file writes (`\openout`, `\newwrite`), file reads
(`\openin`, `\newread`) and absolute or home-relative include paths. This is
an admission check, not compile safety: sandboxed compilation remains P2a7's
responsibility. Relative includes stay legal.

Many versions and many root families may be valid at once. There is no unique
`current_resume.tex` and no single ACTIVE version. Nothing is ever
overwritten: an AI revision or template derivation registers a new version
and records `parent_version_id`. A parent must exist under the same subject,
and the child inherits its `root_family_id`; a supplied family that
contradicts the parent fails closed. A first parentless version derives a new
stable family deterministically from its own binding, never from a filename
or a timestamp. User-provided, imported, template-derived and AI-generated or
AI-revised sources share one registry while keeping distinct source kinds.

Version identity binds subject, managed source reference and hash, source
kind, parent, root family, optional template, source resume, draft and
fact-QA bindings, normalized labels and contract version; time is excluded,
so identical input replays as `UNCHANGED` without duplicating the artifact or
record. A different content under the same identity is an integrity conflict
that never overwrites history. `list_selectable()` returns typed versions
sorted by version ID, independent of path, filename or mtime, and an empty
registry is a normal `SUCCEEDED` result rather than a deferral.

The Slice performs no version selection, no draft-to-template mapping, no
compilation or PDF generation, no Visual QA, no chat command parsing, and no
Agent, browser, ATS or Application Engine call.

## P2a6b — Automatic Base LaTeX Version Selection `[完成]`

```text
ApplicationPlan + PASSED FactQA + TailoredResumeDraft + JobPosting
+ selectable LaTeX versions
→ deterministic selection ladder
→ bounded BaseLatexSelectionAgentPort (only on a genuine tie)
→ BaseLatexSelectionDecision
```

`select_base_latex_version(SelectBaseLatexVersionCommand, ...)` picks the
LaTeX version that P2a6c should build on. It first revalidates the whole
Plan/FactQA/Draft/Selection/Job binding, and admits only a fact-QA result
that names this draft, matches its content hash and carries verdict
`PASSED`; `BLOCKED` and `DEFERRED` never reach LaTeX selection.

Candidates come only from `ResumeLatexVersionProvider.list_selectable()`.
Any candidate declaring fact-QA provenance has that record re-read, hash
compared and verdict confirmed `PASSED`; corrupt provenance fails closed.
Only version metadata is used — the Slice never opens a `.tex` file, and the
Agent context carries no source reference at all.

The deterministic ladder resolves most cases with zero Agent calls: an
explicit version or family requirement found as a literal ID in the plan's
user instructions wins first; no candidate at all yields
`MANAGED_TEMPLATE_FALLBACK`, which is a normal outcome rather than a
deferral; a single candidate is `ONLY_CANDIDATE`; and a unique version bound
to the current source resume is `EXACT_SOURCE_RESUME_MATCH`. Nothing is ever
chosen by recency or filename.

Only a genuine remaining tie calls the bounded Agent, at most once, over the
trusted JD, the plan's verbatim user instructions and restricted version
metadata. The Agent may name one candidate, ask for the managed template, or
ask for a human. An unknown ID, an illegal structure or a human request falls
back to the managed template rather than interrupting the user — unless the
plan carried an explicit version or family requirement that cannot then be
satisfied, in which case the item defers as `DEFERRED_NEEDS_HUMAN`.

`MANAGED_TEMPLATE_FALLBACK` only records that P2a6c should use the managed
default template; no template file is chosen or implemented here. Decision
identity binds plan, draft ID and hash, passed fact-QA ID and hash, job
revision and hash, source resume, candidate-set hash and
Agent/prompt/model/contract versions, excluding time, so a completed binding
replays as `UNCHANGED` with zero Agent calls and a changed candidate set
creates a new immutable decision. Selection implies nothing about whether
LaTeX exists, compiles, passes Visual QA or may be submitted.

## P2a6c — TailoredDraft → LaTeX Version Construction `[完成]`

```text
PASSED TailoredResumeDraft + BaseLatexSelectionDecision
→ controlled LaTeX construction
→ immutable ResumeLatexVersion

EXISTING_VERSION          → AI_REVISED child version
MANAGED_TEMPLATE_FALLBACK → SYSTEM_TEMPLATE_DERIVED root version
```

`construct_resume_latex_version(ConstructResumeLatexCommand, ...)` writes the
Draft into the layout P2a6b already chose. It revalidates the whole
Plan/Draft/FactQA/BaseSelection binding, admits only a `PASSED` fact-QA
result matching this draft's content hash, and never re-selects a version:
the decision's `selection_kind` is obeyed exactly.

Content is addressed through a controlled marker contract —
`\JobopsSection{section_id}{title}` and `\JobopsBullet{bullet_id}{text}`
inside a single `%% JOBOPS-CONTENT-BEGIN` / `%% JOBOPS-CONTENT-END` region.
Historical versions supply layout only. Every visible candidate statement
comes from the current Draft: each Draft section and each retained bullet
appears exactly once, with text byte-identical to the Draft after
single-pass LaTeX escaping, and omitted bullets are dropped entirely.

Three construction methods, two of which never call a model. The managed
fallback renders through one built-in template,
`managed-resume-one-page-v1`; there is no template catalogue, recommendation
or selection. A historical version that already carries the controlled
region is derived by replacing that region while every other byte of the
layout survives. Only a historical version without the region reaches the
bounded Agent, at most once, and it receives the base LaTeX text, the Draft,
the plan's user instructions, the marker contract and a static policy — no
repository, tool, compiler or evidence.

Every path is validated deterministically before anything is registered:
UTF-8, document structure, the P2a6a capability scan, exactly one controlled
region, no markers outside it, no duplicate or unknown marker, no missing
Draft content, exact escaped text, and a stale-content check that rejects the
base version's long visible-text runs when they are not current Draft
content. Any violation defers as `DEFERRED_NEEDS_HUMAN` without auto-retry.
An unreadable, drifted or missing base source defers as
`DEFERRED_SOURCE_UNREADABLE`, and no other historical version is substituted.

The existing-version path registers `AI_REVISED` with the selected version as
parent, inheriting its root family; the fallback path registers
`SYSTEM_TEMPLATE_DERIVED` with no parent, a new stable family and the
template ID and hash. Both record the Draft and passed fact-QA bindings.
Construction identity binds plan, draft, passed fact-QA, base selection,
parent or template, user instructions and Agent/prompt/model/contract
versions, excluding time, and lives in a P2a6c-owned construction record so
P2a6a's registry identity and lineage semantics are untouched. A completed
binding replays as `UNCHANGED` with zero Agent calls and no duplicate source
artifact. Producing `.tex` implies nothing about compiling, fitting one
page, passing Visual QA or being authorized to submit.

## P2a7 — Sandboxed LaTeX Compilation `[完成]`

```text
ResumeLatexConstructionRecord + ResumeLatexVersion
→ sandboxed compiler
→ validated PDF
→ ResumeCompilationRecord
```

`compile_resume_latex(CompileResumeLatexCommand, ...)` turns a P2a6c-produced
version into a managed PDF. It first revalidates the construction record
against the version — subject, version ID, source hash, family, lineage,
template and Draft/FactQA provenance — then re-reads the managed source,
re-hashes the actual bytes and re-runs the P2a6a capability scan. Every one
of those checks happens before the compiler is reachable.

Only one allowlisted engine exists in V1. `LatexCompilerPort` separates a
cheap `describe()`, which supplies engine, version and normalized flags for
the binding, from `compile()`, the single side-effecting call — so a replay
resolves to `UNCHANGED` without starting a compiler. Execution uses
`shell=False`, a fixed argument vector, a fresh temporary directory as cwd, a
minimal deterministic environment (stable locale, UTC, `SOURCE_DATE_EPOCH`,
sandbox-local `HOME` and `TEXMF*`, `shell_escape=f`, `openout_any=p`), a
wall-clock timeout, POSIX resource limits, and stdout/stderr written to
capped files inside the sandbox. Flags fix no shell escape, non-interactive,
halt on error, file/line diagnostics and a bounded output directory. The
sandbox is removed afterwards and the engine runs at most once.

Deferrals keep the item moving without touching the source. A source that
pulls in files the registry does not manage returns
`DEFERRED_SOURCE_INCOMPLETE` — nothing is scanned, downloaded or rewritten.
A missing engine returns `DEFERRED_COMPILER_UNAVAILABLE`. Ordinary LaTeX
errors, timeouts and a success exit without a usable PDF return
`DEFERRED_COMPILATION_ERROR` with bounded diagnostics whose absolute paths,
home directory and sandbox location are redacted.

A PDF is accepted only after deterministic validation: correct signature,
non-empty, within the size cap, no symlink, inside the sandbox, and at least
one page. Page count is parsed with the existing pdfplumber dependency
rather than scanned from raw bytes, because a real engine compresses page
objects. The count is recorded and never enforced: whether the resume stays
on one page belongs to P2a8. Accepted bytes are copied into a
subject-isolated managed directory and hashed from the stored bytes;
`.aux`, `.log` and `.fls` never leave the sandbox, only a bounded diagnostic
summary does.

The compilation binding covers construction record ID and binding, version ID
and source hash, engine, compiler version, normalized flags and the
compile/sandbox policy versions, excluding time. A completed binding replays
as `UNCHANGED` with zero compiler runs, no duplicate PDF and the original
`compiled_at`; any change creates a new immutable record. A successful
compile proves only that a structurally valid PDF exists — not that the
content was re-checked, the layout is sound, it fits one page, or that
anything may be approved or submitted.

## P2a8a — Resume Visual QA `[完成]`

```text
ResumeCompilationRecord + managed PDF
→ deterministic PDF checks
→ bounded page rendering
→ bounded ResumeVisualQAAgentPort
→ ResumeVisualQAResult
```

`review_resume_visual_qa(ReviewResumeVisualQACommand, ...)` inspects one
compiled PDF and reports. It never edits LaTeX, the PDF or the Draft, never
recompiles, and never asks an Agent for a patch. It first revalidates the
Compilation, LaTeX version, Construction record and Draft binding, then
re-reads the managed PDF and re-verifies its hash, signature and page count
against the compilation record.

Page expectations come from the versioned `ResumeVisualQAPolicy`, never from
free text. No typed layout policy existed before this Slice, so it defines
the minimal safe default — `resume-visual-qa-policy-v1` with `max_pages=1`,
a minimum font size, a margin tolerance and a minimum text length per page.
Typed parsing of natural-language layout requests is out of scope.

Deterministic checks run first, over the pdfplumber projection the earlier
Slices already rely on: page count against policy, blank or near-empty
pages, page dimensions, characters whose boxes fall outside the page, the
smallest glyph size, and whether every retained Draft section title and
bullet is recognisable in the PDF text. A blocking deterministic finding
returns `REVISION_REQUIRED` immediately, with zero renders and zero Agent
calls.

Only when those checks are clean are pages rendered, through
`PdfPageRendererPort` at a fixed DPI in stable page order; the local adapter
uses the pypdfium2 dependency pdfplumber already brings in. A renderer that
cannot describe itself, cannot render, or returns pages out of order defers
as `DEFERRED_RENDERER_UNAVAILABLE` without calling the Agent. `describe()`
is cheap and feeds the binding, so a replay renders nothing.

The bounded Agent is called at most once and receives only page images with
their pixel dimensions, the deterministic findings, the policy and a static
Agent policy — no repository, path, PDF bytes, LaTeX or credential. It may
judge only overlap, unreadably small type, crowding, unexplained whitespace,
inconsistent alignment, glyph corruption and broken hierarchy. It returns
findings and a verdict; severity is derived from the finding type by
ordinary code, so an Agent cannot downgrade a defect. Every finding must
name a supplied page, and a bounding box must lie inside that page.

`PASSED` requires no blocking finding from either source, a satisfied page
policy and every page checked; advisory findings alone never block.
`REVISION_REQUIRED` says P2a8b may attempt an automatic fix — it is not an
immediate request for a human. An unknown page, an out-of-page box, an
illegal structure or an uncertain verdict returns `DEFERRED_NEEDS_HUMAN`
without auto-retry, pausing only this job.

The binding covers compilation record and binding, PDF hash, LaTeX version
and source hash, Draft ID and hash, renderer name/version/DPI, the whole
policy and the Agent/prompt/model versions, excluding time. A completed
binding replays as `UNCHANGED` with no re-render and no Agent call; any
change creates a new immutable result. `PASSED` states only that this PDF
looks sound — not that Gate A, an ATS or submission is authorized.

## P2a8b — Bounded Resume Layout Revision Orchestrator `[完成]`

```text
VisualQA REVISION_REQUIRED
→ bounded layout revision
→ P2a7 compile
→ P2a8a review
→ PASSED or defer
```

`revise_resume_layout(ReviseResumeLayoutCommand, ...)` fixes typography, and
only typography. It never touches the Draft, the evidence, the fact-QA
result, or a single word of resume text.

The initial verdict decides everything. `PASSED` returns `NOT_REQUIRED` with
no Agent, render or compile. `DEFERRED` returns `DEFERRED_NEEDS_HUMAN`. Only
`REVISION_REQUIRED` starts the loop, after the whole
VisualQA/Compilation/Version/Provenance/Draft/Plan binding is revalidated.

Attempts are serial and bounded by `ResumeLayoutRevisionPolicy`, whose V1
maximum is three. Each attempt reads the current managed source, renders the
current PDF, calls the Revision Agent at most once, validates the output,
registers an immutable `AI_REVISED` child, invokes P2a7 once and P2a8a once,
and stops the moment visual QA passes.

The Agent sees the current LaTeX source, the current page images, the
current findings, both policies and the plan's verbatim user instructions —
nothing else. It may adjust margins, spacing, leading, font size within
range, header spacing, alignment and existing safe macros. Deterministic
validation then proves the controlled content region is byte-identical, the
markers and their IDs and order are unchanged, the capability scan still
passes, no new file dependency appeared, font sizes and margins stay inside
policy, and no hiding trick — white text, transparency, clipping, phantom
content, off-page shifting, zero-size boxes or `\tiny` — was introduced. A
rejected output defers for a human; safety rules are never relaxed to make
an attempt succeed.

Compilation and visual QA are reached only through injected steps bound to
the P2a7 and P2a8a public entry points, so no sandbox or QA logic is
duplicated. P2a7 deferring or failing stops the run rather than blindly
revising again. P2a8a deferring returns `DEFERRED_NEEDS_HUMAN`. Exhausting
the attempt budget returns `DEFERRED_ATTEMPTS_EXHAUSTED` with the full
attempt lineage preserved, pausing only this job.

Every revision creates a new immutable version whose parent is the previous
attempt's version, inheriting the same root family, plus a
`ResumeLayoutRevisionRecord` that P2a7 and P2a8a accept as build provenance
through the shared `LatexBuildProvenance` protocol — a minimal,
backward-compatible extension that leaves construction records unchanged.
Run identity binds the initial visual QA ID and hash, the initial version
and source hash, the Draft, both policies, renderer metadata and the
Agent/prompt/model versions, excluding time; replay returns `UNCHANGED` with
zero Agent, render, compile and QA calls. This Slice never solves a page
overflow by shortening or rewriting content — when typography alone cannot
satisfy the policy, it defers.

## P2a9 — Prepared Resume Material Publication `[完成]`

```text
ApplicationPlan + final PASSED Visual QA
→ validated managed PDF
→ PreparedResumeMaterial
```

`publish_prepared_resume(PublishPreparedResumeCommand, ...)` declares which
compiled PDF is the prepared resume for one ApplicationPlan. It records an
already managed artifact; it never copies, regenerates, recompiles, renders
or modifies anything upstream, and it calls no Agent.

Exactly one source must be supplied. The direct path takes a Visual QA
result ID. The revision path takes a P2a8b run ID, requires that run to have
ended in a passing visual QA, and resolves the run's own final Visual QA,
compilation and LaTeX version — the run's final lineage must agree with that
QA result or the item is not ready.

The whole chain is then revalidated: plan subject ownership, the QA verdict,
the compilation binding and PDF hash against the QA result, the LaTeX
version's source hash and Draft binding, the Draft against the plan's job
revision and content hash, and the fact-QA result named by that exact LaTeX
version, which must cover this precise Draft and carry verdict `PASSED`.

Two distinct outcomes keep a stalled resume from ever being published.
`NOT_READY` covers work that is simply not finished or not approved — visual
QA not passed, an unsuccessful or exhausted revision run, fact QA not
passed, and any cross-chain binding mismatch. `FAILED` covers structural
problems — a missing or corrupt record, a subject mismatch, or a managed PDF
that is unreadable, hash-drifted, wrongly sized or of a different page count
than its compilation record. Neither ever writes a material, and neither
falls back to an older compilation, a historical PDF or the source
ResumeCandidate.

Before publishing, the managed PDF is re-read from its subject-isolated
location, re-hashed against the compilation record, checked for a valid
signature and exact byte size, and its page count re-parsed. The published
record points at that same artifact reference.

Publication identity binds the plan and job revision, the Draft, the passed
fact QA, the final LaTeX version and source hash, the compilation and its
PDF hash, the final passed Visual QA, and the optional successful revision
run, excluding time. Replay returns `UNCHANGED` with the original
`published_at` and no duplicate record or artifact; any changed link in the
chain creates a new immutable material. `find_current_for_plan()` resolves
by stored publication time with a stable material-ID tie-break, never by
directory order or mtime.

A published resume means the content passed fact QA, the compiled PDF passed
visual QA, and the artifact is ready for downstream material assembly. It
does not mean a cover letter or answers exist, that Approval Gate A passed,
or that submission or ATS execution is authorized.

## P2b1 — Plan-scoped Material Manifest Assembly `[完成]`

```text
ApplicationPlan + PreparedResumeMaterial
→ Plan-scoped Material Manifest (RESUME entry)
```

`assemble_plan_material_manifest(AssemblePlanMaterialManifestCommand, ...)`
declares one published resume as a plan's formal RESUME material. It
assembles finished work only: it generates nothing, calls no Agent, and
re-runs no tailoring, fact QA, compilation or visual QA.

This is a new contract, deliberately separate from the legacy
`MaterialManifest` in `core/materials.py`. That one is tier and
job-directory centric and keeps its behaviour untouched; `PlanMaterialManifest`
is bound to an immutable ApplicationPlan and references only records the
preparation chain already published. The two names, modules and stores never
overlap, and this Slice changes no legacy execution semantics.

Assembly validates plan subject ownership, then the published material's
subject, plan, job ID, revision and content hash, its RESUME role, and the
presence of its complete draft, fact-QA, LaTeX, compilation and visual-QA
provenance. The managed PDF is then re-read and re-verified — existence, no
symlink, SHA-256, `%PDF-` signature, exact byte size and re-parsed page
count. The manifest references that artifact; it never copies, moves,
regenerates or modifies it, and never falls back to a legacy job directory,
a historical PDF or the source ResumeCandidate.

Completeness is modelled explicitly rather than as one ambiguous flag. The
manifest stores `included_roles` and an `assembly_state` of `RESUME_ONLY`,
and exposes `resume_prepared` separately from
`complete_application_material_prepared`, which is False while cover letters
and application answers remain later Slices. Approval Gate A is not
represented by this contract at all. Missing materials are simply absent: no
placeholder file and no fake entry is ever created, and each material role
may appear at most once, in deterministic entry order.

Manifest identity binds the plan and its job revision and content hash, the
prepared material ID and a hash of its own content, the PDF artifact hash,
the ordered entry hashes and the contract version, excluding time. Replay
returns `UNCHANGED` with the original `assembled_at`; any changed plan,
material, artifact or contract version creates a new immutable manifest.
`find_current_for_plan()` resolves by stored assembly time with a stable
manifest-ID tie-break, never by directory order or mtime. An unresolvable or
mismatched prepared resume returns `NOT_READY` and pauses only this job.

## F1 — Bounded Agent Extraction Fallback `[实验 / blocked]`

F1 is not a free browsing Agent. It may perform at most:

```text
one bounded page read
→ one structured extraction
→ optional one supplemental read
→ stop
```

Allowed output fields:

```text
company
title
description
location
source_url
application_url
posted_at
ats_type
```

Ordinary code must validate the result before it becomes a
`SourceJobObservation`. The Agent cannot save a job, apply, log in, bypass
CAPTCHA, search indefinitely, choose arbitrary tools or guess missing fields.

F1 depends on C1–C3 and on a product decision defining the missing DOM Recipe
Slice and cross-reader escalation matrix. It is not part of the current V1
formal support surface.

## Legacy reuse and isolation

Potential narrow reuse:

- Greenhouse and Lever endpoint/field knowledge from `utils/discovery.py`.
- Sanitized URL extraction cases from `utils/url_resolver.py`.
- Existing `httpx.MockTransport` testing approach.

Must remain isolated:

- `discover_all_jobs()` and its legacy `Job` model;
- keyword filtering and title/company fuzzy deduplication;
- Dashboard/Scheduler direct `utils.tracker` writes;
- `utils/career_page_source.py` Playwright + Claude extraction;
- browser redirect/click exploration and static company-to-ATS routing;
- ATS Adapters and application execution;
- exception swallowing that turns source failure into an empty success.

## Pending product decisions

1. Whether JavaScript-rendered JSON-LD remains unsupported or is deferred to
   DOM Recipe.
2. The missing DOM Recipe Slice scope and whether it is required before F1.
3. Whether F1 belongs to a later release rather than V1.
4. Conversation-state lifetime and how an add/apply choice binds to the exact
   observation.
5. Search provider/allowlist, uniqueness threshold, result lifetime and
   candidate-selection binding.
6. Whether one high-confidence search result may proceed without explicit
    candidate selection.
7. Canonical-source rules when `source_url` and `application_url` differ.
8. The coordinator that receives `REQUEST_APPLICATION` after Discovery.
