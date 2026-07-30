# Jobops Contracts and Tests

This document is the authority for component contracts and implementation evidence. Domain rules are defined in `DOMAIN_AND_RULES.md`; this file does not redefine them.

## 契约

### Contract status

| Contract | Status |
|---|---|
| `CanonicalApplicationAnswerKey` / shared taxonomy registry | Implemented P2b3a as the single versioned field-language contract for FormIR, SemanticMapper and ApplicationBundle |
| `prepare_application_answers()` / `PreparedApplicationAnswerSet` | Implemented P2b3b as the immutable plan-scoped projection of trusted CandidateVault facts into prepared and unresolved canonical answers |
| async `run_application_preparation()` / `ApplicationPreparationRun` | Implemented P2b4/P2b4f as strict serial composition of synchronous or awaitable public preparation Slices with exactly-once invocation, typed lineage, defer/failure short-circuiting, cancellation propagation and completed zero-call replay |
| `PreparationStageOutcome` / `PreparationStopReasonEnvelope` | Implemented P2b4a as the v2 closed stage-specific stop-reason foundation, with exact orchestration-v1 read compatibility, explicit `LEGACY_UNTYPED` adapters and a Base LaTeX sample migration |
| Resume semantic stage stop-reason contracts | Implemented P2b4b for Base Resume Selection, Source Resume Projection, CandidateEvidence Snapshot, Tailored Resume Draft and Resume Fact QA, with exhaustive typed adapters and no new legacy writes from those stages |
| Cover Letter / Application Answers stop-reason contracts | Implemented P2b4c for Cover Letter Evidence, Draft, Fact QA and Prepared Application Answers, including typed fact/choice/attestation deferrals and preserved QA blocker lineage |
| Publication / Manifest stop-reason contracts | Implemented P2b4d for Prepared Resume Publication, Resume Manifest Entry, Prepared Cover Letter Publication and Cover Letter Manifest Entry, with four independent closed typed adapters and unchanged formal record identity/idempotency |
| Technical preparation stage stop-reason contracts | Implemented P2b4e for LaTeX Construction, sandboxed Compilation, Resume Visual QA and bounded Layout Revision; all new production P2b4 stages now have closed typed contracts while historical v1 remains byte-stable |
| `build_current_human_attention_queue()` / `HumanAttentionQueueResult` | Implemented P2b5 as a zero-write subject read model over current PreparationRuns and blocking AnswerSet unresolved items |
| `HumanAttentionResolutionCapability` / typed deferred classification | Implemented P2b5a/P2b5a2 for all registered typed defers, including all 16 technical reasons and Layout child-compilation lineage dispatch; no reason grants approval without review-target identity |
| `FactQAFindingAttentionRef` / `FactQABlockingFindingProvider` | Implemented P2b5d as the queue-v3 finding-level projection for direct and Publication-derived Resume/Cover Letter Fact QA blockers; mapping identity remains human-attention-mapping-v3 |
| async `run_selective_batch_preparation()` / `SelectiveBatchPreparationResult` | Implemented P2b6/P2b6a/P2b4f as bounded serial P2b4 composition using direct await, one fixed P2b5 snapshot, deterministic Plan selection, per-Plan failure isolation, and exact completed/unchanged Preparation Assembly lineage projection |
| `PlanMaterialManifest` v1/v2 / `ManagedArtifactReference` | Implemented P2c0 as exact v1 compatibility read, v2 PDF byte-size binding, and optional managed Cover Letter PDF support in the existing MaterialBundle |
| `assemble_application_bundle()` / `ApplicationBundleAssemblyRecord` | Implemented P2c1 as the fail-closed plan-scoped handoff from a v2 Manifest and canonical AnswerSet into the existing ApplicationBundle |
| `ApplicationExecutionIdentityProfile` / `ApplicationBundleConsumerBinding` | Implemented P2c1d1a as the closed, versioned production identity/contact taxonomy and explicit consumer-input classification; answers, materials, job context and Workday runtime settings no longer come from the mixed profile mapping |
| `CandidateIdentityFact` / `PrivateHomeCandidateIdentityFactRepository` | Implemented P2c1d1b as immutable source-bound identity facts over the exact P2c1d1a field enum, with proposed/legacy/verified separation, SQLite transaction-backed current CAS, typed current reads and deterministic PII-minimal subject indexes |
| `CandidateInformationSource` / `PrivateHomeCandidateInformationSourceRepository` | Implemented C1a as the immutable FILE/URL/USER_STATEMENT source registry with byte-detected bounded formats, HTTPS canonicalization, atomic content-addressed managed payloads, subject-scoped metadata/payload providers and direct P2c1d1b source-ref projection |
| `CandidateSourceProjection` / `PrivateHomeCandidateSourceProjectionRepository` | Implemented C1b as immutable exact-C1a projections with deterministic document/text/image/HTML blocks, stable locators, managed visual assets, pinned-address HTTPS capture, atomic subject-scoped persistence and payload-free metadata reads |
| `CandidateFactProposal` / `PrivateHomeCandidateFactProposalRepository` | Implemented C1c as one-generation structured extraction over bounded exact-C1b blocks/assets, closed P2c1d1a fields, deterministic P2c1d1b normalization, exact evidence lineage, immutable subject-scoped proposal/run persistence and PII-minimal list reads |
| `CandidateFactReviewDecision` / `CandidateFactReviewUIController` | Implemented C1d as the authenticated proposal/current/evidence review queue with explicit accept/edit/reject/keep/replace/missing actions, immutable claim/receipt recovery, public P2c1d1b USER_CONFIRMED writes and expected-current CAS |
| `run_selective_bundle_assembly()` / `SelectiveBundleAssemblyResult` | Implemented P2c10b as the bounded serial public P2b6-lineage → P2c1 handoff with no Preparation/Assembly repository scan |
| `RecoverableApplicationBundleEnvelopeRepository.get_for_assembly()` | Implemented P2c1b as the immutable, subject-isolated and hash-verified recovery contract for the exact P2c1 ApplicationBundle |
| `plan_application_document_uploads()` / `ApplicationDocumentUploadPlan` | Implemented P2c2 as deterministic at-most-once Resume/Cover Letter PDF selection for typed FormIR file controls and shared BaseATSAdapter fill |
| `execute_non_submit_application()` / `NonSubmitApplicationExecutionRecord` | Implemented P2c3 as the Gate-A-aware, one-shot Browser/Engine handoff for one recovered P2c1 bundle with a hard non-submit boundary |
| `decide_submission_authorization()` / `SubmissionAuthorizationDecision` | Implemented P2c4 as the offline Gate B policy decision for one exact P2c3 Review, with automatic or review-scoped explicit-user authorization and zero submission side effects |
| `PlanScopedSubmissionPermitBindings` / `GateAConsumptionReference` / `OpaquePermitTokenReference` | Implemented P2c5a as an explicit versioned extension of the existing Foundation Permit contract; legacy permit bytes and semantics remain unchanged, P2c3 v2 records persist verifiable Gate A consumption provenance, signer metadata is read-only, and bearer tokens remain behind subject-isolated opaque credential references |
| `issue_submission_permit()` / `SubmissionPermitRecord` | Implemented P2c5b as the offline issuance boundary that converts one exact `AUTHORIZED` Decision into a 300-second, plan/review/adapter/action-scoped Foundation Gate B permit while persisting only an opaque token reference |
| `execute_authorized_submission()` / `AuthorizedSubmissionExecutionRecord` | Implemented P2c6 as the one-shot Browser/Engine bridge for a plan-scoped permit, with Review replay, adapter-callback point-of-no-return consumption, existing submission-intent/evidence reuse, immutable verified/uncertain outcomes and zero automatic retry |
| `run_application_execution()` / `ApplicationExecutionRun` | Implemented P2c7 as strict serial composition of the four public P2c3–P2c6 stages, with typed ordered lineage, defer/block/failure short-circuiting, terminal uncertainty and completed/uncertain zero-call replay |
| `build_current_application_execution_queue()` / `CurrentApplicationExecutionQueueResult` | Implemented P2c8 as the zero-write subject read model that combines deterministic current Assemblies with terminal/current ExecutionRuns into stable READY, DEFERRED, FAILED, SUBMISSION_UNCERTAIN and SUBMITTED items |
| `run_selective_batch_execution()` / `SelectiveBatchExecutionResult` | Implemented P2c9 as one-snapshot, READY-only, bounded serial P2c7 composition with caller-order allowlists, terminal skips and per-Plan defer/failure/uncertainty isolation |
| `run_automation_cycle()` / `AutomationCycleRun` | Implemented P2c10a/P2c10b as invocation-scoped immutable v2 audit over one bounded serial P1d3 → P2a1b → P2b6 → Bundle → P2c9 cycle, with exact v1 four-stage reads |
| `SemanticMapper.map_controls()` | Implemented as an in-process provider-neutral Protocol |
| `AdapterRegistry.run()` / deterministic ATS lifecycle | Implemented |
| `run_discovery(JobDiscoveryRequest)` | Implemented for typed conversational proposals and Private Home upsert |
| `save_search_profile()` / `SearchProfileProvider` | Implemented S3a subject-scoped immutable search configuration with canonical JobSearchRequest, typed Greenhouse board source and deterministic current/enabled reads |
| `refresh_job_library()` / `JobLibraryRefreshRun` | Implemented S3b manual all-enabled-profile Search → Public Read → formal ADD_JOB Discovery → optional explicit S3c intent decision → bounded P1d3 flow with canonical URL de-duplication and invocation replay |
| `save_search_profile_intent_policy()` / `decide_search_profile_intent()` | Implemented S3c immutable subject/profile policy with default ADD_JOB_ONLY and explicit AUTO_REQUEST_APPLICATION |
| `resolve_authenticated_subject()` / `AuthenticatedSubjectContext` | Implemented S3d0 as fixed-cookie, Keychain-backed server-side subject resolution with explicit-now expiry and a reusable FastAPI dependency |
| `RefreshJobLibraryUIController.refresh()` / `/api/job-library/refresh` | Implemented S3d as the authenticated, one-invocation UI adapter over the injected S3b public callable with in-flight de-duplication and a bounded safe result projection |
| `ContinueAutomationUIController.run()` / `/api/automation-cycle/run` | Implemented S3e as the authenticated, server-budgeted UI adapter over the injected P2c10a public callable with in-flight de-duplication and typed safe stage summaries |
| `HumanAttentionInboxUIController.load()` / `/api/human-attention-inbox` | Implemented S3f as the authenticated, read-only UI projection of one injected P2b5 snapshot with order-preserving USER/OPERATOR groups |
| `resolve_application_answer()` / `/api/human-attention-inbox/{item_id}/resolve` | Implemented S3g1 for current USER Application Answers items with one bounded parser call, deterministic taxonomy validation, authoritative fact or plan-scoped attestation writes, immutable receipts and one P2b4 rerun |
| `resolve_version_choice()` / `/api/human-attention-inbox/{item_id}/resolve-version-choice` | Implemented S3g2 for current P2a3/P2a6b USER choice items with public selectable-option validation, deterministic or one-call parser resolution, immutable plan-scoped overrides/receipts and one P2b4 rerun |
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
| `PreparationAdmissionPolicy` | Implemented P1a2 as reviewed, versioned policy content for Application Preparation eligibility |
| `PriorityAgentPort` / `PriorityProposal` | Implemented P1b as one injected, tool-free call followed by ordinary-code validation |
| `finalize_priority_proposal()` / `PriorityDecision` | Implemented P1c with deterministic hard-constraint reconciliation and immutable Private Home persistence |
| `orchestrate_single_job_priority()` | Implemented P1d1 for one persisted job with pre-Agent input-binding idempotency |
| `build_current_priority_queue()` | Implemented P1d2 typed read model over existing P1d1 bindings and completed artifacts |
| `selectively_reprioritize_jobs()` | Implemented P1d3 bounded serial composition of P1d2 selection and P1d1 execution |
| `build_runnable_application_queue()` | Implemented P1d4 read-only preparation-admission view over one P1d2 snapshot and accepted intents |
| `create_application_plan()` / `ApplicationPlan` | Implemented P2a1 immutable automation-first handoff from one selected RUNNABLE job |
| `run_selective_batch_plan_creation()` | Implemented P2a1b bounded serial RUNNABLE-job handoff from one fixed P1d4 snapshot |
| `register_resume_candidate()` / `ResumeCandidateProvider` | Implemented P2a2 explicit subject-scoped trusted artifact registry and typed selectable-candidate reads |
| `select_base_resume()` / `ResumeSelectionDecision` | Implemented P2a3 bounded automatic base-resume selection with pre-Agent idempotency |
| `create_source_resume_projection()` / `SourceResumeProjection` | Implemented P2a4a deterministic, hash-bound PDF/DOCX source projection |
| `create_candidate_evidence_snapshot()` / `CandidateEvidenceSnapshot` | Implemented P2a4b subject-specific immutable source-resume evidence boundary |
| `tailor_resume()` / `TailoredResumeDraft` | Implemented P2a4c evidence-bound tailoring draft with deterministic Agent-output validation |
| `run_resume_fact_qa()` / `ResumeFactQAResult` | Implemented P2a5 independent fact gate with deterministic checks before any bounded QA Agent call |
| `register_resume_latex_version()` / `ResumeLatexVersionProvider` | Implemented P2a6a/P2a6a1 trusted subject-scoped LaTeX version registry with unchanged general-source admission plus an explicit strict single-file base-template profile |
| `select_base_latex_version()` / `BaseLatexSelectionDecision` | Implemented P2a6b metadata-only base-version selection gated on a PASSED fact-QA result |
| `construct_resume_latex_version()` / `ResumeLatexConstructionRecord` | Implemented P2a6c controlled-marker LaTeX construction with deterministic fidelity and stale-content validation |
| `compile_resume_latex()` / `LatexCompilerPort` | Implemented P2a7 shell-free sandboxed compilation with deterministic PDF validation and managed artifacts |
| `review_resume_visual_qa()` / `ResumeVisualQAResult` | Implemented P2a8a report-only visual QA with deterministic checks before any render or Agent call |
| `revise_resume_layout()` / `ResumeLayoutRevisionRun` | Implemented P2a8b bounded typography-only revision composing the P2a7 and P2a8a entry points |
| `publish_prepared_resume()` / `PreparedResumeMaterial` | Implemented P2a9 immutable publication of one approved compiled PDF per ApplicationPlan |
| `assemble_plan_material_manifest()` / `PlanMaterialManifest` | Implemented P2b1 plan-scoped manifest carrying the published resume, separate from the legacy `MaterialManifest` |
| `create_cover_letter_evidence_snapshot()` / `CoverLetterEvidenceSnapshot` | Implemented P2b2a subject-specific cover-letter evidence with its own `COVER_LETTER` scope, independent of resume-tailoring evidence |
| `draft_cover_letter()` / `CoverLetterDraft` | Implemented P2b2b evidence-bound cover letter draft with deterministic Agent-output validation |
| `review_cover_letter_fact_qa()` / `CoverLetterFactQAResult` | Implemented P2b2c independent fact QA over one CoverLetterDraft, deterministic-first with a bounded QA Agent for semantic exaggeration only |
| `publish_prepared_cover_letter()` / `PreparedCoverLetterMaterial` | Implemented P2b2d deterministic publication of one PASSED Draft through the managed one-page template and existing sandboxed compiler port |
| `include_cover_letter_in_plan_material_manifest()` | Implemented P2b2e immutable inclusion of one published cover letter while preserving the prior Resume entry |
| `LatexBuildProvenance` | Implemented P2a8b shared protocol letting construction and revision records both describe one managed build |
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
    ResolvePendingIntakeRequest(subject_id, conversation_id, pending_intake_id, action),
    *,
    pending_store,
    accepted_intent_repository,
    discovery_port,
) -> ResolvePendingIntakeResponse
```

The request contains explicit `subject_id`, `conversation_id`,
`pending_intake_id` and one fixed `ADD_JOB` / `REQUEST_APPLICATION` action.
The handler validates subject binding, ownership,
expiry, state and the retained observation, then maps that observation without
new facts into the existing `ResolvedJobCandidate`, `JobIntakeProposal` and
`JobDiscoveryRequest` contracts. Stable proposal/request IDs derive from the
pending ID.

The store atomically marks the item `RESOLVING` before one port call. A typed
accepted Discovery response moves through `PERSISTING_INTENT` before completion.
The immutable intent write result is retained with the completed item; the same
action replays it without another Discovery or write. A different action or
subject fails without another call. An exception before a typed Discovery
response restores `WAITING_FOR_ACTION`. A persistence failure becomes typed
`INTENT_PERSISTENCE_FAILED`; explicit retry resumes only the intent write.

#### Accepted job intent repository

```text
save(AcceptedJobIntent) -> CREATED | UNCHANGED | typed failure
get_current(subject_id, job_id) -> FOUND | NOT_FOUND | INTEGRITY_FAILURE
```

Records live under Private Home `state/intake/accepted-job-intents/`. Explicit
contract-version dispatch preserves v1 serialized bytes and identity without
injecting compatibility fields. New `accepted-job-intent-v2` records add a
typed `AcceptedJobIntentSourceProvenance`: `CONVERSATIONAL_INTAKE` binds its
proposal source ID, while `SEARCH_PROFILE_REFRESH` can bind a stable,
deduplicated ordering of one or more profile IDs and an optional source
version. The v2 immutable ID hashes the original bindings plus the canonical
provenance payload; time remains excluded. An existing ID with different
content is an integrity conflict. Reads fail closed on corrupt records and use
the unchanged domain timestamp/stable-ID selection, with any explicit
`REQUEST_APPLICATION` taking precedence over `ADD_JOB`; provenance never
affects precedence. Neither value is a submission permit or Application Engine
intent.

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

#### `save_search_profile()` / `SearchProfileProvider`

```text
save_search_profile(
    SaveSearchProfileCommand(
        subject_id,
        optional existing profile_id,
        display_name,
        company,
        title,
        optional location,
        SearchProfileSourceReference(
            KNOWN_GREENHOUSE_BOARD,
            board_token,
        ),
        enabled,
        refresh_mode=MANUAL,
        now,
    ),
    repository=SearchProfileRepository,
) -> SaveSearchProfileResult

