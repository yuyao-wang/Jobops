# Jobops Contracts and Tests

This document is the authority for component contracts and implementation evidence. Domain rules are defined in `DOMAIN_AND_RULES.md`; this file does not redefine them.

## 契约

### Contract status

| Contract | Status |
|---|---|
| `SemanticMapper.map_controls()` | Implemented as an in-process provider-neutral Protocol |
| `AdapterRegistry.run()` / deterministic ATS lifecycle | Implemented |
| `run_discovery(JobDiscoveryRequest)` | Implemented for typed conversational proposals and Private Home upsert |
| `read_public_job(ReadJobRequest)` | Implemented provider-neutral entry with Greenhouse, Lever and bounded Generic JSON-LD branches |
| `handle_conversational_url_intake(ConversationalIntakeRequest)` | Implemented I1 single-URL read ending at `WAITING_FOR_ACTION` |
| `handle_conversational_intake(ConversationalIntakeRequest)` | Implemented S1b URL-first routing and named-job search ending at candidate selection |
| `select_search_candidate(CandidateSelectionRequest)` | Implemented S2 candidate read ending at existing `WAITING_FOR_ACTION` state |
| `resolve_pending_intake(ResolvePendingIntakeRequest)` | Implemented I2 add/apply resolution through an injected callable Discovery port |
| `search_jobs(JobSearchRequest)` | Implemented S1a provider-neutral entry for configured Greenhouse boards |
| `GreenhousePublicJobReader.read_job()` | Implemented connector detail retained for tests and legacy compatibility |
| `LeverPublicJobReader.read_job()` | Implemented internal connector detail |
| `SourceJobObservation` source/ATS identity | Implemented with distinct `SourcePlatform` and `AtsType` enums |
| `PrioritizationPolicyInterpreterPort` | P1a injected contract; fake implementation only |
| `PrioritizationPolicyDraft` / `PrioritizationPolicy` | Implemented P1a with review-gated approval and Private Home version history |
| `PriorityAgentPort` / `PriorityProposal` | Implemented P1b as one injected, tool-free call followed by ordinary-code validation |
| `finalize_priority_proposal()` / `PriorityDecision` | Implemented P1c with deterministic hard-constraint reconciliation and immutable Private Home persistence |
| `orchestrate_single_job_priority()` | Implemented P1d1 for one persisted job with pre-Agent input-binding idempotency |
| `ResumeSelector.select()` | Target V1 business port |
| Semantic Mapper HTTP API | Proposed transport only; no HTTP service is implemented |

Machine-readable contracts:

- [`contracts/openapi.yaml`](contracts/openapi.yaml) — proposed Semantic Mapper transport.
- [`contracts/job-posting.schema.json`](contracts/job-posting.schema.json) — normalized `JobPosting`.
- [`contracts/priority-proposal.schema.json`](contracts/priority-proposal.schema.json) — non-executable AI priority recommendation.
- [`contracts/priority-decision.schema.json`](contracts/priority-decision.schema.json) — versioned priority result.
- [`contracts/mapping-result.schema.json`](contracts/mapping-result.schema.json) — mapper result and allowed key/status pairs.

Python dataclasses and Protocols remain authoritative for the current in-process runtime. A machine schema becomes runtime-authoritative only when its boundary is implemented and covered by equivalence tests.

### Core interfaces

#### `handle_conversational_intake()`

```text
async handle_conversational_intake(
    ConversationalIntakeRequest,
    *,
    clue_extractor: NamedJobClueExtractor,
    job_search_port: JobSearchPort,
    candidate_store,
    pending_store,
) -> ConversationalIntakeResponse | NamedJobSearchResponse
```

URL-bearing messages retain the I1 path and never invoke named search. A
URL-free message is converted by the bounded extractor Port into company,
title, optional location and an `ADD_JOB`, `REQUEST_APPLICATION` or
`UNSPECIFIED` hint. Valid clues produce exactly one existing
`search_jobs(...)` call. Missing or invalid clues do not search.

