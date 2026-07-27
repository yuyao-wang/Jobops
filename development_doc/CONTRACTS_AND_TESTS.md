# Jobops Contracts and Tests

This document is the authority for component contracts and implementation evidence. Domain rules are defined in `DOMAIN_AND_RULES.md`; this file does not redefine them.

## 契约

### Contract status

| Contract | Status |
|---|---|
| `SemanticMapper.map_controls()` | Implemented as an in-process provider-neutral Protocol |
| `AdapterRegistry.run()` / deterministic ATS lifecycle | Implemented |
| `JobSourceConnector.search()` | Target V1 port; legacy source utilities are migration inputs |
| `JobPrioritizationService.prioritize()` | Target V1 business service |
| `ResumeSelector.select()` | Target V1 business port |
| Semantic Mapper HTTP API | Proposed transport only; no HTTP service is implemented |

Machine-readable contracts:

- [`contracts/openapi.yaml`](contracts/openapi.yaml) — proposed Semantic Mapper transport.
- [`contracts/job-posting.schema.json`](contracts/job-posting.schema.json) — normalized `JobPosting`.
- [`contracts/priority-decision.schema.json`](contracts/priority-decision.schema.json) — versioned priority result.
- [`contracts/mapping-result.schema.json`](contracts/mapping-result.schema.json) — mapper result and allowed key/status pairs.

Python dataclasses and Protocols remain authoritative for the current in-process runtime. A machine schema becomes runtime-authoritative only when its boundary is implemented and covered by equivalence tests.

### Core interfaces

#### `JobSourceConnector.search()`

```text
search(JobSearchRequest) -> JobSearchResult
```

Input:

- `request_id`;
- immutable `SearchProfile` ID/version;
- source-specific cursor;
- collection timestamp and result limit.

Output:

- raw postings with source identity and observed time;
- next cursor;
- typed source warnings/failures.

Invariants:

- read-only with respect to external sources;
- one source failure does not discard other source results;
- upsert/deduplication happens after normalization, outside the connector;
- source identity and ATS identity remain separate;
- candidate profile values are not sent to a source unless that source contract explicitly requires and permits them.

#### `JobPrioritizationService.prioritize()`

```text
prioritize(JobPosting, EvidenceSummary, PriorityPolicy) -> PriorityDecision
```

The result binds the job content hash, candidate snapshot version, and scoring version. Model-assisted JD extraction may produce structured observations, but deterministic rules own hard filters, score aggregation, and P0–P3. The operation cannot mutate the queue.

Repeated calls with identical bound inputs must return the same qualification, scores, priority, filters, and reasons. Persistence metadata such as `decision_id` and `decided_at` is excluded from semantic-equivalence comparison.

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
| Priority | job hash + candidate snapshot + scoring version | identical bindings produce identical output |
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
| Model-assisted JD analysis | 20 s, one attempt per job revision | prioritization caller |
| Resume selection | local operation; no internal retry | preparation caller |
| Semantic Mapper | target: 20 s end-to-end, one dispatch per run; persistent enforcement pending | execution caller |
| ATS navigation | 30 s per navigation | adapter context |
| Submit confirmation | adapter-bounded observation; never repeated by clicking again | execution service |

Timeout does not imply retryability. A Semantic Mapper timeout degrades to unresolved-control handoff. A timeout after submission intent or click becomes `SUBMIT_UNKNOWN`.

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
| Job Discovery service | Not yet contracted in code | schema and target interface only |
| Prioritization / P0–P3 | Not yet implemented as one service | schema and domain rules only |
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
- The new JSON/YAML files parse successfully; automated OpenAPI validation, `$ref` resolution, format checks, and Python-to-schema equivalence are still required before a machine contract is marked implemented.
- Every fixed bug adds a sanitized regression test named for the failed invariant.
- A regression is complete only when the test fails on the old behavior and passes with the fix.
- Fixture metrics and live metrics are always reported separately by ATS, adapter version, and date.

Primary evidence:

```text
tests/test_acceptance_metrics.py
tests/test_ats_adapter_contract.py
tests/test_workday_adapter.py
tests/test_application_engine.py
tests/test_semantic_mapper_contract.py
```