SearchProfileProvider.list_current(subject_id)
SearchProfileProvider.list_enabled(subject_id)
```

The persisted query is a real `JobSearchRequest`. Company and title/location
use the same exported canonicalizers as Known Greenhouse Board Search, so
case, whitespace and punctuation-equivalent queries replay `UNCHANGED`.
Content changes append a new immutable version and retain the original
`created_at`; time is excluded from content hash. `get()` and list reads parse
and validate every selected record, fail closed on version corruption, isolate
subjects and use display-name/profile-ID domain ordering. This contract has no
JobSearchPort, network, Discovery, Priority or application capability.

#### `refresh_job_library()`

```text
await refresh_job_library(
    ManualJobLibraryRefreshCommand(
        subject_id,
        invocation_id,
        now,
        positive max_reprioritizations,
    ),
    profile_provider=SearchProfileProvider,
    search_executor=SearchProfileSearchExecutor,
    public_job_reader=PublicJobReader callable,
    discovery=JobDiscoveryPort callable,
    priority_refresh=P1d3 callable,
    repository=JobLibraryRefreshRunRepository,
) -> ManualJobLibraryRefreshResult
```

The service reads `list_enabled(subject_id)` once. It searches each profile
once with the persisted `JobSearchRequest`, then uses public
`normalized_job_url()` identity to group candidates across profiles. Each
unique valid URL is read once and converted to one resolved
`ADD_JOB / MANUAL_LIBRARY_REFRESH` Discovery request. Discovery alone writes
or revises JobPosting and DiscoveryRun records.

Failures are retained per profile/candidate and never stop later work. P1d3 is
called once after candidate processing with the same subject/time and explicit
bound, including after partial failures. Empty enabled snapshots are `NOOP`
with no downstream call. Subject/invocation replay reads the immutable,
hash-validated Private Home Run before every downstream dependency.

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
immutable Private Home snapshot.

P1a2 adds a typed `PreparationAdmissionPolicy` to every new draft and approved
snapshot. Its default directly admits P0/P1/P2 to later preparation
consideration and requires a separate explicit promotion for P3. The reviewed
direct and promotion sets are P0–P3-only, duplicate-free, deterministic and
disjoint; `NEEDS_USER` and `EXCLUDED` fail validation. Admission does not carry
application intent or execution/submission authority.

The canonical content hash includes raw policy text, approved hard/soft items
and preparation admission. Draft ID, time and interpreter metadata do not
affect it. Equal active content is idempotent; changed content increments the
subject-local policy version and supersedes the prior active version without
deleting history. The repository schema requires admission explicitly: an old
approved record without it fails closed with a typed compatibility error rather
than receiving defaults or a changed hash at read time. A separate migration
must precede reapproval; P1a2 does not implement that migration.

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

#### Current priority queue read model

```text
build_current_priority_queue(
    CurrentPriorityQueueCommand(subject_id, now),
    typed JobPosting list repository,
    ACTIVE policy provider,
    CandidateSummaryProvider,
    orchestration read repository,
    PriorityDecision read repository,
    adapter metadata,
) -> CurrentPriorityQueueResult
```

`PrivateHomeJobPostingRepository.list_current()` uses the same canonical parser
as single-job reads, returns typed usable records in stable job-ID order and
fails closed on any damaged persisted record. The queue builder reuses
`build_single_job_priority_binding()`; it does not maintain a parallel
definition of current inputs.

Each `CurrentPriorityQueueItem` is exactly one of `CURRENT`, `STALE`, `MISSING`
or `INCOMPLETE`. Only `CURRENT` exposes the existing Proposal and Decision.
Stale reasons are direct binding comparisons covering job revision/content,
policy, CandidateSummary, Agent/prompt/model metadata, evaluation time, Gate
version and orchestration version. An exact non-completed lifecycle is
`INCOMPLETE`; corrupted orchestration or Decision data fails the whole read.

The operation has no Agent, Proposal, Gate, claim or repository-write path.
Current items sort by persisted Decision rank P0→P3, NEEDS_USER, EXCLUDED,
then `validated_at` and `job_id`; the remaining states form separate stable
groups. This is a priority read model, not an execution eligibility queue.

#### Selective batch reprioritization

```text
selectively_reprioritize_jobs(
    SelectiveBatchReprioritizationCommand(
        subject_id,
        now,
        job_ids=None,
        max_jobs=None,
    ),
    queue_reader=P1d2 callable,
    single_job_orchestrator=P1d1 callable,
) -> SelectiveBatchReprioritizationResult
```

The command must provide a non-empty `job_ids` allowlist or a positive
`max_jobs`. Explicit IDs retain first-occurrence order after deduplication;
when both bounds exist, `max_jobs` truncates the deduplicated allowlist. Without
an allowlist, P1d3 selects at most `max_jobs` STALE/MISSING entries in P1d2
order. CURRENT, INCOMPLETE and absent explicit IDs become typed skipped or
not-found items without a P1d1 call.

P1d3 calls P1d2 exactly once and then awaits P1d1 serially at most once per
selected job. It forwards the identical aware `now`, preserves each
`SingleJobPriorityResult`, records typed per-item failures and continues with
later selected jobs. It does not import or call the Agent, Proposal service,
Gate, binding builder or repositories. There is no batch persistence, retry,
concurrency or compensating rollback.

#### Runnable Application Queue read model

```text
build_runnable_application_queue(
    RunnableApplicationQueueCommand(subject_id, now),
    priority_queue_reader=P1d2 callable,
    accepted_intent_repository=typed read port,
) -> RunnableApplicationQueueResult
```

P1d4 calls P1d2 once and consumes the exact `PrioritizationPolicy` snapshot
exposed by that successful result. The snapshot supplies the reviewed
`PreparationAdmissionPolicy`; P1d4 never performs a second ACTIVE-policy
lookup. Each item is typed as RUNNABLE or a deterministic blocked state for
non-current Priority, absent application intent, NEEDS_USER, EXCLUDED,
unadmitted priority, promotion requirement or unavailable Job lifecycle.

Only an authoritative `REQUEST_APPLICATION` intent can satisfy the intent
condition. `NOT_FOUND` means no accepted intent; intent corruption or read
failure fails the complete queue. The result preserves P1d2 order and does not
call P1d1/P1d3, Agent, Proposal, Gate, claim/save, Application Preparation or
Execution.

#### Automation-first ApplicationPlan

```text
create_application_plan(
    CreateApplicationPlanCommand(
        subject_id,
        job_id,
        now,
        optional user_preparation_instructions,
    ),
    runnable_queue_reader=P1d4 callable,
    repository=ApplicationPlanRepository,
) -> CreateApplicationPlanResult
```

P2a1 calls P1d4 exactly once and creates only from the matching typed
`RUNNABLE` item. The immutable `ApplicationPlan` binds job revision/content,
Decision ID, policy ID/version/hash, accepted REQUEST_APPLICATION intent,
priority, plan contract version, fixed preparation stages, automation/human
attention policies and an exact instruction hash.

The stable plan ID excludes `created_at`. Identical semantic input is
`UNCHANGED` and returns the original timestamp; any job, Decision, policy,
intent or instruction change creates a distinct immutable record. Private Home
uses atomic create-if-absent storage and typed FOUND/NOT_FOUND/INTEGRITY_FAILURE
reads. P2a1 neither executes a stage nor imports Agent, materials, browser, ATS
or Application Engine code.

#### Selective Batch ApplicationPlan Creation

```text
run_selective_batch_plan_creation(
    SelectiveBatchPlanCreationCommand(
        subject_id,
        now,
        optional ordered job_ids,
        optional positive max_jobs,
        optional typed per-job preparation instructions,
    ),
    runnable_queue_reader=P1d4 public callable,
    single_job_plan_creator=P2a1 public callable,
) -> SelectiveBatchPlanCreationResult
```

P2a1b reads one P1d4 snapshot and invokes P2a1 at most once, serially, for
each bounded `RUNNABLE` item. Explicit allowlists retain first-occurrence
caller order; blocked and absent jobs remain typed result items without using
the P2a1 call quota. Per-job exceptions and typed failures are isolated.
`NOOP / COMPLETED / PARTIAL_FAILURE / FAILED` aggregate the actual calls, and
replay delegates identity and persistence entirely to P2a1.

#### `register_resume_candidate()` / `ResumeCandidateProvider`

```text
register_resume_candidate(
    RegisterResumeCandidateCommand(
        subject_id,
        managed artifact path,
        display name,
        selection-safe summary + trust metadata,
        explicit now,
    ),
    home=PrivateHome,
    repository=ResumeCandidateRepository,
) -> RegisterResumeCandidateResult