Zero candidates returns `NEEDS_USER / NO_CANDIDATES` without selection state.
One or more candidates retain their returned order in process-local,
caller-TTL `WAITING_FOR_CANDIDATE_SELECTION` state, bound to the conversation,
original request and intent hint. S1b does not select, read details, create an
I1 `PendingIntake`, persist, call Discovery, or start application work.

#### `select_search_candidate()`

```text
async select_search_candidate(
    CandidateSelectionRequest,
    *,
    candidate_store,
    pending_store,
    reader: PublicJobReader,
) -> ConversationalIntakeResponse
```

The request contains only conversation, CandidateSet and candidate IDs. The
store validates ownership, expiry, state, membership and source URL, then
atomically claims one candidate before exactly one provider-neutral read.
Success creates the existing `WAITING_FOR_ACTION` PendingIntake with the full
observation, original intent hint and source candidate IDs.

The CandidateSet moves `WAITING_FOR_CANDIDATE_SELECTION → RESOLVING_CANDIDATE
→ COMPLETED`. Completion retains the selected candidate, pending ID and typed
read result. Repeating the same completed selection replays that result without
another read; a different or concurrent selection conflicts. Typed failures or
pre-result exceptions release the claim for explicit retry and create no
PendingIntake. S2 does not search, call Discovery/I2, persist or execute.

#### `handle_conversational_url_intake()`

```text
async handle_conversational_url_intake(
    ConversationalIntakeRequest,
    *,
    pending_store,
) -> ConversationalIntakeResponse
```

The request contains `conversation_id` and one natural-language message. Zero
extracted URLs returns `NEEDS_MORE_INFORMATION`; multiple URLs returns
`NEEDS_USER_SELECTION` without reading; exactly one calls only
`read_public_job(...)`. A successful read creates process-local,
caller-TTL `WAITING_FOR_ACTION` state containing the conversation ID and exact
`SourceJobObservation`, then returns its pending ID, summary and only
`ADD_JOB` / `REQUEST_APPLICATION`. I1 creates no proposal, `JobPosting` or
durable state and does not call `run_discovery()`.

#### `resolve_pending_intake()`

```text
resolve_pending_intake(
    ResolvePendingIntakeRequest,
    *,
    pending_store,
    discovery_port,
) -> ResolvePendingIntakeResponse
```

The request contains only `conversation_id`, `pending_intake_id` and one fixed
`ADD_JOB` / `REQUEST_APPLICATION` action. The handler validates ownership,
expiry, state and the retained observation, then maps that observation without
new facts into the existing `ResolvedJobCandidate`, `JobIntakeProposal` and
`JobDiscoveryRequest` contracts. Stable proposal/request IDs derive from the
pending ID.

The store atomically marks the item `RESOLVING` before one port call. A typed
Discovery response is retained with a `COMPLETED` item; the same action replays
that result and a different action fails without another call. An exception
before a typed response restores `WAITING_FOR_ACTION` and returns a retryable
`DISCOVERY_TEMPORARILY_UNAVAILABLE`. `REQUEST_APPLICATION` records intent only;
it does not start Priority, materials, `ApplicationPlan`, browser or ATS work.

#### `run_discovery()`

```text
run_discovery(JobDiscoveryRequest) -> JobDiscoveryResponse
```

The current trigger is only `CONVERSATIONAL`. The request contains one explicit `JobIntakeProposal`; no intent is inferred. `RESOLVED` may create, update, or leave unchanged one schema-compatible `JobPosting`. `INCOMPLETE` and `AMBIGUOUS` return `NEEDS_CLARIFICATION` without persistence. `UNSUPPORTED` and invalid formal requests persist a failed `DiscoveryRun`.

Canonical URL identity owns cross-run upsert. Tracking parameters do not create a second posting. `REQUEST_APPLICATION` is returned unchanged and has no application or ATS execution capability. The entry does not call models, fetch URLs, search jobs, invoke legacy collectors, or expose repository formats to callers.

