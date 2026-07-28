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
S1a Known Greenhouse Board Candidate Search            [完成]
  ↓
S1b Conversational Named Job Search                    [完成]
  ↓
S2 Candidate Selection                                 [完成]

P1a Editable Prioritization Policy                      [完成]
  ↓
P1b AI Priority Proposal                               [完成]
  ↓
P1b2 Single-call Real Priority Agent Adapter           [完成]
  ↓
P1c Validation Gate and PriorityDecision               [完成]
  ↓
P1d1 Single-job Priority Proposal Orchestrator         [完成]
  ↓
P1d Reprioritization and Queue                         PLANNED

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

## P1d — Reprioritization and Queue `PLANNED`

```text
policy, job revision, candidate version or time changes
→ reprioritization
→ queue ordering
```

P1d does not rewrite historical decisions; it creates decisions with new
bindings and projects the current queue.

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