ResumeCandidateProvider.list_selectable(subject_id)
    -> ResumeCandidateListResult
```

P2a2 never scans a directory or promotes `default_resume`,
`resume_variants` or fallback paths. The registration source must be an
explicit PDF/DOCX below Private Home `documents/master/`; bytes are validated
and hashed locally, then copied to
`state/preparation/resume-candidates/artifacts/<subject-key>/`. Immutable typed
records live below the sibling `records/<subject-key>/` directory.

The stable resume ID binds subject, artifact hash/type, display name, exact
summary, summary source/trust, selectable status and contract version; time is
excluded. Identical replay is `UNCHANGED`. Reads require explicit subject
ownership and revalidate both the JSON record and artifact bytes. Any corrupt
record or missing/mismatched artifact fails the full list with
`INTEGRITY_FAILURE`. Only authenticated-caller summaries marked `VERIFIED` or
`USER_CONFIRMED` are accepted. P2a2 calls no Agent, JobPosting reader,
selection, tailoring or execution service.

#### `select_base_resume()`

```text
select_base_resume(
    SelectBaseResumeCommand(subject_id, application_plan_id, explicit now),
    application_plan_repository,
    job_repository,
    candidate_provider,
    agent,
    metadata,
    decision_repository,
) -> SelectBaseResumeResult
```

P2a3 loads the immutable plan, validates subject ownership, then requires the
typed JobPosting ID/revision/hash to match its binding exactly. The complete
candidate set comes from `ResumeCandidateProvider`; no path or artifact bytes
are sent to the Agent.

Zero candidates return `DEFERRED_NO_RESUME` with no write. One candidate is
selected deterministically with zero Agent calls. Multiple candidates permit
one tool-free `ResumeSelectionAgentPort.evaluate()` call over typed JD data,
selection-safe candidate projections and the exact plan-scoped instructions.
Ordinary code validates the returned resume ID, candidate contract version and
artifact hash. Refusal, ambiguity or any mismatch is
`DEFERRED_NEEDS_HUMAN`; there is no retry or successful Decision.

The candidate-set canonical hash and a pre-Agent selection binding include the
plan/job bindings, candidate set, selection contract and configured
Agent/prompt/model versions. A completed binding is read before evaluation and
returns `UNCHANGED` with the original `selected_at`. Immutable Decision content
has its own validated hash; conflicting or corrupt records fail closed. P2a3
does not tailor or copy a resume, create Human Attention state, prepare
materials, invoke execution code or authorize submission.

#### `create_source_resume_projection()`

```text
create_source_resume_projection(
    CreateSourceResumeProjectionCommand(subject_id, resume_id, explicit now),
    candidate_repository,
    artifact_reader,
    deterministic parser,
    projection_repository,
) -> CreateSourceResumeProjectionResult
```

P2a4a reads one subject-owned `ResumeCandidate`, re-reads its managed artifact
bytes and checks their SHA-256 before any repository reuse or parsing.
`DeterministicSourceResumeParser` uses `pdfplumber` for text-based PDF
page/line extraction and standard-library ZIP/XML for DOCX body paragraphs and
table cells. It performs no OCR, model call or semantic completion.

The typed projection contains ordered sections and blocks, exact extracted
text, stable section/block/bullet IDs and typed PDF or DOCX locators. IDs bind
artifact hash, locator, contract and parser versions. The projection ID also
binds subject/resume ownership; its canonical content hash excludes
`projected_at`. Private Home records are immutable under
`state/preparation/source-resume-projections/<subject-key>/`. Reads distinguish
FOUND, NOT_FOUND and INTEGRITY_FAILURE; creates distinguish CREATED, UNCHANGED,
UNSUPPORTED, UNREADABLE and FAILED.

#### `create_candidate_evidence_snapshot()`

```text
create_candidate_evidence_snapshot(
    CreateCandidateEvidenceSnapshotCommand(
        subject_id,
        application_plan_id,
        explicit now,
    ),
    application_plan_repository,
    selection_repository,
    candidate_repository,
    projection_repository,
    snapshot_repository,
) -> CreateCandidateEvidenceSnapshotResult
```

P2a4b uses typed read queries to select the latest complete immutable
`ResumeSelectionDecision` for the Plan and latest complete immutable
`SourceResumeProjection` for the selected resume/artifact. Ordering is by the
stored domain timestamp, then stable ID; malformed sibling records fail the
whole query. The builder revalidates every Plan/Selection/Candidate/Projection
identity and content-hash binding before creating evidence.

Each ordered `CandidateEvidenceItem` copies one source block's exact text,
section/block/bullet identity and typed locator. Source type is
`SOURCE_RESUME_PROJECTION`; sensitivity is `PERSONAL`; the sole allowed scope
is `RESUME_TAILORING`; verification is
`USER_PROVIDED_DOCUMENT_STATEMENT`. The snapshot is immutable below
`state/preparation/candidate-evidence-snapshots/<subject-key>/` and provides
typed FOUND/NOT_FOUND/INTEGRITY_FAILURE reads plus
CREATED/UNCHANGED/DEFERRED_NO_EVIDENCE/FAILED creation outcomes.

#### `tailor_resume()`

```text
tailor_resume(
    TailorResumeCommand(
        subject_id,
        application_plan_id,
        evidence_snapshot_id,
        explicit now,
    ),
    application_plan_repository,
    job_repository,
    selection_repository,
    candidate_repository,
    projection_repository,
    evidence_snapshot_repository,
    agent: ResumeTailoringAgentPort,
    metadata: ResumeTailoringAgentMetadata,
    draft_repository,
) -> TailorResumeResult
```

P2a4c revalidates every Plan/Job/Selection/Candidate/Projection/Snapshot
identity and content-hash binding, then checks the pre-Agent tailoring
binding so a completed input replays `UNCHANGED` with zero Agent calls. The
`ResumeTailoringAgentPort` receives one typed context—trusted JD, source
projection, `RESUME_TAILORING` evidence, verbatim Plan instructions and the
static versioned policy—and must return a typed
`ResumeTailoringAgentOutput` covering every source block exactly once.
Deterministic validation enforces evidence existence and scope, source
references, verbatim JD alignment substrings, evidenced numbers and
proper-noun tokens, evidence-supported JD verbs, weak-verb rejection and
user-protected retention. Outcomes are typed
CREATED/UNCHANGED/DEFERRED_INSUFFICIENT_EVIDENCE/DEFERRED_NEEDS_HUMAN/FAILED;
the immutable draft persists below
`state/preparation/tailored-resume-drafts/<subject-key>/`.

#### `run_resume_fact_qa()`

```text
run_resume_fact_qa(
    RunResumeFactQACommand(
        subject_id,
        tailored_resume_draft_id,
        explicit now,
    ),
    draft_repository,
    application_plan_repository,
    job_repository,
    selection_repository,
    projection_repository,
    evidence_snapshot_repository,
    agent: ResumeFactQAAgentPort,
    metadata: ResumeFactQAAgentMetadata,
    qa_repository,
) -> RunResumeFactQAResult
```

P2a5 revalidates the complete Draft/Plan/Job/Selection/Projection/Snapshot
binding and returns `BLOCKED_BINDING_MISMATCH` with no Agent call and no
persisted record on any mismatch. It then re-derives every checkable fact
independently of the P2a4c validator; a blocking deterministic finding
returns `BLOCKED_UNSUPPORTED_CLAIM` with zero Agent calls. `ResumeFactQAAgentPort`
is invoked at most once, only for semantic judgment, over rewritten bullets
and tailoring-scoped evidence alone, and its findings are accepted only after
ordinary code confirms every bullet and evidence reference. Each finding
records a stable content-derived ID, source reference, type, severity
(`BLOCKING` or `ADVISORY`), claim text, cited evidence, explanation and
`DETERMINISTIC`/`AGENT` provenance. `PASSED`, `BLOCKED` and `DEFERRED`
verdicts all persist immutably below
`state/preparation/resume-fact-qa-results/<subject-key>/`; a completed
binding replays as `UNCHANGED` with zero Agent calls.

#### `register_resume_latex_version()`

```text
register_resume_latex_version(
    RegisterResumeLatexVersionCommand(
        subject_id,
        source_kind,
        explicit now,
        latex_source | source_path,
        optional parent_version_id,
        optional root_family_id,
        optional template / source resume / draft / fact-QA bindings,
        optional labels,
        source_profile=GENERAL_SOURCE_V1
    ),
    home,
    repository,
) -> RegisterResumeLatexVersionResult
```

Exactly one of `latex_source` and `source_path` is required; supplying both
is `SOURCE_AMBIGUOUS` and supplying neither is `SOURCE_MISSING`. A path must
already resolve inside Private Home, end in `.tex`, and not be a symlink.
Bytes are decoded as UTF-8, capability-scanned, hashed, then written to
`state/preparation/resume-latex-versions/sources/<subject-key>/<sha256>.tex`;
records live under the sibling `records/<subject-key>/`. Rejected
capabilities are reported as a typed `ResumeLatexCapability` alongside
`SOURCE_CAPABILITY_REJECTED`.

`source_profile` is closed and versioned. `GENERAL_SOURCE_V1` is the default
and preserves the original record bytes and relative-include behavior.
Explicit `SINGLE_FILE_BASE_TEMPLATE_V1` additionally validates one document
root, the empty ordered controlled region, the `JobopsSection` /
`JobopsBullet` two-argument interface, the managed package set, and the
absence of every external-file capability. Its immutable record and identity
include `base-latex-template-v1`,
`resume-latex-single-file-dependencies-v1`, and
`resume-latex-source-safety-v1`. Structure failures,
dependency-policy failures and unsafe capabilities are distinct typed
registration failures. Historical records omit these fields and continue to
decode as the general profile without being rewritten.

`ResumeLatexVersionProvider.list_selectable(subject_id)` returns typed
versions in stable version-ID order and treats an empty registry as
`SUCCEEDED`. `ResumeLatexVersionRepository` adds `save()` and
`get(subject_id, latex_version_id)`. Reads and writes re-verify the managed
source: a missing artifact, a hash drift or a newly unsafe capability turns
the record into `INTEGRITY_FAILURE` and fails any listing that contains it.

#### `select_base_latex_version()`

```text
select_base_latex_version(
    SelectBaseLatexVersionCommand(
        subject_id,
        application_plan_id,
        fact_qa_result_id,
        explicit now,
    ),
    application_plan_repository,
    fact_qa_repository,
    draft_repository,
    selection_repository,
    job_repository,
    latex_version_provider,
    agent: BaseLatexSelectionAgentPort,
    metadata: BaseLatexSelectionAgentMetadata,
    decision_repository,
) -> SelectBaseLatexVersionResult
```

The supplied fact-QA result must bind this plan and job, name the draft it is
paired with, match that draft's content hash, and carry verdict `PASSED`;
otherwise the call fails closed with zero Agent calls and no record.
Candidates arrive only through `ResumeLatexVersionProvider.list_selectable()`
and are projected into `BaseLatexCandidateView`, which carries version ID,
source kind, source hash, family, lineage, template, source resume, labels
and a verified `has_passed_fact_qa` flag — never a source reference or any
LaTeX content.

`selection_kind` is `EXISTING_VERSION` or `MANAGED_TEMPLATE_FALLBACK`, and
`selection_method` is `USER_REQUIRED_VERSION`, `ONLY_CANDIDATE`,
`EXACT_SOURCE_RESUME_MATCH`, `AGENT_SELECTED` or
`MANAGED_TEMPLATE_FALLBACK`. Every method except `AGENT_SELECTED` is reached
with `agent_invoked=False`. Decisions persist immutably below
`state/preparation/base-latex-selections/<subject-key>/`.

#### `construct_resume_latex_version()`

```text
construct_resume_latex_version(
    ConstructResumeLatexCommand(
        subject_id,
        application_plan_id,
        base_latex_selection_decision_id,
        fact_qa_result_id,
        explicit now,
    ),
    application_plan_repository,
    draft_repository,
    fact_qa_repository,
    base_selection_repository,
    latex_version_repository,
    template_provider: ManagedResumeTemplateProvider,
    agent: ResumeLatexConstructionAgentPort,
    metadata: ResumeLatexConstructionAgentMetadata,
    construction_repository,
    home,
) -> ConstructResumeLatexResult
```

The controlled marker contract is `jobops-latex-markers-v1`:
`\JobopsSection{section_id}{title}` and `\JobopsBullet{bullet_id}{text}`
inside one `%% JOBOPS-CONTENT-BEGIN` / `%% JOBOPS-CONTENT-END` region, with
`\providecommand` definitions so a base layout may override the rendering.
Bullet IDs are the Draft bullets' `source_block_id`.

`construction_method` is `DETERMINISTIC_TEMPLATE_RENDER`,
`DETERMINISTIC_REGION_REPLACEMENT` or `AGENT_RECONSTRUCTED`; only the last
sets `agent_invoked`. `construction_path` is `MANAGED_TEMPLATE` or
`DERIVED_FROM_EXISTING_VERSION`. Statuses are
`CREATED`/`UNCHANGED`/`DEFERRED_SOURCE_UNREADABLE`/`DEFERRED_NEEDS_HUMAN`/
`FAILED`.

Registration goes through `register_resume_latex_version()`, so managed
source storage, hashing and the capability scan stay owned by P2a6a.
Construction provenance and the pre-Agent `UNCHANGED` lookup live in a
separate immutable record below
`state/preparation/resume-latex-constructions/<subject-key>/`, keyed by the
construction binding, which leaves registry identity and lineage untouched.

The stale-content check compares visible-text runs of at least
`STALE_CONTENT_MIN_CHARS` (40) characters between the base source and the
constructed source, rejecting any that is not current Draft content. Shorter
runs — a name, a contact line, a section label — may legitimately carry over
from the base layout.

#### `compile_resume_latex()`

```text
compile_resume_latex(
    CompileResumeLatexCommand(
        subject_id,
        resume_latex_construction_record_id,
        resume_latex_version_id,
        explicit now,
    ),
    construction_repository,
    latex_version_repository,
    compiler: LatexCompilerPort,
    compilation_repository,
    home,
) -> CompileResumeLatexResult
```

`LatexCompilerPort` has two methods. `describe()` returns a
`LatexCompilerDescription` (engine, compiler version, normalized flags,
compile and sandbox policy versions) and must not compile; it feeds the
binding so a replay never starts an engine. `compile()` takes a
`LatexCompileRequest` and returns a `LatexCompileOutcome` whose status is
`SUCCEEDED`, `COMPILATION_ERROR`, `TIMEOUT`, `OUTPUT_INVALID` or
`UNAVAILABLE`, plus bounded diagnostics and a `compiler_started` flag.

`SandboxedPdfLatexCompiler` is the V1 adapter: `pdflatex` only,
`shell=False`, fixed argv, a disposable temp directory as cwd,
`sandbox_environment()` for a minimal deterministic environment, a wall-clock
timeout, POSIX rlimits applied in the child, and stdout/stderr captured to
capped files inside the sandbox. `normalized_compile_flags()` replaces the
per-run output directory with `<sandbox>` so the binding stays stable across
runs.

Page count comes from `pdf_page_count()`, which parses the document with the
existing pdfplumber dependency rather than scanning raw bytes — a real engine
compresses page objects, so byte scanning undercounts. An unparseable
document counts zero pages and fails validation. `unmanaged_file_dependencies()`
reports the LaTeX macros that would pull in files the registry does not hold,
which is always a `DEFERRED_SOURCE_INCOMPLETE` in V1 because P2a6a manages
exactly one `.tex` per version. Records live below
`state/preparation/resume-compilations/<subject-key>/` and validated PDFs
below `state/preparation/compiled-resumes/<subject-key>/<pdf-sha256>.pdf`.

#### `review_resume_visual_qa()`

```text
review_resume_visual_qa(
    ReviewResumeVisualQACommand(
        subject_id,
        resume_compilation_record_id,
        explicit now,
    ),
    compilation_repository,
    latex_version_repository,
    construction_repository,
    draft_repository,
    renderer: PdfPageRendererPort,
    agent: ResumeVisualQAAgentPort,
    metadata: ResumeVisualQAAgentMetadata,
    visual_qa_repository,
    policy: ResumeVisualQAPolicy | None,
    home,
) -> ReviewResumeVisualQAResult
```

`ResumeVisualQAPolicy` is versioned as `resume-visual-qa-policy-v1` and
carries `max_pages` (default 1), `min_font_size_pt`,
`page_margin_tolerance_pt` and `min_text_characters_per_page`. It is the
only source of page expectations; nothing is inferred from prose.

`PdfPageRendererPort` splits a cheap `describe()`, returning
`PdfRendererDescription` (name, version, DPI, image format) for the binding,
from `render()`, which returns `RenderedPage` values in stable page order.
`PdfiumPageRenderer` is the local adapter, built on the pypdfium2 dependency
pdfplumber already installs, at 150 DPI producing PNG.

`ResumeVisualQAFindingType` covers `PAGE_COUNT_MISMATCH`,
`UNEXPECTED_PAGE_COUNT`, `BLANK_PAGE`, `CONTENT_MISSING`, `CONTENT_CLIPPED`,
`ELEMENT_OVERLAP`, `TEXT_TOO_SMALL`, `EXCESSIVE_DENSITY`,
`EXCESSIVE_WHITESPACE`, `BROKEN_GLYPH`, `INCONSISTENT_ALIGNMENT`,
`UNREADABLE_LAYOUT` and `AGENT_OUTPUT_UNRELIABLE`. `AGENT_FINDING_TYPES`
restricts what an Agent may report; `ADVISORY_FINDING_TYPES` (density,
whitespace, alignment) is the only non-blocking set, and severity is derived
from the type rather than supplied, so an Agent cannot downgrade a defect.
Results persist below
`state/preparation/resume-visual-qa/<subject-key>/`.

#### `revise_resume_layout()`

```text
revise_resume_layout(
    ReviseResumeLayoutCommand(
        subject_id,
        resume_visual_qa_result_id,
        explicit now,
    ),
    visual_qa_repository,
    compilation_repository,
    latex_version_repository,
    provenance_repository,
    revision_record_repository,
    application_plan_repository,
    draft_repository,
    renderer: PdfPageRendererPort,
    agent: ResumeLayoutRevisionAgentPort,
    metadata: ResumeLayoutRevisionAgentMetadata,
    compile_step: LayoutRevisionCompileStep,
    review_step: LayoutRevisionReviewStep,
    revision_repository,
    policy: ResumeLayoutRevisionPolicy | None,
    home,
) -> ReviseResumeLayoutResult
```

`LayoutRevisionCompileStep` and `LayoutRevisionReviewStep` are the only way
this Slice reaches compilation and visual QA. Callers bind them to
`compile_resume_latex()` and `review_resume_visual_qa()` with their own
compiler, renderer, QA Agent and repositories, so the orchestrator holds no
copy of that logic.

`LatexBuildProvenance` is the minimal backward-compatible extension that
makes this possible: a runtime-checkable protocol exposing the version,
lineage, template and Draft/fact-QA bindings a build produced, plus
`build_provenance_binding`. `ResumeLatexConstructionRecord` satisfies it
unchanged through a property alias, and `ResumeLayoutRevisionRecord`
satisfies it too, so P2a7 and P2a8a accept either without knowing which
Slice produced the version. `CompositeLatexBuildProvenanceRepository` routes
a lookup to the right store by record-ID prefix.

`ResumeLayoutRevisionPolicy` is versioned as
`resume-layout-revision-policy-v1` with `max_attempts=3`,
`min_font_size_pt`, `max_font_size_pt` and `min_margin_inches`.
`validate_revised_layout()` is the deterministic gate; attempt outcomes are
`PASSED`, `REVISION_REQUIRED`, `AGENT_OUTPUT_REJECTED`,
`RENDER_UNAVAILABLE`, `VERSION_REGISTRATION_FAILED`, `COMPILATION_STOPPED`,
`VISUAL_QA_DEFERRED` and `VISUAL_QA_FAILED`. Runs persist below
`state/preparation/resume-layout-revisions/runs/<subject-key>/` and
provenance records below the sibling `records/<subject-key>/`.

#### `publish_prepared_resume()`

```text
publish_prepared_resume(
    PublishPreparedResumeCommand(
        subject_id,
        application_plan_id,
        explicit now,
        resume_visual_qa_result_id | resume_layout_revision_run_id,
    ),
    application_plan_repository,
    draft_repository,
    fact_qa_repository,
    latex_version_repository,
    compilation_repository,
    visual_qa_repository,
    layout_revision_repository,
    material_repository,
    home,
) -> PublishPreparedResumeResult
```

Exactly one source identifier is accepted; both is
`SOURCE_SELECTION_AMBIGUOUS` and neither is `SOURCE_SELECTION_MISSING`. The
revision path resolves the run's own `final_visual_qa_result_id`,
`final_latex_version_id` and `final_compilation_record_id`, and requires
`final_status` to be the successful status with a passing last attempt.

The result carries two separate diagnosis fields.
`PreparedResumeMaterialNotReadyReason` explains a `NOT_READY` outcome —
`VISUAL_QA_NOT_PASSED`, `REVISION_RUN_NOT_SUCCESSFUL`,
`FACT_QA_NOT_PASSED`, or a `*_BINDING_MISMATCH`.
`PreparedResumeMaterialFailureReason` explains a `FAILED` outcome, including
`PDF_UNREADABLE`, `PDF_HASH_DRIFT` and `PDF_INVALID`. Only `CREATED` and
`UNCHANGED` carry a material.

`PreparedResumeMaterial` records `material_role=RESUME`, the plan and job
revision, Draft, passed fact QA, final LaTeX version, compilation binding,
the existing `pdf_reference` with its SHA-256, byte size and page count, the
final passed Visual QA and any successful revision run.
`find_current_for_plan()` orders by stored `published_at` then material ID,
so resolution never depends on directory traversal or mtime. Materials
persist below `state/preparation/prepared-resume-materials/<subject-key>/`;
reads and writes re-verify the referenced PDF, so artifact drift turns the
record into `INTEGRITY_FAILURE`.

#### `assemble_plan_material_manifest()`

```text
assemble_plan_material_manifest(
    AssemblePlanMaterialManifestCommand(
        subject_id,
        application_plan_id,
        prepared_resume_material_id,
        explicit now,
    ),
    application_plan_repository,
    prepared_resume_repository,
    manifest_repository,
    home,
) -> AssemblePlanMaterialManifestResult
```

`PlanMaterialManifest` is distinct from the legacy `MaterialManifest` in
`core/materials.py`: different name, module, contract version and storage
location, with no change to `load_material_manifest()` or
`build_tier_materials()`. A test asserts the two types and their symbols stay
separate.

Each v2 `PlanMaterialEntry` carries a content-derived `entry_id`, the material
role, the prepared material ID, the managed artifact reference, SHA-256 and
actual byte size, the media type, the page count, and a `provenance_type` of
`PREPARED_RESUME_MATERIAL` with the source record ID and a hash of that
record's own content, computed here by `prepared_material_content_hash()`
without altering P2a9.

Completeness is deliberately three separate things, never one boolean:
`included_roles` lists what is present, `assembly_state` is `RESUME_ONLY`,
and `resume_prepared` is exposed separately from
`complete_application_material_prepared`, which stays False until cover
letters and answers exist. Gate A has no representation in this contract.
`PlanMaterialManifestNotReadyReason` explains a `NOT_READY` outcome, while
`PlanMaterialManifestFailureReason` explains a `FAILED` one, including
`ARTIFACT_UNREADABLE`, `ARTIFACT_HASH_DRIFT` and `ARTIFACT_INVALID`.
Manifests persist below
`state/preparation/plan-material-manifests/<subject-key>/`; reads and writes
re-verify every referenced artifact. Explicit v1 parsing omits byte size and
retains the original entry/manifest serialization and identity; explicit v2
parsing requires it.

#### `include_cover_letter_in_plan_material_manifest()`

```text
include_cover_letter_in_plan_material_manifest(
    IncludeCoverLetterInPlanMaterialManifestCommand(
        subject_id,
        application_plan_id,
        plan_material_manifest_id,
        prepared_cover_letter_material_id,
        explicit now,
    ),
    application_plan_repository,
    manifest_repository,
    prepared_cover_letter_repository,
    home,
) -> IncludeCoverLetterInPlanMaterialManifestResult
```

The operation returns `CREATED`, `UNCHANGED`, `NOT_READY` or `FAILED`. It
preserves the input RESUME entry exactly and appends one
`PREPARED_COVER_LETTER_MATERIAL` entry in fixed order. Before assembly it
validates Plan, prior-manifest and publication provenance, then re-reads the
managed cover-letter PDF to verify containment, regular-file status,
signature, actual-byte hash, size and page count.

The v2 two-entry identity adds prior manifest ID/content hash, preserved Resume
entry hash, prepared cover-letter material ID/content hash, PDF hash and byte
size, and ordered entry hashes. These fields are conditionally absent from
Resume-only serialization. Historical v1 records keep their original IDs and
canonical hashes; P2b2e refuses a v1 prior instead of upgrading it in place.
The resulting assembly state is `RESUME_AND_COVER_LETTER`, while
`complete_application_material_prepared` remains false and no Answers, Gate,
approval, submission or ATS state is serialized.

#### `create_cover_letter_evidence_snapshot()`

```text
create_cover_letter_evidence_snapshot(
    CreateCoverLetterEvidenceSnapshotCommand(
        subject_id,
        application_plan_id,
        explicit now,
    ),
    application_plan_repository,
    selection_repository,
    candidate_repository,
    projection_repository,
    snapshot_repository,
) -> CreateCoverLetterEvidenceSnapshotResult
```

`core/cover_letter_evidence.py` mirrors P2a4b's structure over the same
`SourceResumeProjection` input, but defines its own
`CoverLetterEvidenceScope.COVER_LETTER`, sensitivity and verification-status
enums rather than importing anything from `core/candidate_evidence.py`. A
test parses the module's AST and asserts no `candidate_evidence` import
exists. Every evidence and snapshot ID binds this scope, so a cover-letter
evidence ID is never equal to a resume-tailoring evidence ID even when built
from the identical source block and projection.

Validation and idempotency mirror P2a4b: the complete
Plan/Selection/Candidate/Projection binding is revalidated fail-closed, the
latest complete Selection and Projection are chosen by domain timestamp with
stable ID tie-break, an empty projection returns
`DEFERRED_NO_EVIDENCE`, and snapshot identity excludes time so identical
input replays `UNCHANGED`. Snapshots persist below
`state/preparation/cover-letter-evidence-snapshots/<subject-key>/`.

#### `draft_cover_letter()`

```text
draft_cover_letter(
    DraftCoverLetterCommand(
        subject_id,
        application_plan_id,
        cover_letter_evidence_snapshot_id,
        explicit now,
    ),
    application_plan_repository,
    job_repository,
    evidence_snapshot_repository,
    agent: CoverLetterAgentPort,
    metadata: CoverLetterAgentMetadata,
    draft_repository,
) -> DraftCoverLetterResult
```

`CoverLetterAgentPort.generate()` receives a `CoverLetterAgentContext`
carrying only `CoverLetterJobContext` (JD fields plus revision/content
hash), `COVER_LETTER`-scoped `CoverLetterEvidenceView` items, the Plan's
verbatim `user_preparation_instructions`, and the static
`COVER_LETTER_DRAFT_AGENT_POLICY` text at
`COVER_LETTER_DRAFT_POLICY_VERSION`. It must return a typed
`CoverLetterAgentOutput` (greeting, ordered `CoverLetterParagraphProposal`
items, closing, rationale); an untyped or missing return defers as
`DEFERRED_NEEDS_HUMAN`.

`_validate_agent_output()` is the deterministic gate: every cited evidence
ID must exist in the snapshot with `COVER_LETTER` scope; every JD alignment
reference must be a verbatim substring of `job.description`; every checkable
token (a number, or a capitalized word after the first) in a
`QUALIFICATION`/`MOTIVATION` paragraph must appear in the paragraph's own
cited evidence, and a token that appears only in the JD is rejected as an
unproven candidate claim; a regex rejects bracket/brace placeholders, `TBD`
and generic stand-ins anywhere in the letter. The tokenizer
(`_WORD_PATTERN = r"[A-Za-z0-9][A-Za-z0-9+#/]*"`) deliberately excludes
trailing punctuation from a token, avoiding the false-positive P2a4c/P2a5's
tokenizer produces on a sentence-final word before a period.

Drafts persist below
`state/preparation/cover-letter-drafts/<subject-key>/`.

#### `review_cover_letter_fact_qa()`

```text
review_cover_letter_fact_qa(
    RunCoverLetterFactQACommand(
        subject_id,
        application_plan_id,
        cover_letter_evidence_snapshot_id,
        cover_letter_draft_id,
        explicit now,
    ),
    application_plan_repository,
    job_repository,
    evidence_snapshot_repository,
    draft_repository,
    agent: CoverLetterFactQAAgentPort,
    metadata: CoverLetterFactQAAgentMetadata,
    result_repository,
) -> RunCoverLetterFactQAResult
```

Independent of `draft_cover_letter()`: this module never imports its
private `_validate_agent_output()` or any other private helper from
`core.cover_letter_draft`. Every check is re-derived from the typed
`CoverLetterDraft`, `CoverLetterEvidenceSnapshot` and `JobPosting` objects
it reads itself. The complete Plan/JobPosting/EvidenceSnapshot/Draft
binding (subject, job revision/content hash, snapshot ID/hash, and the
Draft's own recorded snapshot/job binding) is revalidated first;
`BLOCKED_BINDING_MISMATCH` short-circuits before any Result identity is
computed and before the Agent is reachable.

`_deterministic_findings()` runs unconditionally and covers: unknown or
wrong-scope evidence IDs; JD alignment references that are not verbatim
`job.description` substrings; `QUALIFICATION`/`MOTIVATION` paragraphs
without cited evidence; checkable tokens (numbers, or capitalized words
after the first, excluding the pronoun "I") in a candidate-fact paragraph
that trace to neither cited evidence nor the JD; a JD-only token used to
prove a candidate trait; a general paragraph asserting a fact absent from
the JD (description, title or company) and its own cited evidence; an
unverified name in the greeting; any placeholder; and duplicated paragraph
order/identity. Any `BLOCKING`-severity finding here returns
`BLOCKED_UNSUPPORTED_CLAIM` immediately, with the Agent never called.

Only when no deterministic finding is `BLOCKING` does
`CoverLetterFactQAAgentPort.review()` run, at most once per new binding.
Its `CoverLetterFactQAAgentContext` carries only the current
greeting/`CoverLetterFactQAParagraphView` tuple/closing,
`COVER_LETTER`-scoped `CoverLetterFactQAEvidenceView` items,
`CoverLetterJobContext`, and the static `COVER_LETTER_FACT_QA_AGENT_POLICY`
text at `COVER_LETTER_FACT_QA_POLICY_VERSION`. It must return a typed
`CoverLetterFactQAAgentOutput` (a `PASSED`/`BLOCKED`/`UNCERTAIN` verdict
plus typed findings restricted to `AGENT_ELIGIBLE_FINDING_TYPES` —
`RESPONSIBILITY_LEVEL_EXAGGERATION`, `DEPLOYMENT_STAGE_EXAGGERATION`,
`UNSUPPORTED_IMPACT_OR_CAUSALITY`, `FABRICATED_COMPANY_CONNECTION`,
`SEMANTIC_SCOPE_OVERREACH`); a `BLOCKED` verdict requires at least one
`BLOCKING` finding and vice versa, enforced by the output dataclass itself.

`_agent_findings()` independently re-verifies every returned finding's
`paragraph_id` against the current Draft's paragraph IDs, every
`evidence_id` against the current snapshot's `COVER_LETTER`-scoped items,
and every `jd_references` entry as a verbatim `job.description` substring.
An unknown reference, an `UNCERTAIN` verdict, or an untyped/missing return
all resolve to `DEFERRED_NEEDS_HUMAN` — no Result is persisted, so a later
manual re-run may call the Agent again, and the Draft is never modified.

A validated Agent verdict (`PASSED` or `BLOCKED`) is combined with the
deterministic findings and persisted as one immutable
`CoverLetterFactQAResult`. Its identity binds the Draft ID and content
hash, job revision/content hash, evidence snapshot ID/hash, and QA
Agent/prompt/model/policy/contract versions, excluding time — a completed
binding replays `UNCHANGED`, and a changed Draft/JobPosting/
EvidenceSnapshot/QA-version always creates a new immutable Result. Results
persist below
`state/preparation/cover-letter-fact-qa-results/<subject-key>/`.

#### `publish_prepared_cover_letter()`

```text
publish_prepared_cover_letter(
    PublishPreparedCoverLetterCommand(
        subject_id,
        application_plan_id,
        cover_letter_fact_qa_result_id,
        explicit now,
    ),
    application_plan_repository,
    job_repository,
    draft_repository,
    fact_qa_repository,
    template_provider: ManagedCoverLetterTemplateProvider,
    compiler: LatexCompilerPort,
    material_repository,
    home,
) -> PublishPreparedCoverLetterResult
```

The service revalidates the complete subject/Plan/JobPosting/Draft/Fact-QA
lineage and requires the named typed result to be `PASSED`. `BLOCKED`, an
absent/deferred result, or a binding mismatch returns `NOT_READY` before
template lookup, compiler description, source persistence or compilation.

`DefaultManagedCoverLetterTemplateProvider` returns exactly
`managed-cover-letter-one-page-v1`. `render_cover_letter_latex()` is
deterministic and Agent-free: it consumes only the Draft greeting, ordered
paragraphs and closing, escapes each input character once, and emits each
paragraph once under its stable ID marker. Template and rendered-source
validation reject shell escape, arbitrary TeX file I/O, external programs,
unallowlisted packages and unmanaged file dependencies. The sole managed
package is TeX Live's `fontenc[T1]`, which keeps escaped ASCII glyphs
recognizable in the PDF text projection. The actual UTF-8 bytes are stored at
`state/preparation/cover-letter-latex-sources/<subject-key>/<sha>.tex`.

Compilation calls the P2a7 `LatexCompilerPort.describe()/compile()` contract
without reproducing process or sandbox logic. Compiler absence maps to
`DEFERRED_COMPILER_UNAVAILABLE`; any compile error/timeout/invalid compiler
output maps to `DEFERRED_COMPILATION_ERROR`; more than one parsed page maps
to `DEFERRED_LAYOUT_OVERFLOW`. No branch edits the Draft or retries.

Before PDF persistence, the service requires a valid signature, bounded
actual bytes, at least one parsed page and an exact normalized visible-text
projection of the Draft greeting, every paragraph in order, and closing.
Missing, duplicated, placeholder or unknown visible content fails closed.
The accepted one-page PDF is stored at
`state/preparation/compiled-cover-letters/<subject-key>/<sha>.pdf`.

`PreparedCoverLetterMaterial` records both artifact references/hashes and
sizes, PDF page count and projection hash, template ID/version/hash,
compiler engine/version/flags/policies, Plan/job/evidence/Draft/PASSED-QA
lineage, `COVER_LETTER` role, publication policy/contract,
`material_content_hash` and immutable `published_at`. Publication identity
excludes time; lookup occurs before `compile()`, so a completed binding
replays `UNCHANGED` with the original timestamp and no compiler or duplicate
artifact. Repository reads re-hash and re-parse both managed artifacts and
fail closed on conflicts, corruption, drift or cross-subject access.

#### End-to-end automation cycle

```text
run_automation_cycle(
    RunAutomationCycleCommand(
        subject_id,
        invocation_id,
        now,
        max_reprioritizations,
        max_plan_creations,
        max_preparations,
        max_executions,
        composition_binding,
    ),
    priority_refresh=P1d3 public callable,
    plan_creation=P2a1b public callable,
    preparation=P2b6 public callable,
    execution=P2c9 public callable,
    repository=AutomationCycleRunRepository,
) -> RunAutomationCycleResult
```

The four public batch calls are strictly serial and receive the same subject
and explicit timestamp. Zero-budget stages are typed skips. Every other stage
is called at most once; batch failure, defer, Human Attention and uncertainty
are summarized without stopping later stages or triggering retry.

Cycle identity binds the caller-supplied invocation ID, four budgets,
composition binding, four batch contract versions and the cycle contract
version. Time is audit-only. A matching persisted invocation returns
`UNCHANGED` before all batch calls. Private Home storage is subject-isolated,
immutable and hash-validated.

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

`MappingResponse.canonical_key` is the shared
`CanonicalApplicationAnswerKey`, not a mapper-local Literal. Response status
is derived from the V1 definition: ordinary facts and material semantics are
`mapped`; legal/compensation/sensitive facts, voluntary-demographic fields
and attestation/consent/signature are `needs_review`; `unknown` is
`unsupported`. The legacy mapper output `phone_number` is normalized at the
response boundary to canonical `phone`; it is never retained internally.

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
| Conversational URL intake and action resolution | Implemented I1 + I2 + I2b | 45 synthetic reader/store/Discovery/intent-repository cases for extraction, conversion, atomic consumption, durable subject intent, replay, precedence, integrity failures and dependency boundaries; no network claim |
| Accepted Job Intent Source Provenance | Implemented I2c | 3 focused cases preserve fixed v1 bytes/ID/hash and precedence, validate v2 conversational and ordered multi-SearchProfile provenance through restart reads and identity changes, and cover replay/immutable conflict behavior |
| Known Greenhouse board candidate search | Implemented S1a | 43 fake-HTTP/fixture contract, allowlist, matching, ordering, failure and dependency-boundary cases; no live-network claim |
| SearchProfile Contract | Implemented S3a | 4 focused cases cover typed Greenhouse profile creation, Private Home restart recovery, canonical query replay, immutable query/enabled version history, current/enabled deterministic ordering, subject isolation, invalid source/query/refresh rejection and zero side effects; 3 known-board JobSearchRequest compatibility variants pass |
| Manual Full Job Library Refresh | Implemented S3b | 4 focused cases cover all-enabled one-call Search, cross-profile canonical-URL de-duplication with complete source lineage, one Public Read/Discovery per URL, explicit ADD_JOB manual trigger, isolated Search/Reader/Discovery failures, one final bounded P1d3 call, empty-profile NOOP, immutable restart replay with zero downstream calls, all-search-failed status and dependency/lifecycle boundaries |
| SearchProfile Auto-application Intent Policy | Implemented S3c | 4 focused cases cover default zero-intent behavior, explicit auto intent only after successful Discovery, multi-profile one-write provenance, immutable policy replay/versioning, future-only add-only changes, subject isolation and zero planning/preparation/execution dependencies |
| Authenticated Subject Session | Implemented S3d0 | 3 focused cases cover Keychain-backed cookie resolution, client subject-override rejection, typed context, missing/expired/hash-drift safe 401 behavior, credential redaction/storage hashing, legacy health-route compatibility and zero Search/Discovery/Automation/Browser/ATS dependencies |
| Refresh Job Library UI Wiring | Implemented S3d | 3 focused cases cover authenticated subject forwarding, explicit-now/budget/invocation command construction, in-flight duplicate suppression, same-ID S3b replay, COMPLETED/PARTIAL_FAILURE/NOOP safe projections, disabled-running UI state and zero direct Search/Discovery/Priority/Automation/Browser dependencies |
| Continue Automatic Application UI Wiring | Implemented S3e | 3 focused cases cover authenticated subject forwarding, explicit-now/invocation and versioned server-budget command construction, in-flight duplicate suppression, same-ID P2c10a replay, COMPLETED/PARTIAL_FAILURE/NOOP/UNCHANGED projections, Human Attention/defer/uncertain display, independent S3b/P2c10a requests and zero direct batch/Gate/permit/Browser/Engine dependencies |
| Human Attention Inbox UI Wiring | Implemented S3f | 3 focused cases cover one authenticated P2b5 snapshot with concurrent read sharing, order-preserving USER/OPERATOR projection across multiple Plans, typed kind/action/stage/item-ID display, EMPTY/FAILED safe states, unsafe diagnostic redaction, one initial and one post-S3e refresh, no polling and zero write/Preparation/Automation/Browser dependencies; one P2b5 mapping regression passes |
| Conversational Application Answer Resolution | Implemented S3g1 | 3 focused cases cover current USER fact persistence and one P2b4 rerun, plan-scoped attestation with ambiguous-input fail-closure, and immutable replay with zero queue/parser/write/rerun side effects; P2b5 and P2b4 focused regressions pass |
| Conversational Resume / LaTeX Choice Resolution | Implemented S3g2 | 3 focused cases cover deterministic ResumeCandidate selection plus P2a3 override consumption, one-call safe-metadata LaTeX parsing plus P2a6b override consumption and invalid-option rejection, and ambiguous/defer/failure/replay behavior with preserved override/receipt history; 86 focused P2a3/P2a6b/P2b4/P2b5 regressions pass |
| Conversational named-job search | Implemented S1b application boundary | 13 fake-extractor/search-port tests for URL priority, clues, 0/1/many results, TTL, failures and side-effect boundaries |
| Candidate selection to pending action | Implemented S2 | 13 fake-reader/store cases for validation, atomic claim, replay/conflict, failure release and dependency boundaries |
| Conversational/remaining connector Job Discovery | Partial | URL read, named search, candidate selection and add/apply entry exist; Lever search, SearchProfile and product-surface wiring remain |
| Editable Prioritization Policy and preparation admission | Implemented P1a + P1a2 | 31 fake-interpreter, draft/admission validation, approval, version/hash, Private Home compatibility and dependency-boundary cases |
| AI Priority Proposal | Implemented P1b | 35 synthetic-context/fake-agent binding, deterministic-fact, evidence, invariant, failure and dependency-boundary cases; no real-model claim |
| Single-call Priority Agent adapter | Implemented P1b2 | fake-provider and mocked-Responses tests prove one tool-free strict-schema call, system/data separation, metadata ownership, sanitized logging and existing P1b validation; optional synthetic smoke script is excluded from routine tests |
| Priority Validation Gate / formal P0–P3 decision | Implemented P1c | 35 synthetic Gate/repository cases within the 70-case Priority suite; deterministic constraints, reconciliation, binding, schema, immutability and idempotency evidence |
| Single-job Priority orchestration | Implemented P1d1 | 15 synthetic orchestration/read/provider cases; atomic pre-Agent claim, one-call flow, `CREATED`/`UNCHANGED`, changed bindings, subject isolation and failure boundaries |
| Current Priority queue read model | Implemented P1d2 | 14 synthetic cases for typed listing, current/stale/missing/incomplete projection, stale reasons, subject isolation, stable sorting, fail-closed data integrity and zero-write/zero-Agent boundaries |
| Selective batch reprioritization | Implemented P1d3 | 16 synthetic and real-service composition cases for bounded selection, caller/P1d2 order, serial execution, exact-time forwarding, typed aggregation, failure isolation, NOOP and repeated-run zero-extra-Agent idempotency |
| Runnable Application Queue read model | Implemented P1d4 | 17 synthetic cases for direct admission, accepted-intent isolation/integrity, every blocked state, same-snapshot policy use, order preservation and zero-write/zero-execution boundaries |
| Automation-first ApplicationPlan | Implemented P2a1 | 16 synthetic cases for RUNNABLE-only creation, immutable bindings, exact instructions, stable identity/replay, changed inputs, restart reads, fail-closed persistence and zero-Agent/zero-execution boundaries |
| Selective Batch ApplicationPlan Creation | Implemented P2a1b | 4 focused cases cover one fixed P1d4 snapshot, RUNNABLE-only snapshot-order selection, caller-order allowlist de-duplication, execution-count bounds excluding blocked/not-found items, maximum concurrency one, exact subject/time forwarding, explicit-only per-job instructions, isolated P2a1 failure, and P2a1 `UNCHANGED` replay |
| Trusted Resume Candidate Registry | Implemented P2a2 | 15 synthetic cases for managed artifact validation, actual-byte hashing, immutable replay/conflict, subject isolation, trusted summaries, stable restart reads, fail-closed integrity and zero-selection/zero-execution boundaries |
| Automatic Base Resume Selection | Implemented P2a3 | 21 synthetic cases for Plan/Job binding, zero-or-one Agent calls, safe context, deterministic/deferred outcomes, pre-Agent replay, changed bindings, subject isolation, immutable restart reads, conflicts and dependency boundaries |
| Hash-bound Source Resume Projection | Implemented P2a4a | 12 synthetic cases for PDF/DOCX structure, faithful text, stable locators/IDs, replay/restart, parser/artifact changes, unsupported/unreadable documents, subject isolation, immutable conflicts and zero-Agent/OCR/execution boundaries |
| Subject-specific CandidateEvidence Snapshot | Implemented P2a4b | 14 synthetic cases for exact source lineage, conservative trust/scope, binding failures, stable replay/restart, empty evidence, changed Plan/Selection/Projection, subject isolation, immutable conflicts and zero-profile/Agent/QA/execution boundaries |
| Evidence-bound Cover Letter Draft | Implemented P2b2b | 21 synthetic fake-Agent cases for binding fail-closure, bounded single Agent call, restricted Agent context, unevidenced-claim rejection, JD-requirement-as-fact rejection, placeholder rejection (greeting/closing/paragraph), insufficient-evidence deferral, illegal-output deferral (four variants), Agent unavailability, replay, restart reads, conflicts and zero-FactQA/rendering/manifest/execution boundaries |
| Evidence-bound Cover Letter Fact QA | Implemented P2b2c | 23 synthetic fake-QA-Agent cases for binding mismatch (plan/job/snapshot/draft, zero Agent calls), deterministic blocking (unknown evidence, unsupported claim, JD-requirement-as-fact — all zero Agent calls), Agent-blocked semantic exaggeration (responsibility-level, fabricated company connection), restricted Agent context, illegal Agent-finding references (three variants) and uncertain/untyped Agent output all deferring without persisting, draft non-mutation, replay, new-Result-on-version-change, restart reads, conflicts and zero-rendering/manifest/execution boundaries |
| Cover Letter Document Publication | Implemented P2b2d | 31 synthetic/controlled cases for typed publication, deterministic exactly-once rendering, one-pass escaping, template/source capability rejection, blocked/missing/mismatched Fact QA with zero compiler calls, compiler absence/errors, one-page overflow, invalid PDFs, missing/duplicated/placeholder/unknown visible text, replay before compile, changed Draft/QA/template/compiler identities, restart hashes, artifact and record drift, subject isolation, zero-Agent/Manifest/Browser/ApplicationEngine boundaries, and optional real sandboxed-pdflatex text fidelity |
| Plan Manifest Cover Letter Inclusion | Implemented P2b2e | 16 synthetic cases for ordered RESUME+COVER_LETTER assembly, field-for-field Resume preservation, publication/PDF provenance, prior-manifest lineage identity, replay and already-included idempotency, changed-cover-letter history, binding mismatch, missing/corrupt prior manifest, PDF hash/signature/size/page drift, repository conflict without overwrite, restart/current resolution, subject isolation, artifact immutability and zero generation/compilation/Gate/Browser/ATS/ApplicationEngine boundaries |
| Unified Canonical Application Answer Taxonomy | Implemented P2b3a | 11 synthetic contract cases for complete typed metadata, contact/legal/demographic/attestation distinctions, phone and vault alias normalization, fail-safe UNKNOWN, sensitive mapper review status, typed ApplicationBundle answers, out-of-taxonomy rejection, explicit legacy conversion, stable serialization/hash, shared caller types and zero candidate/execution capability |
| Application Answers Preparation | Implemented P2b3b | 21 synthetic Private Home cases for authoritative fact metadata/snapshot identity, alias normalization, unsupported UNKNOWN, strict value types, no high-stakes inference, demographic choice/decline policy, immutable attestation boundaries, skip versus human-required states, Plan restrictions, salary confirmation, expired/job-scoped exclusion, conflicts without safe-answer loss, no-trusted-fact and human deferrals, subject/binding failures, replay, changed-binding history, restart/current reads, corruption, isolation and zero FormIR/SemanticMapper/Browser/ATS/Gate/ApplicationEngine capability |
| Single-job Automated Application Preparation | Implemented P2b4 | 20 synthetic application-layer cases for exact serial order and common inputs, CREATED/UNCHANGED continuation, Visual-QA pass skip and revision-final-lineage publication, Resume and Cover-Letter defer short-circuiting, preserved completed roles, blocking-answer attention, typed/exception failures without rollback, completed zero-call replay, changed upstream binding history, immutable restart reads, corruption and subject isolation, missing-output contract failure, dependency-source boundary, plus one real P2b3b public-call composition |
| Async Preparation Stage Invocation | Implemented P2b4f | Focused cases cover mixed synchronous/awaitable canonical stages with exactly-once invocation and maximum concurrency one, async stop/exception short-circuiting, cancellation propagation, zero-call completed replay with unchanged Run/lineage identity, and direct serial P2b6 awaiting without event-loop or thread bridges |
| Typed Preparation Stop Reason Contract Foundation | Implemented P2b4a | 4 focused cases cover typed completed/deferred/failed v2 round trips, closed stage/enum/version/outcome validation, unchanged v1 Run bytes/hash with explicit `LEGACY_UNTYPED` restart reads, Base LaTeX reason adaptation, and mixed typed/legacy P2b4 serial consumption; 72 focused P2b4/P2b5/Base-LaTeX/S3g regressions pass |
| Resume Semantic Stage Stop Reason Migration | Implemented P2b4b | 4 focused cases cover exhaustive closed mappings for all five stage failure enums, distinct no-resume/no-evidence and unsafe-output deferrals, typed unsupported/unreadable projection stops, and separation of unsupported-claim safety blocks from QA integrity failures; affected stage and P2b4a compatibility suites pass |
| Cover Letter / Application Answers Stop Reason Migration | Implemented P2b4c | 4 focused cases cover exhaustive closed mappings for all four stage failure enums, no-evidence and unsafe-output deferrals, distinct fact/choice/attestation answer blockers, unsupported-claim versus QA-integrity separation, and proof that the four adapters contain no legacy write path |
| Publication / Manifest Stop Reason Migration | Implemented P2b4d | 4 focused cases exhaust all four stage adapter mappings, preserve publication compiler/layout deferrals versus binding/hash/version/persistence/integrity failures, reject plain-string/wrong-stage/version/outcome envelopes, and prove the four target modules have no legacy stop path; affected idempotency tests verify CREATED/UNCHANGED adaptation and mixed typed/legacy plus historical-v1 regressions pass |
| Technical Stage Stop Reason Migration | Implemented P2b4e | 4 focused cases exhaust the four closed source-to-stage reason sets, adapt all Construction/Compilation stopped outcomes, preserve persisted Visual QA/Layout lineage, distinguish compiler content failure from unavailable/timeout infrastructure, keep renderer/Agent/layout/integrity boundaries separate, prove full-typed P2b4 composition, and retain exact historical-v1 reads; no nonexistent no-progress/duplicate branch was invented |
| Layout Downstream Compilation Stop Lineage | Implemented P2b4e1 | 4 focused cases bind content-error and compiler-unavailable stops to distinct typed child result IDs/envelopes, fail closed on parent-child binding drift, preserve exact legacy-incomplete attempt reads without detail inference, and retain immutable replay |
| Compilation Attempt Binding and Source Resolution Lineage | Implemented P2b4e2a | 4 focused cases prove one pre-run invocation binding is shared by all stage requests and the final Run, resolved Compilation stops bind exact Construction/LaTeX/source-hash identity, invalid/missing early sources use closed unresolved states without fabricated hashes, attempt/subject/Plan/state drift fails closed, and stage-result v3 round trips while historical v1/v2 remain explicit |
| Compilation Stopped Source Lineage | Implemented P2b4e2 | 4 focused cases persist distinct resolved records for unmanaged dependencies and compilation errors, preserve resolved infrastructure stops and hash-free unresolved early stops, validate subject/Plan/invocation/attempt/reason/source/reference bindings, prove stage-result v3 references and immutable replay, and keep repository failures non-recursive while legacy/synthetic results receive no fabricated reference |
| Publication Stopped Source Lineage | Implemented P2b4d1 | 4 focused contract cases plus affected publication regressions bind Resume/Cover Letter Fact QA, Resume Visual QA, Resume Layout Revision and Cover Letter overflow to distinct immutable source identities; stage/material/hash drift fails closed, deterministic replay is stable, public projection is bounded, and no path, stderr, diagnostic text or historical reconstruction participates |
| Current Human Attention Queue Read Model | Implemented P2b5/P2b5a | Synthetic read-model cases cover typed deferred items, clean completed omission, per-blocking AnswerSet expansion with optional skips excluded, attestation/fact/choice/correction/replacement/operator mappings, failed and unclassified reasons forced to operator, superseded-item disappearance, repository current ordering, stable item/snapshot hashes across restart/mtime/reversed listing, priority/audience/kind ordering, subject isolation, missing/mismatched AnswerSet fail-closure, and byte/mtime-proven zero-write dependency boundaries |
| Human Attention Semantic Classification | Implemented P2b5a/P2b5a2 | 4 focused cases prove complete 16/16 technical coverage, content-correction versus compiler/renderer/operator boundaries, Layout child-lineage classification with damaged-lineage fail-closure, zero approvable Visual QA mappings, mapping-v3 identity, and unchanged fact/choice/attestation plus legacy behavior |
| Fact QA Finding-Level Attention Projection | Implemented P2b5d | 4 focused cases split direct Resume/Cover Letter and P2b4d1 Publication Fact QA blockers into one stable item per exact blocking finding, validate subject/Plan/material/result/hash/version/finding bindings as an atomic collection, preserve formal finding order and replay identity, and fail closed to one operator item without claim-text or index inference |
| Selective Batch Application Preparation / Assembly Lineage | Implemented P2b6/P2b6a | Focused synthetic and real-composition cases cover P0-to-P3 ordering, bounded serial execution, one fixed attention snapshot, COMPLETED/UNCHANGED/deferred/failure aggregation, exact Run/Manifest/AnswerSet lineage on completion and replay, zero-call unchanged replay, missing/cross-subject lineage fail-closure with later-item continuation, and absence of lineage on deferred/failed/skipped items |
| Preparation-to-Execution Material Contract Migration | Implemented P2c0 | 15 compatibility cases covering fixed v1 Resume-only and Resume+Cover Letter bytes/IDs/hashes, explicit unavailable-size projection, v1 restart reads without rewrite, v2 actual byte-size writes for both roles, byte-size identity/hash participation, invalid and missing-size fail-closure, v2 Resume preservation, typed v1-prior rejection, unchanged legacy MaterialBundle digest/text, typed managed Cover Letter PDF carriage through ApplicationBundle, strict reference validation and zero adapter/Engine selection behavior |
| Plan-scoped Application Bundle Assembly | Implemented P2c1 | 20 synthetic cases covering execution-compatible bundle construction, exact canonical answers and managed material carriage, factory-bound prepared inputs, blocking and non-blocking unresolved handling, subject/job/Manifest/AnswerSet binding fail-closure, incomplete and v1 Manifest rejection, PDF absence/hash/size/signature/page-count/symlink/cross-subject failures, immutable replay and changed-AnswerSet history, restart and mtime-independent current reads, record corruption, factory drift rejection and zero Preparation/SemanticMapper/Browser/ATS/Gate/Engine imports |
| Execution Profile Consumer Taxonomy Migration | Implemented P2c1d1a | 4 focused synthetic cases cover the closed identity/contact field set, mixed-profile rejection, canonical-answer and managed-material separation, injected Workday runtime/job context, dynamic-question Human Attention, explicit legacy compatibility, and unchanged historical bundle identity |
| Candidate Identity Fact Lineage Migration | Implemented P2c1d1b | 4 focused synthetic cases cover exact user-confirmed source lineage and replay, proposal/legacy exclusion from current, source-status eligibility, deterministic normalization, monotonic versions, cross-process SQLite CAS with one stale writer, supersede lineage, fork conflict and hash-drift fail-closure, stable PII-minimal indexing, 0600 storage and unchanged CandidateVault legacy projection |
| Verified Application Execution Profile | Implemented P2c1d1 | 4 focused synthetic cases cover exact Plan/Job/subject and per-field fact lineage, required/optional closed-registry projection, pure Bundle-compatible mapping, missing/cross-subject/hash-drift fail-closure without partial output, deterministic replay, new snapshot on current-fact change, immutable historical reads, time-independent identity, subject isolation and PII/path-safe results |
| Candidate Information Source Registry | Implemented C1a | 4 focused synthetic cases cover actual-byte PDF/DOCX/PPTX/PNG/JPEG/UTF-8 detection, content/display-name-independent replay, deterministic HTTPS and statement canonicalization without network access, cross-subject isolation, invocation conflict, direct P2c1d1b source projection, path/archive/macro/image/binary/size rejection, transactional rollback, payload-drift fail-closure and payload-free metadata reads |
| Candidate Source Deterministic Projection | Implemented C1b | 4 focused synthetic cases cover C1a-only PDF/DOCX/PPTX/text/image/statement projection, stable blocks/locators/assets and replay, immutable HTML capture without scripts, production loopback SSRF denial, exact source/capture lineage, invocation conflict and cross-subject isolation, asset drift fail-closure, payload-only public reads and metadata/diagnostic text/path redaction |
| Agent-assisted Candidate Fact Proposal | Implemented C1c | 4 focused synthetic cases cover bounded exact-C1b text/image snapshots, one-call structured extraction, closed-field and exact block/asset evidence validation, deterministic normalization/deduplication with independent conflicting values, immutable source-sensitive proposal identity, zero-call replay, invocation conflict, resolver-enforced image/isolation capability, subject-scoped PII-minimal reads and zero verified-fact/current-index writes |
| Candidate Fact Review and Verification UI | Implemented C1d | 4 focused synthetic cases cover proposal acceptance and edited USER_CONFIRMATION lineage, conflict display, zero-write keep/reject behavior, expected-current CAS under concurrent drift, missing-required typed entry, bounded text/image preview with cross-subject denial, deterministic child-invocation receipt recovery, invocation conflict and authenticated route rejection of client-supplied subject/binding fields |
| Selective Bundle Assembly and Cycle Handoff | Implemented P2c10b | Focused cases prove fixed P2b6 snapshot consumption, exact Run/Manifest/AnswerSet lineage transfer to serial public P2c1 calls, deterministic order, completed/unchanged assembly, invalid/deferred non-budget-consuming skips, failure continuation, same-cycle P2c9 ordering, zero-budget P2c9 continuation, five-service zero-call replay, and byte-stable historical four-stage reads |
| Recoverable Application Bundle Envelope | Implemented P2c1b | 7 focused synthetic cases covering complete typed material/answer/profile/policy recovery, shared canonical hash equality, subject and AssemblyRecord hash fail-closure, immutable replay and conflict protection, persisted-payload corruption, restart recovery, historical `NOT_FOUND`, and zero Manifest/AnswerSet/factory/Gate/Browser/ATS/Engine dependency |
| Canonical Resume / Cover Letter Upload Mapping | Implemented P2c2 | 9 focused synthetic cases covering legacy Resume upload, exact two-role selection and non-crossing, absent/required/optional Cover Letter behavior, hash/size/signature/symlink/containment fail-closure, typed Base validation failure, unknown/ambiguous controls, at-most-once shared fill, taxonomy classification and representative Greenhouse/Ashby inheritance; 167 affected execution/Workday compatibility tests pass with 14 environment skips, plus 2 sanitized Chromium adapter cases pass separately |
| Plan-scoped Gate A and Non-submit Engine Integration | Implemented P2c3/P2c5a provenance extension | 9 focused synthetic cases covering authorized one-shot Review persistence with a typed consumed-Gate-A reference, human Gate A pre-Browser defer, exact recovered materials/answers and hard non-submit arguments, Browser defer without retry, runtime sensitive-input handoff, submission-evidence fail-closure, zero-call immutable replay that reuses the reference, managed-artifact drift plus restart reads, and Workday special-route Bundle carriage |
| Plan-scoped Gate B Submission Authorization | Implemented P2c4 | 6 focused synthetic cases covering explicit-user authorization, default human defer, formally autonomous authorization, exact attestation/consent/signature handoff, validation/binding/submission-boundary blocking, immutable replay and changed authorization history with zero Browser/Engine/ATS/ledger dependency; 60 focused and Gate-B/review regressions pass |
| Plan-scoped Submission Permit Contract Migration | Implemented P2c5a | 4 focused cases plus Foundation Permit and P2c3 regressions cover byte-compatible legacy bindings, exact plan/subject/authorization/execution/adapter/action scope validation, ledger-verifiable Gate A consumption references, non-secret signer metadata, subject-isolated opaque token recovery, token drift fail-closure, and zero submission-permit issuance or Browser/Engine/ATS/submit capability |
| Plan-scoped Submission Permit Issuance | Implemented P2c5b | 4 focused cases cover exact AUTHORIZED issuance with token-only opaque storage, unauthorized/binding/Gate-A/submission-state fail-closure, validator rejection after every plan-scoped binding mutation, zero-issue unexpired replay, v1 expiry requiring reauthorization, issuer/store/record failure isolation, and zero Browser/Engine/ATS/submission-intent/submit capability; 26 focused P2c3–P2c5b and Foundation Permit regressions pass |
| Authorized Submission Execution and Evidence | Implemented P2c6 | 5 focused cases cover one verified submit with intent and bounded evidence, expired/consumed/binding/token rejection before Browser, changed-Review and runtime-input defer before consumption, adapter-callback point-of-no-return consumption, successful zero-call replay, consumed-but-unverified uncertainty with no retry, and bearer-token exclusion; 100 focused and affected P2c3–P2c6, Foundation Permit, Engine, shared-adapter and Workday regressions pass with 47 environment skips |
| Single-job Automated Application Execution | Implemented P2c7 | 5 focused cases cover exact P2c3→P2c4→P2c5b→P2c6 order with one shared explicit timestamp and maximum concurrency one, Gate A and explicit-user authorization deferrals with zero later calls, failure prefix preservation, immutable restart recovery, terminal uncertainty with no retry, and completed/uncertain zero-call replay |
| Current Application Execution Queue | Implemented P2c8 | 5 focused cases cover READY without a Run, permanent SUBMITTED across a newer Assembly, terminal uncertainty ahead of later nonterminal history, old deferred isolation from a new current Assembly, deterministic status/priority ordering, stable item/snapshot hashes across changed evaluation time, mtime and reversed repository reads, plus byte/mtime-proven zero writes; 25 Assembly-current and ExecutionRun repository regressions pass |
| Selective Batch Application Execution | Implemented P2c9 | 4 focused cases cover READY-only snapshot-order execution with maximum concurrency one, deferred/failed/submitted/uncertain typed skips, per-Plan defer/failure/uncertainty isolation with later execution continuation, caller-order allowlist de-duplication, execution-count bounds that exclude skips/not-found, per-Plan Gate A inputs, one queue read per batch and P2c7 `UNCHANGED` replay; 3 focused P2c7/P2c8 terminal regressions pass |
| End-to-end Automation Cycle | Implemented P2c10a/P2c10b | 4 focused cycle cases cover exact P1d3→P2a1b→P2b6→Bundle→P2c9 serial order, shared subject/time and independent budgets, stage failure/defer/uncertainty continuation, zero-budget typed skips with P2c9 continuation, immutable restart recovery, time-excluded invocation replay with five zero-call public services, and exact historical v1 compatibility |
| Cover Letter Evidence Snapshot | Implemented P2b2a | 16 synthetic cases for exact source lineage, `COVER_LETTER`-only scope, evidence-ID disjointness from resume-tailoring evidence, stable replay/restart, binding failures, missing locator, empty evidence, changed Plan/Selection/Projection, contract-version identity, subject isolation, immutable conflicts and zero-JD/Agent/tailoring/execution boundaries |
| Plan-scoped Material Manifest Assembly | Implemented P2b1 | 19 synthetic cases for typed assembly, exactly one RESUME entry, entry provenance binding, refusal to claim completeness or Gate A, no placeholder entries, plan and subject mismatch, unknown prepared material, PDF drift, removal and page-count drift, artifact immutability, no legacy-directory fallback, replay, changed material, deterministic current-manifest resolution, conflicts, restart reads, subject isolation and separation from the legacy manifest |
| Prepared Resume Material Publication | Implemented P2a9 | 25 synthetic cases for the direct and revision publication paths, distinct provenance per path, unapproved visual QA, unsuccessful and exhausted revision runs, blocked fact QA, draft and compilation binding mismatch, PDF drift, removal and page-count drift, never copying or regenerating the artifact, subject isolation, replay, changed chains, current-material resolution by publication time rather than mtime, conflicts, restart reads and zero-compiler/renderer/Agent boundaries |
| Bounded Resume Layout Revision | Implemented P2a8b | 24 cases with fake Revision Agents, fake renderers, scripted compilers and the real P2a7/P2a8a entry points for not-required passes, single-attempt success, child lineage, byte-identical content region, restricted Agent context, eight rejected unsafe revisions, untyped output, bounded serial attempts, compilation stop, renderer failure, Agent unavailability, replay with zero extra work, changed policy, conflicts, restart and content-preservation boundaries |
| Resume Visual QA | Implemented P2a8a | 32 cases with fake renderers and fake visual Agents for binding fail-closure before any render, PDF hash and page-count drift, page-policy violation leaving both documents byte-identical, blank pages, clipped characters, undersized type and missing Draft content found deterministically, renderer unavailability and out-of-order pages, restricted Agent context, blocking versus advisory severity derived from finding type, unknown pages and out-of-page boxes, uncertain verdicts, replay with zero re-render, changed policy and Agent versions, conflicts, restart and subject isolation, plus real pypdfium2 rendering of a genuine multi-page PDF |
| Sandboxed LaTeX Compilation | Implemented P2a7 | 29 cases with fake compilers and controlled fake executables for binding fail-closure before any run, source re-hash and capability rescan, compiler unavailability, unmanaged dependencies, bounded de-pathed error diagnostics, timeout, invalid or missing PDF, multi-page recording, managed-artifact isolation, replay with zero extra runs, changed compiler version, artifact drift and record corruption, restart and subject isolation, real-subprocess env/cwd/shell isolation, plus an optional real-`pdflatex` end-to-end case that skips when no engine is installed |
| TailoredDraft to LaTeX Construction | Implemented P2a6c | 25 synthetic fake-Agent cases for deterministic template render, marked-base region replacement, unmarked-base single Agent call, restricted Agent context, exactly-once section/bullet fidelity, omitted-bullet removal, eight unsafe-output deferrals, non-PASSED fact QA, base-selection drift, unreadable base without substitution, replay, changed template binding, restart lineage reads, corrupt records and zero-compilation/execution boundaries |
| Automatic Base LaTeX Version Selection | Implemented P2a6b | 23 synthetic fake-Agent cases for managed-template fallback, single-candidate and source-resume determinism, one bounded Agent call, metadata-only Agent context, unusable Agent answers, explicit version requirements and unsatisfiable deferral, non-PASSED and drift-bound fact QA, candidate provenance verification, subject isolation, replay, candidate-set change, conflicts, restart reads and zero-compilation/Visual-QA/execution boundaries |
| Trusted LaTeX Resume Version Registry | Implemented P2a6a | 47 synthetic cases for explicit-source registration, managed-byte hashing, survival after deleting the original input, coexisting versions and families, parent/child lineage, unknown or cross-subject parents, family conflicts, all five source kinds, replay and identity conflicts, nine rejected capabilities, unmanaged and non-UTF-8 sources, subject isolation, filesystem-independent ordering, artifact drift and record corruption, restart reads and zero-selection/compilation/Agent boundaries |
| Single-file Base LaTeX Registration Contract | Implemented P2a6a1 | 4 focused tests cover strict registration/replay with versioned profile metadata, external dependency/unmanaged package/unsafe capability/broken interface rejection, unchanged GENERAL multi-file and historical record semantics, and profile-separated version/family identity over shared content-addressed bytes; P2a6a and P2a6c affected regressions remain green |
| Evidence-bound Resume Fact QA | Implemented P2a5 | 31 synthetic fake-Agent cases for binding blocks, deterministic unsupported claims with zero Agent calls, altered unrewritten text, missing source coverage, advisory JD references, four semantic exaggerations, restricted Agent context, replay of passed and blocked results, invalid findings and uncertain verdicts, new results on version change, immutable conflicts, restart reads and zero-rendering/Visual-QA/browser/execution boundaries |
| Evidence-bound Resume Tailoring Draft | Implemented P2a4c | 26 synthetic fake-Agent cases for binding fail-closure, bounded single Agent call, safe context, evidence/JD/verb validation, user-retention protection, deferred outcomes, immutable replay/restart, conflicts and zero-rendering/QA/browser/execution boundaries |
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

### P2b5c Material Correction Target Contract

Four focused cases prove closed 10/10 reason-to-target coverage, exact Fact QA
finding binding, P2b4d1 Publication visual/layout/overflow lineage
consumption, P2b4e2 Compilation stopped-source identity, immutable replay and
current/stale/incomplete behavior, optional P2b5 target references, and safe
output without paths, hashes, stderr, credentials, permits, exceptions, or
Agent text.

### S3g4a Unsupported Claim Correction Resolution

Three focused cases cover direct Resume and Cover Letter finding targets,
explicit REMOVE/REWRITE actions, exact target/finding/source binding,
immutable directive versioning and replay, optional Draft-provider
consumption without evidence mutation, one P2b4 call, retained directives on
defer/failure, and fail-closed stale or unsupported targets. The affected
Draft, Fact QA, P2b4, P2b5c/P2b5d, and S3f compatibility set passes without
weakening evidence or QA rules.

### S3g4b LaTeX Compilation Correction Resolution

Three focused cases cover exact current target/stopped-source validation,
reason-derived managed-dependency and compilable-LaTeX modes, immutable
directive/receipt replay, P2a6c construction-identity consumption, a new
managed immutable Construction result, one P2b4 call, stale and unsupported
fail-closure, retained directives on defer/failure, disappeared-item replay,
and zero automatic correction loop. Compatibility covers P2a6c, Compilation
source resolution/stopped-source persistence, P2b5c targets, P2b4, and S3f.

### P2b5e1 Resume Layout Correction Safe Preview Contract

Three focused cases cover exact Visual QA target artifact binding, immutable
preview creation and replay, opaque authenticated PNG reads, source/hash and
cross-subject drift, renderer unavailability, unsafe media fail-closure, and
safe S3f metadata without paths, source hashes, credentials, permits,
exceptions, material writes, or P2b4 calls.

### P2b5e2 Cover Letter Overflow Correction Safe Preview Contract

Three focused cases cover exact Publication/evaluation/source binding,
compiler-and-renderer-bound immutable preview creation and replay, closed
source/evaluation drift, cross-subject reads, unavailable adapters, unsafe
media, source and preview integrity failures, and opaque authenticated PNG
reads. S3f exposes limited page metadata only; tests assert no paths, hashes,
stderr, mutation, P2b4 call, or item resolution.

### S3g4c Resume Layout Correction Resolution

Three focused cases cover exact current target/preview binding, closed visual
issues, origin-derived correction modes, immutable directive/receipt replay,
one P2b4 call, missing/stale/unsupported fail-closure, authenticated Dashboard
action wiring, P2a8b directive consumption into a new Layout run identity,
unchanged attempt limits, and deterministic byte-identical Resume content
preservation without automatic correction loops.

### S3g4d Cover Letter Overflow Correction Resolution

Three focused cases cover current target/preview/source/evaluation binding,
immutable directive and receipt replay, one P2b4 call, invalid action,
missing/stale/unsupported fail-closure, authenticated Dashboard wiring,
optional Publication consumption into a new content-addressed source and
Publication identity, and deterministic byte-identical Cover Letter document
body preservation. Defer and failure retain the directive without Queue
mutation, hidden retry or automatic correction loops.

### P2b5f Input Replacement Target Contract

Four focused cases prove closed 3/3 mapping coverage, exact ResumeCandidate
binding for both Source Resume reasons, exact LaTeX Version/family/source
binding, immutable target replay, Queue-v5 optional references, current/stale
and incomplete behavior, and safe authenticated projection without paths,
content hashes, credentials, permits, exceptions or internal diagnostics.
Compatibility covers semantic classification, selection adapters, Source
Resume/LaTeX contracts, S3f and S3g1/S3g2 item behavior.

### S3g5 Existing Input Replacement Resolution

Three focused cases cover exact ResumeCandidate and LaTeX Version selection,
same-input and unlisted-option rejection, replacement provenance on the
existing plan-scoped override v2 contract, single P2b4 invocation, immutable
receipt retention, failed-rerun replay, and zero automatic replacement loops.
S3g2 v1 override reads remain compatible; P2a3 and P2a6b additionally
revalidate v2 replaced-input version/hash plus the selected typed option.

### S3g5b1 New ResumeCandidate Registration and Replacement

Three focused cases cover valid byte-detected PDF registration followed by
one exact S3g5 delegation and deterministic child invocation; oversize,
unsupported bytes and LaTeX-target rejection before registration; and
invocation replay, P2a2 same-content `UNCHANGED` reuse, delegated partial
failure, retained candidate identity and controlled staging cleanup.
Compatibility exercises P2b5f Source Resume targets, P2a2 registration,
S3g5 typed commands and the authenticated multipart route without trusting
filename or browser media type.

### S3g5b2 New Base LaTeX Version Registration and Replacement

Three focused cases cover valid strict-profile registration in the exact
target family with predecessor lineage followed by one S3g5 delegation;
oversize, binary, unsafe, structurally invalid and wrong-target rejection
without delegation; and invocation replay, P2a6a same-contract `UNCHANGED`
reuse, deterministic child invocation, delegated partial failure and retained
version identity. Compatibility exercises P2b5f Base LaTeX targets, P2a6a1
single-file registration, S3g5 typed commands and the authenticated multipart
route without accepting client family, parent, path, hash or media authority.
# M1a Model Provider Capability Contract

`ModelBackendCapabilities`, `ModelComponentRequirements`, and
`resolve_component_backend()` form the versioned capability boundary.
Resolution honors an explicit component mapping before `default_backend`,
never falls back, and distinguishes missing backend, missing runtime
credential, unsupported capability contract, and incompatible capabilities.
All nine Preparation components require untrusted-input safety, provider-native
strict JSON Schema, one semantic generation, and no model-visible tools,
filesystem, shell, browser, or external functions. Resume Visual QA additionally
requires image input.

M1a2 adds `NativeModelBackendCapabilities`,
`ModelExecutionIsolationProfile`, and
`EffectiveModelBackendCapabilities`. Resolution reports transport,
authentication mode, native/isolation/effective contract versions, and a safe
typed status. Raw subscription CLI access remains capability-incompatible;
selecting `ISOLATED_SUBSCRIPTION_CLI_V1` reports
`ISOLATION_UNAVAILABLE` until a runner exists. Direct OpenAI text remains
compatible, missing API credentials remain distinct, and Visual QA remains
`MODALITY_UNSUPPORTED` because the wrapper is text-only.

# M1b Isolated Subscription CLI Structured Runner

`IsolatedStructuredModelRequest` accepts a deterministic typed projection,
strict JSON Schema, bounded stdin text, and up to four content-validated
managed PNG/JPEG images. `IsolatedSubscriptionCLIRunner` executes exactly one
provider adapter process inside a fresh macOS Seatbelt workspace with an
explicit environment. The Codex adapter projects only `auth.json`, disables
Agent tools and optional integrations, uses `--output-schema` and `--image` in
the same non-interactive invocation, and never requires an API key.

Five focused cases cover structured image success and cleanup; denial of
outside reads, ambient environment and child execution; invalid/oversized
image input; Codex schema/image/session contract projection; and timeout,
oversized output, tool events, schema failure, and cleanup failure without
retry. A no-generation runtime probe gates the M1a2 profile. The optional
`scripts/smoke_isolated_codex_subscription.py` command requires an explicit
subscription-usage acknowledgement and supplies synthetic text and a 1x1 PNG
only.

# P2a10 Production Preparation Structured-Agent Adapters

`production-preparation-agent-adapters-v1` supplies concrete implementations
for Resume Selection, Resume Tailoring, Resume Fact QA, Base LaTeX Selection,
Resume LaTeX Construction, Resume Visual QA, Resume Layout Revision, Cover
Letter Draft, and Cover Letter Fact QA. The factory resolves all nine
canonical M1a component IDs before constructing any bundle and exposes the
existing stage-specific metadata types from each adapter.

Every call is one bounded structured request with stable backend resolution,
component, prompt, and schema metadata. Static stage policy is bound as a
developer instruction; untrusted typed context is deterministic JSON data.
Only Visual QA sends managed PNG/JPEG bytes. Provider results must pass native
strict schema enforcement, local JSON Schema validation, exact-key parsing,
and the existing domain constructors; original stage validators remain
authoritative.

Five focused cases cover all nine typed ports and nested outputs, unique
component metadata and image projection, malformed/unknown-enum/timeout/
unavailable single-call failures, complete-bundle fail-fast behavior for
missing backend, credential, and image modality, secret-safe bounded
diagnostics and stable resolution metadata, and deterministic rejection of an
unsafe typed LaTeX result by the existing construction validator.