#### `read_public_job()` / `SourceJobReader.read_job()`

```text
async read_public_job(ReadJobRequest) -> ReadJobResult
async read_job(ReadJobRequest) -> ReadJobResult
```

New business callers use `read_public_job(...)`. It recognizes Greenhouse or
Lever from the URL and delegates to the matching internal reader. Only otherwise
unknown URLs enter the Generic JSON-LD reader. The Greenhouse branch accepts
exactly these hosted-job path forms, with US or EU hosts:

```text
http(s)://boards.greenhouse.io/{board_token}/jobs/{numeric_job_id}
http(s)://job-boards.greenhouse.io/{board_token}/jobs/{numeric_job_id}
http(s)://boards.eu.greenhouse.io/{board_token}/jobs/{numeric_job_id}
http(s)://job-boards.eu.greenhouse.io/{board_token}/jobs/{numeric_job_id}
```

The Lever branch accepts:

```text
http(s)://jobs.lever.co/{company_token}/{job_id}
http(s)://jobs.lever.co/{company_token}/{job_id}/apply
```

Both forms allow an optional trailing slash, query or fragment and normalize to
the hosted job URL without `/apply`, query or fragment. Each reader performs one
unauthenticated `GET` to its corresponding single-job public endpoint. A
successful result contains one provider-neutral
`SourceJobObservation`: source and ATS identity, source/application URLs,
company, title, plain-text description, optional location and publication time,
`UNKNOWN` work mode, observation time, and field provenance.

The Generic branch performs a bounded public HTML/XHTML fetch and accepts
exactly one Schema.org `JobPosting` across one object, an array or `@graph`.
It uses `source_platform=GENERIC_WEB` and `ats_type=UNKNOWN`. The initial URL,
DNS results and every redirect target must be public; unsafe targets return
non-retryable `UNSAFE_URL` before the rejected request. Fetching is limited to
three redirects, 10 seconds and 2 MB, without cookies, authentication,
JavaScript, browser execution or link exploration.

`source_platform` identifies where the observation was read; `ats_type`
identifies the application system. They use distinct enums even when both
serialize to `"GREENHOUSE"`. Current contract values are
`SourcePlatform.{GREENHOUSE, LEVER, GENERIC_WEB}` and
`AtsType.{GREENHOUSE, LEVER, UNKNOWN}`.

`UNSAFE_URL` means a structurally valid HTTP(S) URL was rejected by public
network policy before accessing the initial target or a redirect target. It is
always a non-retryable `FAILED` result. `INVALID_URL` remains a syntax/protocol
failure; `UNSUPPORTED_URL` remains a safe URL outside current reader capability.

The reader is read-only. It cannot persist a `JobPosting` or `DiscoveryRun`, call
`run_discovery()`, access Private Home, invoke an ATS Adapter or model, or fall
back to a browser. `SourceJobObservation` is external evidence and is not a
durable `JobPosting`.

#### `search_jobs()`

```text
async search_jobs(
    JobSearchRequest,
    *,
    port: JobSearchPort,
) -> JobSearchResult
```

`JobSearchRequest` contains only `request_id`, company, title and optional
location. It contains no board token, source selector, URL, `JobPosting` or
natural-language message. Search and single-URL read have separate result and
reason types.

Invariants:

- `SUCCEEDED` always contains a `CandidateSet`, including a valid empty set;
- `FAILED` / `UNSUPPORTED` never contain a candidate set;
- `UNSUPPORTED_COMPANY` is non-retryable and sends no request;
- timeout, rate limit and 5xx unavailability are retryable; invalid input or
  response is not;
- S1a resolves only normalized exact canonical names or explicit aliases from
  an injected Greenhouse allowlist;
- title matching is exact-first, then normalized contiguous phrase; optional
  location uses normalized containment;
- results are stably ordered, capped at 10 and never auto-selected;
- one board listing GET returns summaries only; search does not read details,
  persist, call Discovery, invoke models, browser or ATS execution.

#### Editable prioritization policy

```text
create_policy_draft(CreatePolicyDraftRequest) -> CreatePolicyDraftResult
approve_policy(ApprovePolicyRequest) -> PrioritizationPolicyResult
get_active_policy(subject_id) -> PrioritizationPolicy | null
```

`PrioritizationPolicyInterpreterPort` performs one bounded transformation from
natural-language policy text to typed interpretation data. It cannot persist,
approve, read jobs, search, call Discovery or create Priority output. Ordinary
code validates the allowlisted hard-constraint types, soft-preference
categories/importances, subject binding and ambiguity status before retaining a
process-local TTL draft.

Approval accepts user-reviewed content, requires every hard constraint to be
explicitly user-confirmed and rejects unresolved ambiguity, mismatch, expiry or
already-consumed conflicting state. An approved `PrioritizationPolicy` is an
immutable Private Home snapshot. Its canonical content hash includes only raw
policy text and approved hard/soft items; draft ID, time and interpreter
metadata do not affect it. Equal active content is idempotent; changed content
increments the subject-local policy version and supersedes the prior active
version without deleting history.

#### Priority proposal and planned decision contracts

```text
PrioritizationPolicyDraft
    AI interpretation; editable, expiring and not effective

PrioritizationPolicy
    user-approved immutable policy snapshot

create_priority_proposal(
    JobPosting + active PrioritizationPolicy
    + verified CandidateSummary + explicit now,
    injected PriorityAgentPort,
    adapter-owned metadata,
) -> CreatePriorityProposalResult

ValidationGate.validate(
    PriorityProposal,
    approved policy,
    candidate summary,
    deterministic facts,
) -> PriorityDecision
```

P1b constructs `PriorityContext` in ordinary code, including job age and
`KNOWN / UNKNOWN / FUTURE` posted-at state. Candidate facts require explicit
verified and prioritization-safe provenance; the current Slice defines this
snapshot contract but does not guess a projection from private vault values
that lack category and stable fact-ID metadata.

The Port has one method and no tool surface. Its typed output is accepted only
after binding, evidence-reference and qualification-invariant validation.
Adapter-owned agent/prompt/model metadata cannot come from the model payload.
`PriorityProposal` is AI advice and cannot trigger application work or
persistence. `PriorityDecision` is the formal result and binds job ID, job
revision/content hash, policy ID/version, candidate summary version and
agent/prompt/model versions plus `validated_at`. P0–P3 do not depend on a fixed
global score.

Both `PriorityProposal` and `PriorityDecision` require one finding for each
eligibility category: work authorization, citizenship/permanent residency,
student status and security clearance. Applicable findings cite the JD; a
definitive satisfied/not-satisfied result also cites a verified CandidateFact.
Student-status mismatch or uncertainty cannot have `NONE` impact: it lowers
priority or requires the user. Exclusion is valid only through a matched,
approved `EXCLUDED_STUDENT_ONLY_ROLE` hard constraint. A citizenship/PR
preference alone is not treated as an absolute exclusion.

P1c implements the formal boundary as
`finalize_priority_proposal(FinalizePriorityProposalRequest)`. It revalidates
all current bindings and Proposal evidence/invariants, deterministically
evaluates only approved hard constraints, and reconciles clear, Agent-evidence
and unresolved findings. The immutable Decision binds the source Proposal
content hash and `priority-gate-v2`; atomic Private Home storage is idempotent
by stable Decision ID. It does not create a current pointer or queue index.

Agent tests inject a fake `PriorityAgentPort`; model output must schema validate.
Ordinary code owns hard-constraint regression tests and rejects unsupported
candidate facts/evidence. JD prompt-injection text cannot change the Agent's
allowed behavior. A policy-version change makes an old decision historical,
never current. Neither proposal nor decision may call ATS, browser or
Application Preparation.

#### Single-job priority orchestration

```text
orchestrate_single_job_priority(
    SingleJobPriorityCommand(subject_id, job_id, now),
    typed JobPosting read repository,
    ACTIVE policy provider,
    CandidateSummaryProvider,
    orchestration repository,
    PriorityDecision repository,
    PriorityAgentPort,
    adapter metadata,
) -> SingleJobPriorityResult
```

`PrivateHomeJobPostingRepository.get(job_id)` reuses Discovery's canonical
persisted `JobPosting` validation and returns a typed object or `null`.
`PrivateHomeCandidateSummaryProvider.get_current(subject_id, now=...)` reads
only explicitly typed `prioritization_facts` from the current CandidateVault
`facts.json`, then delegates trust filtering and hashing to
`build_candidate_summary()`. Missing subject binding or typed facts is a
provider failure; there is no `profile.yaml` or guessed-fact fallback.

Before P1b is called, P1d1 creates a stable input binding over subject, job
revision/hash, policy ID/version/hash, CandidateSummary version/hash,
Agent/prompt/model metadata, explicit evaluation time, Gate version and
orchestration version. The claim is an atomic Private Home create under
`state/prioritization/orchestrations/`. A completed match returns
`UNCHANGED`; an incomplete/failed match is not treated as completed and does
not trigger an implicit Agent retry. Only a newly acquired binding may call
`create_priority_proposal()` once and `finalize_priority_proposal()` once.

#### `ResumeSelector.select()`

```text
select(ApplicationPlan, tuple[ResumeVersion, ...]) -> ResumeSelection
```

Only approved, unexpired, hash-valid, evidence-current variants are eligible. The result contains the selected resume ID/revision and deterministic reason codes; it never returns altered document bytes. No eligible resume produces `MATERIALS_REQUIRED`.

#### `SemanticMapper.map_controls()`

```python
async map_controls(
    requests: tuple[MappingRequest, ...],
) -> tuple[MappingResponse, ...]
```

`MappingRequest` contains only:

```text
index, role, tag, type, label, name, aria_label,
placeholder, autocomplete, required=true, options
```

Contract invariants:

- batch size is `1..40`; request indices are unique and non-negative;
- each request has at least one semantic descriptor;
- selectors, element IDs, URLs, page/company/job identity, field values, credentials, prompts, and tools are absent;
- known private values are redacted before serialization;
- responses may be a subset; omitted controls remain unresolved;
- response indices are unique and must have been requested;
- any invalid response rejects the entire batch.

The only valid response pairs are:

| `canonical_key` | `status` | Runtime effect |
|---|---|---|
| `email` | `mapped` | local resolver may fetch verified email |
| `phone_number` | `mapped` | local resolver may fetch verified phone |
| `work_authorization` | `needs_review` | human handoff; no value is fetched |
| `unknown` | `unsupported` | leave unresolved |

The mapper has no CandidateVault, browser, filesystem, tool, state-mutation, permit, or submission capability. Provider choice is hidden behind the Protocol, so Jobops does not depend on a concrete model provider. `FakeSemanticMapper` makes results and failures controllable in tests.

Value-free input is a required boundary, not proof that arbitrary page labels contain no private text. Current projection removes field values and redacts candidate values already known locally. Before any remote production provider is enabled, the caller/service must also reject detected private/secret patterns and retain the residual-risk handoff path.

#### ATS execution

```text
AdapterRegistry.run(AdapterRunRequest) -> ApplicationOutcome
ATSAdapter.run(ApplicationContext) -> ApplicationOutcome
```

Implemented production routing is:

```text
jobctl
→ JobApplicationEngine
→ AdapterRegistry
→ deterministic ATS adapter or bounded Generic Adapter
→ ApplicationOutcome
```

`ApplicationExecutionService` is the target business façade around this existing route.

Legacy boolean `apply_*` façades are compatibility surfaces, not the authoritative contract.

Required lifecycle:

```text
route → inspect → fill → validate → persist Review
→ Gate B → reserve submission intent → one click → verify evidence
```

Every adapter returns the exact `ApplicationOutcome`; blockers cannot be collapsed to a boolean. `SUBMITTED_VERIFIED` requires an eligible `EvidenceRef`.

### Error and outcome semantics

| Status / code | Meaning | Automatic retry |
|---|---|---|
| `INVALID_INPUT` / schema error | Caller or implementation violated a contract | No |
| `MATERIALS_REQUIRED` | Valid material package is absent or stale | No |
| `AWAITING_GATE_A`, `AWAITING_GATE_B` | Required approval is absent | No |
| `NEEDS_USER_*` | Human action or verified answer is required | No |
| `FAILED_RETRYABLE` | Engine-normalized safe transient failure with no unresolved intent | Only under domain retry rules |
| `FAILED_UNSUPPORTED` | ATS/control/path is outside capability | No |
| `SKIPPED_POLICY` | Policy intentionally prevented action | No |
| `SUBMIT_UNKNOWN` | Submission may have occurred | Never |
| `FAILED_TERMINAL`, `INTERNAL_ERROR` | Non-recoverable or invariant failure | No automatic retry |
| `SUBMITTED_VERIFIED` | Eligible confirmation evidence exists | Never submit again |

For the proposed mapper HTTP transport, safe error codes are `INVALID_REQUEST`, `REQUEST_TOO_LARGE`, `VALUE_FREE_POLICY_VIOLATION`, `AUTHENTICATION_FAILED`, `NOT_AUTHORIZED`, `BUDGET_EXCEEDED`, `RATE_LIMITED`, `INVALID_MODEL_OUTPUT`, `PROVIDER_UNAVAILABLE`, `MODEL_TIMEOUT`, and `INTERNAL_ERROR`. Provider exception text is never returned or persisted.

Raw adapter outcomes are not a retry authority. `JobApplicationEngine` converts any non-verified result after intent reservation to `SUBMIT_UNKNOWN`; callers must use the engine-normalized outcome rather than retrying `AdapterRegistry.run()` directly.

### Idempotency

| Operation | Key / binding | Rule |
|---|---|---|
| Discovery upsert | source identity + canonical URL/content identity | repeated observations update one posting |
| Approved prioritization policy | subject + canonical approved content hash | equal active content returns the active version; changed content appends a version |
| Priority decision | job revision/hash + policy ID/version + candidate summary + agent/prompt/model versions | old bindings never masquerade as current |
| Resume selection | plan revision + eligible resume revisions + selector version | pure selection |
| Semantic mapping | application run + durable semantic-attempt reservation | target: at most one dispatch per run; request ID is correlation, not idempotency |
| Fill / Review | run + material/answer hashes + review hash | resumable only from a persisted safe checkpoint |
| Submit | application key + review/material/answer/policy hashes | one signed permit, one intent reservation, one click |

If dispatch or submission may already have occurred, repetition is prohibited unless durable state proves otherwise.

Current gap: the Generic Adapter enforces one semantic call only in memory per adapter invocation. A persistent reservation is required before a remote production mapper or cross-invocation resume is enabled.

### Timeout and retry ownership

| Boundary | V1 budget | Owner |
|---|---:|---|
| Source connector | 30 s per source | discovery caller |
| Provider-neutral Greenhouse single-job read | 10 s per request | Greenhouse reader |
| Generic JSON-LD public read | 10 s per request, max 3 redirects and 2 MB | Generic reader |
| Policy interpretation | caller-bounded single dispatch per draft request | policy application service |
| Priority Agent | caller-bounded single proposal per job/policy binding | prioritization caller |
| Resume selection | local operation; no internal retry | preparation caller |
| Semantic Mapper | target: 20 s end-to-end, one dispatch per run; persistent enforcement pending | execution caller |
| ATS navigation | 30 s per navigation | adapter context |
| Submit confirmation | adapter-bounded observation; never repeated by clicking again | execution service |

Timeout does not imply retryability. A Semantic Mapper timeout degrades to unresolved-control handoff. A timeout after submission intent or click becomes `SUBMIT_UNKNOWN`.

### Public source-reader failure matrix

| Result | Reason | Retry |
|---|---|---|
| `UNSUPPORTED` | `UNSUPPORTED_URL` | No |
| `FAILED` | `INVALID_URL`, `UNSAFE_URL`, `JOB_NOT_FOUND`, `JOB_CLOSED` | No |
| `FAILED` | `SOURCE_RESPONSE_INVALID` | No |
| `FAILED` | `SOURCE_TIMEOUT`, `SOURCE_RATE_LIMITED` | Yes |
| `FAILED` | `SOURCE_UNAVAILABLE` | Yes for transport/5xx; no for non-rate-limited 4xx |

No failure returns an empty successful observation.

### Semantic Mapper failure matrix

| Failure | Dispatch state | Jobops action | Retry |
|---|---|---|---|
| Invalid batch/schema/key/status/index | not dispatched or discarded | internal contract blocker | No |
| Private/secret-like value detected | not dispatched | security blocker | No |
| Mapper disabled/unavailable | not dispatched/unknown | local rules continue; unresolved required control hands off | No |
| Provider timeout or transport loss | unknown | discard result; handoff | No |
| Invalid provider output | completed | reject whole batch; handoff | No |
| Valid `needs_review` / `unsupported` / omission | completed | leave unresolved; handoff when required | No |
| Valid `mapped` | completed | local value lookup, structural checks, fill, and read-back | N/A |

There is no fallback to a tool-enabled Agent, guessed value, weaker validator, or alternate provider.

### Event format

Current append-only `EventRecord` envelope:

```text
sequence: integer
event_id: UUID/string
run_id: string
job_id: string
event_type: stable uppercase string
payload: privacy-safe object
created_at: RFC 3339 UTC timestamp
```

Writers use compare-and-swap state transitions where a run state is changed. Events may contain hashes, IDs, reason codes, counts, and redacted metadata; they must not contain candidate values, secrets, cookies, raw prompts/model output, private paths, or unredacted browser content.

Current gap: `EventLedger.append_event()` does not yet validate `event_type` or payload privacy. Until an event schema and enforcement test exist, uppercase event names and payload restrictions are writer obligations rather than ledger guarantees.

## 测试与能力矩阵

### Current capability

These are sanitized fixture results, not live-site reliability claims.

| Capability | Current status | Test evidence |
|---|---|---|
| Greenhouse deterministic path | Fixture-supported | 1 Review-arrival acceptance case plus shared contract cases |
| Lever deterministic path | Fixture-supported | 1 Review-arrival acceptance case plus shared contract cases |
| Ashby deterministic path | Fixture-supported | 1 Review-arrival acceptance case plus shared contract cases |
| Jobvite deterministic path | Fixture-supported | 1 Review-arrival acceptance case plus shared contract cases |
| Workday multi-stage path | Fixture-supported | 1 full sanitized FSM Review-arrival case plus Workday contract suite |
| Generic Adapter | Bounded fallback | unit/fixture coverage; no live reliability claim |
| Semantic Mapper | Local contract staged | invariant tests with controllable fake; no provider or HTTP implementation |
| Typed Job Discovery entry | Implemented Slice 1 | contract, schema-equivalence, upsert, run-persistence and dependency-boundary tests |
| Provider-neutral Greenhouse public job read | Implemented C1 + C2a/C3a contract coverage | 41 focused fake-HTTP/fixture contract, routing, serialization, failure and boundary cases; no live-network claim |
| Provider-neutral Lever public job read | Implemented C2 | 37 focused fake-HTTP/fixture mapping, routing, failure and boundary cases; no live-network claim |
| Generic JSON-LD public job read | Implemented C3 | 45 focused fake-HTTP/fixture parsing, SSRF, redirect, size, failure and boundary cases; no live-network claim |
| Conversational URL intake and action resolution | Implemented I1 + I2 | 29 fake-reader/store/Discovery-port extraction, conversion, atomic consumption, replay, failure and dependency-boundary cases; no network claim |
| Known Greenhouse board candidate search | Implemented S1a | 43 fake-HTTP/fixture contract, allowlist, matching, ordering, failure and dependency-boundary cases; no live-network claim |
| Conversational named-job search | Implemented S1b application boundary | 13 fake-extractor/search-port tests for URL priority, clues, 0/1/many results, TTL, failures and side-effect boundaries |
| Candidate selection to pending action | Implemented S2 | 13 fake-reader/store cases for validation, atomic claim, replay/conflict, failure release and dependency boundaries |
| Conversational/remaining connector Job Discovery | Partial | URL read, named search, candidate selection and add/apply entry exist; Lever search, SearchProfile and product-surface wiring remain |
| Editable Prioritization Policy | Implemented P1a | 20 fake-interpreter, draft-validation, approval, version/hash, Private Home and dependency-boundary cases |
| AI Priority Proposal | Implemented P1b | 35 synthetic-context/fake-agent binding, deterministic-fact, evidence, invariant, failure and dependency-boundary cases; no real-model claim |
| Single-call Priority Agent adapter | Implemented P1b2 | fake-provider and mocked-Responses tests prove one tool-free strict-schema call, system/data separation, metadata ownership, sanitized logging and existing P1b validation; optional synthetic smoke script is excluded from routine tests |
| Priority Validation Gate / formal P0–P3 decision | Implemented P1c | 35 synthetic Gate/repository cases within the 70-case Priority suite; deterministic constraints, reconciliation, binding, schema, immutability and idempotency evidence |
| Single-job Priority orchestration | Implemented P1d1 | 15 synthetic orchestration/read/provider cases; atomic pre-Agent claim, one-call flow, `CREATED`/`UNCHANGED`, changed bindings, subject isolation and failure boundaries |
| Material preparation workflow | Partial | material/bundle tests; end-to-end service not yet unified |
| P0–P3 to execution strategy | Migration blocker | legacy runtime maps P0/P1→High, P2→Medium, P3→Low; it must not consume the target policy |

| Control / safety capability | Current evidence |
|---|---|
| text, email, phone, textarea | shared ATS and Workday fixtures |
| native select, ARIA combobox, radio, checkbox | adapter and exact read-back tests |
| resume upload | uploaded-byte hash/read-back tests |
| unknown required question | handoff tests |
| submit confirmation | verified-evidence and `SUBMIT_UNKNOWN` tests |
| duplicate submit prevention | submission-intent and duplicate-click tests |

Current release fixture baseline:

- supported ATS Review arrival: `5/5`;
- model calls on those five paths: `[0, 0, 0, 0, 0]` (median `0`);
- live Review-arrival and submit-success metrics: not yet measured.

### Test policy

- CI and routine local tests use only synthetic identities and sanitized fixtures.
- `tests/test_real_forms.py` is excluded from routine validation and never submits.
- Current tests cover Python mapper privacy projection/key-status invariants, exact read-back, approval bindings, duplicate protection, and no-retry states.
- `JobPosting` required-key and enum equivalence is enforced against its JSON Schema. Automated OpenAPI validation, `$ref` resolution, and full generic format validation remain pending for the other machine contracts.
- Every fixed bug adds a sanitized regression test named for the failed invariant.
- A regression is complete only when the test fails on the old behavior and passes with the fix.
- Fixture metrics and live metrics are always reported separately by ATS, adapter version, and date.

Primary evidence:

```text
tests/test_acceptance_metrics.py
tests/test_ats_adapter_contract.py
tests/test_workday_adapter.py
tests/test_application_engine.py
tests/test_job_discovery.py
tests/test_job_search.py
tests/test_conversational_named_search.py
tests/test_semantic_mapper_contract.py
```
