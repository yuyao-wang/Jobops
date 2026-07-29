# Jobops V1 Architecture Map

这份文档用于快速查看系统结构，不替代
[`ARCHITECTURE.md`](ARCHITECTURE.md) 中的权威架构决策。

状态：

- `[完成]` 已实现并有测试
- `[部分]` 已有部分能力
- `[计划]` 尚未实现
- `[实验]` 不属于当前 V1 正式支持面
- `[旧版]` 迁移期间保留，不属于 V1 支持面

函数签名只列公开业务入口和有独立职责的内部函数。以 `_` 开头的函数标记为
`内部`，不属于稳定公开契约。

## 1. 系统总览

```text
Jobops V1
│
├── 交互层
│   ├── Dashboard / 对话框
│   ├── Conversational Intake
│   ├── jobctl CLI
│   ├── Scheduler
│   └── Codex workflows
│
├── 业务层
│   ├── PublicJobReader Port
│   ├── JobSearchPort
│   ├── Job Discovery
│   ├── Job Prioritization
│   ├── Application Preparation
│   └── Application Execution
│
└── 数据与基础设施层
    ├── Private Home
    ├── Event Ledger
    ├── Keychain / Mailbox
    ├── Browser Broker / Worker
    ├── Model Provider
    └── Documents / Artifacts
```

## 2. Job Discovery

```text
Job Discovery Domain
│
├── Public Job Read                                    [部分]
│   ├── PublicJobReader Port                           [完成]
│   │   ├── 目标：业务只提供 URL，不选择平台
│   │   ├── Contract / SourceJobObservation            [完成]
│   │   └── async read_public_job(                     [完成]
│   │           request: ReadJobRequest,
│   │       ) -> ReadJobResult
│   │
│   └── Progressive Read
│       ├── 1. Greenhouse Connector                    [完成]
│       │   └── 当前具体实现：
│       │       GreenhousePublicJobReader.read_job(...)
│       ├── 2. Lever Connector                         [完成]
│       │   └── 内部 LeverPublicJobReader.read_job(...)
│       ├── 3. Generic JSON-LD Reader                  [完成]
│       │   └── 内部 GenericJsonLdJobReader.read_job(...)
│       ├── 4. DOM Recipe                              [计划未定义]
│       ├── 5. Bounded Agent Fallback                  [实验 / blocked]
│       └── 6. UNSUPPORTED / handoff
│
├── Conversational Intake                              [部分]
│   ├── I1 URL Intake                                  [完成]
│   │   ├── 调用 PublicJobReader Port
│   │   ├── 裸 URL：询问 add / apply
│   │   └── async handle_conversational_url_intake(
│   │           request: ConversationalIntakeRequest,
│   │           *,
│   │           pending_store: InMemoryPendingIntakeStore,
│   │       ) -> ConversationalIntakeResponse
│   │
│   ├── S1b Named Job Search                           [完成]
│   │   ├── URL-first；无 URL 才提取 company / title / location
│   │   ├── NamedJobClueExtractor Port                 [完成]
│   │   ├── 真实 extractor / product wiring            [计划]
│   │   ├── 每条消息最多调用一次 JobSearchPort
│   │   └── async handle_conversational_intake(
│   │           request: ConversationalIntakeRequest,
│   │           *,
│   │           clue_extractor: NamedJobClueExtractor,
│   │           job_search_port: JobSearchPort,
│   │           candidate_store: InMemoryCandidateSelectionStore,
│   │           pending_store: InMemoryPendingIntakeStore,
│   │       ) -> ConversationalIntakeResponse
│   │            | NamedJobSearchResponse
│   │
│   ├── S2 Candidate Selection                         [完成]
│   │   ├── 原子选择 CandidateSet 中一个 candidate
│   │   ├── 通过 PublicJobReader 读取完整岗位
│   │   ├── 创建现有 WAITING_FOR_ACTION PendingIntake
│   │   └── async select_search_candidate(
│   │           request: CandidateSelectionRequest,
│   │           *,
│   │           candidate_store: InMemoryCandidateSelectionStore,
│   │           pending_store: InMemoryPendingIntakeStore,
│   │           reader: PublicJobReader,
│   │       ) -> ConversationalIntakeResponse
│   │
│   ├── I2 add/apply Resolution                        [完成]
│       ├── 生成 typed JobIntakeProposal
│       ├── 原子消费 Pending；同 action 重放结果
│       ├── 调用 injected JobDiscoveryPort
│       └── resolve_pending_intake(
│               request: ResolvePendingIntakeRequest,
│               *,
│               pending_store: InMemoryPendingIntakeStore,
│               accepted_intent_repository,
│               discovery_port,
│           ) -> ResolvePendingIntakeResponse
│   │
│   └── I2b Accepted Job Intent                        [完成]
│       ├── Discovery ACCEPTED 后才写入
│       ├── explicit subject + formal job/run binding
│       ├── immutable CREATED / UNCHANGED
│       ├── current read：FOUND / NOT_FOUND / INTEGRITY_FAILURE
│       └── Private Home：state/intake/accepted-job-intents/
│
├── Job Search                                         [部分]
│   ├── Provider-neutral JobSearchPort                 [完成]
│   │   └── async search_jobs(
│   │           request: JobSearchRequest,
│   │           *,
│   │           port: JobSearchPort,
│   │       ) -> JobSearchResult
│   ├── S1a Known Greenhouse Board Search              [完成]
│   │   ├── company / alias 精确命中 injected allowlist
│   │   ├── 单次 board listing GET
│   │   ├── title / optional location 确定性匹配
│   │   └── GreenhouseBoardJobSearch.search(
│   │           request: JobSearchRequest,
│   │       ) -> JobSearchResult
│   ├── CandidateSet                                   [完成]
│   │   ├── 0 个候选仍是 SUCCEEDED
│   │   ├── 稳定排序；最多 10 个；不自动选择
│   │   └── 状态：
│   │       WAITING_FOR_CANDIDATE_SELECTION
│   │       → RESOLVING_CANDIDATE
│   │       → COMPLETED
│   ├── Lever Board Search                             [计划]
│   └── SearchProfile / scheduled search               [计划]
│
└── Formal Discovery Write                             [完成]
    ├── Typed Discovery Entry                          [完成]
    │   ├── 唯一正式岗位写入入口
    │   └── run_discovery(
    │           request: JobDiscoveryRequest,
    │       ) -> JobDiscoveryResponse
    ├── Normalize                                      [完成]
    │   └── 内部 _normalize_candidate(...)
    ├── Canonical Identity / Content Hash              [完成]
    │   ├── 内部 _canonical_http_url(...)
    │   └── 内部 _content_hash(...)
    ├── Deduplicate / Update / Revision                [完成]
    │   ├── 内部 _load_existing(...)
    │   └── Upsert 由 run_discovery(...) 负责
    ├── JobPosting Persistence                         [完成]
    │   └── 通过 PrivateHome 写入
    └── DiscoveryRun                                   [完成]
        └── 内部 _persist_run(...)
```

统一读取路径：

```text
已知 URL
  ↓
handle_conversational_url_intake(...)
  ↓
PublicJobReader
  ↓
SourceJobObservation
  ↓
PendingIntake: WAITING_FOR_ACTION
```

命名搜索与选择路径：

```text
company + title + optional location
  ↓
handle_conversational_intake(...)
  ↓
NamedJobClueExtractor
  ↓
search_jobs(...) / JobSearchPort
  ↓
CandidateSet: 0 / 1 / 多个候选
  ↓ 用户必须明确选择
select_search_candidate(...)
  ↓
PublicJobReader
  ↓
SourceJobObservation
  ↓
PendingIntake: WAITING_FOR_ACTION
  ↓ 用户必须明确选择
ADD_JOB / REQUEST_APPLICATION
  ↓
I2 → Job Discovery
  ↓ accepted
AcceptedJobIntent
```

统一写入路径：

```text
Conversational Intake / 其他 typed caller
  ↓
JobIntakeProposal
  ↓
run_discovery(...)
  ↓
验证与标准化
  ↓
去重 / 创建 / 更新
  ↓
Private Home
  ├── JobPosting
  ├── DiscoveryRun
  └── AcceptedJobIntent（subject-specific）
```

## 3. Job Prioritization

```text
Job Prioritization                                     [部分：P1a–P1d4 完成]
│
├── Editable Policy                                    [完成 P1a + P1a2]
│   ├── PrioritizationPolicyService
│   │   ├── async create_policy_draft(
│   │   │       request: CreatePolicyDraftRequest,
│   │   │   ) -> CreatePolicyDraftResult
│   │   ├── approve_policy(
│   │   │       request: ApprovePolicyRequest,
│   │   │   ) -> PrioritizationPolicyResult
│   │   └── get_active_policy(
│   │           subject_id: str,
│   │       ) -> PrioritizationPolicy | None
│   ├── PreparationAdmissionPolicy
│   │   ├── draft default：P0 / P1 / P2 direct eligible
│   │   ├── draft default：P3 explicit promotion
│   │   ├── reviewed typed P0–P3 disjoint sets
│   │   └── NEEDS_USER / EXCLUDED forbidden
│   ├── Draft：进程内短期状态
│   └── Approved Policy：admission 进入 hash/version/Private Home
│
├── AI Priority Proposal                               [完成 P1b]
│   ├── build_candidate_summary(...) -> CandidateSummary
│   ├── JobPosting + ACTIVE Policy + CandidateSummary + now
│   │   └── PriorityContext + deterministic job facts
│   ├── PriorityAgentPort
│   │   └── async evaluate(
│   │           context: PriorityContext,
│   │       ) -> PriorityAgentOutput
│   ├── async create_priority_proposal(
│   │       request: CreatePriorityProposalRequest,
│   │       *,
│   │       agent: PriorityAgentPort,
│   │       metadata: PriorityAgentMetadata,
│   │   ) -> CreatePriorityProposalResult
│   └── PriorityProposal：validated、短期持有、不持久化
│
├── Validation Gate                                    [完成 P1c]
│   ├── finalize_priority_proposal(
│   │       request: FinalizePriorityProposalRequest,
│   │       *,
│   │       repository: PrivateHomePriorityDecisionRepository,
│   │   ) -> PriorityDecisionResult
│   ├── Job / Policy / Candidate / Proposal binding validation
│   ├── approved hard-constraint deterministic evaluation
│   ├── Agent finding / Gate result reconciliation
│   ├── eligibility evidence：work auth / residency / student / clearance
│   └── validation_version = priority-gate-v2
│
├── PriorityDecision                                   [完成 P1c]
│   ├── QUALIFIED → P0 / P1 / P2 / P3
│   ├── EXCLUDED
│   ├── NEEDS_USER
│   └── Private Home：immutable + idempotent JSON
│
├── Single-job Orchestrator                            [完成 P1d1]
│   ├── orchestrate_single_job_priority(
│   │       command: SingleJobPriorityCommand,
│   │       ...ports,
│   │   ) -> SingleJobPriorityResult
│   ├── typed JobPosting read
│   ├── ACTIVE Policy + trusted CandidateSummary
│   ├── pre-Agent input binding：atomic claim
│   ├── new binding → Proposal once → Gate once
│   └── completed binding → UNCHANGED / zero Agent call
│
├── Current Priority Queue                             [完成 P1d2]
│   ├── build_current_priority_queue(
│   │       command: CurrentPriorityQueueCommand,
│   │       ...read ports,
│   │   ) -> CurrentPriorityQueueResult
│   ├── typed JobPosting list
│   ├── ACTIVE Policy + trusted CandidateSummary
│   ├── reuse P1d1 expected binding
│   ├── CURRENT / STALE / MISSING / INCOMPLETE
│   ├── CURRENT：existing Proposal + Decision
│   ├── sorting：P0 → P1 → P2 → P3 → NEEDS_USER → EXCLUDED
│   └── zero Agent / zero Gate / zero claim / zero write
│
├── Selective Batch Reprioritization                   [完成 P1d3]
│   ├── selectively_reprioritize_jobs(
│   │       command: SelectiveBatchReprioritizationCommand,
│   │       queue_reader: P1d2 callable,
│   │       single_job_orchestrator: P1d1 callable,
│   │   ) -> SelectiveBatchReprioritizationResult
│   ├── non-empty job_ids or positive max_jobs
│   ├── execute：STALE / MISSING only
│   ├── skip：CURRENT / INCOMPLETE
│   ├── deterministic serial order / same explicit now
│   ├── typed per-job failure isolation
│   └── no direct Agent / Proposal / Gate / repository access
│
├── Runnable Application Queue                        [完成 P1d4]
│   ├── build_runnable_application_queue(
│   │       command: RunnableApplicationQueueCommand,
│   │       priority_queue_reader: P1d2 callable,
│   │       accepted_intent_repository: typed read port,
│   │   ) -> RunnableApplicationQueueResult
│   ├── exact P1d2 policy snapshot / no second policy lookup
│   ├── RUNNABLE：CURRENT + REQUEST_APPLICATION + direct admission
│   ├── typed blocks：not-current / intent / decision / priority / lifecycle
│   ├── P3 promotion-required is blocked; no promotion record is created
│   ├── preserve P1d2 order
│   └── zero Agent / Gate / claim / save / preparation / execution
│
└── Job Analysis                                       [计划 后续]
```

Priority 主数据流：

```text
自然语言求职策略
  ↓
Policy Draft
  ↓ 用户审核和批准
ACTIVE PrioritizationPolicy

explicit subject_id + persisted job_id + explicit now
  ↓
Single-job Orchestrator
  ↓ typed JobPosting + ACTIVE Policy + trusted CandidateSummary
  ↓ pre-Agent binding claim
JobPosting + ACTIVE Policy + CandidateSummary + explicit now
  ↓
create_priority_proposal(...)
  ↓
PriorityProposal（AI 建议，不是正式决定）
  ↓
finalize_priority_proposal(...)
  ↓
PriorityDecision（正式、不可变、幂等持久化）
  ↓
Private Home

explicit subject_id + explicit now
  ↓
Current Priority Queue
  ↓ list current JobPosting + load ACTIVE Policy + CandidateSummary
  ↓ reuse P1d1 binding + read orchestration history
CURRENT / STALE / MISSING / INCOMPLETE
  ├─ bounded STALE / MISSING selection
  │   ↓
  │ Selective Batch Reprioritization
  │   ↓ serial P1d1 calls
  │ new immutable PriorityDecision bindings
  │
  └─ exact policy snapshot + accepted job intent
      ↓
    Runnable Application Queue
      ↓ explicit single-job selection
    immutable ApplicationPlan
      ↓ later preparation stages; no execution authority
```

边界：

```text
Priority Agent   -X→ Repository / ATS / Browser / Application Preparation
Validation Gate  -X→ 再次调用 Agent
Priority Queue   -X→ Agent / Gate / claim / write / 自动重排 / 自动申请
Selective Batch  -X→ direct Agent / Proposal / Gate / repository save
PriorityDecision -X→ 绕过 accepted intent / admission / P1d4 创建计划
ApplicationPlan -X→ 材料已完成 / browser execution / submit authority
```

## 4. Application Preparation

```text
Application Preparation                                [部分]
│
├── ApplicationPlan                                    [完成 P2a1]
│   ├── create_application_plan(
│   │       command: CreateApplicationPlanCommand,
│   │       runnable_queue_reader: P1d4 callable,
│   │       repository: ApplicationPlanRepository,
│   │   ) -> CreateApplicationPlanResult
│   ├── RUNNABLE-only / P1d4 exactly once
│   ├── immutable job + Decision + policy + intent binding
│   ├── plan-scoped exact user instructions + canonical hash
│   ├── AUTOMATION_FIRST
│   └── DEFER_ITEM_AND_CONTINUE
│
├── CandidateEvidence
│   └── Trusted Resume Candidate Registry              [完成 P2a2]
│       ├── explicit subject-scoped registration only
│       ├── actual PDF/DOCX bytes → SHA-256
│       ├── verified/user-confirmed safe summary
│       ├── immutable CREATED / UNCHANGED records
│       └── typed get + stable list_selectable
├── Resume Preparation                                 [部分]
│   ├── Automatic Base Resume Selection                [完成 P2a3]
│   │   ├── Plan + exact typed JobPosting binding check
│   │   ├── ResumeCandidateProvider only
│   │   ├── 0 candidates → defer / 1 → zero Agent
│   │   ├── many → bounded Agent at most once
│   │   └── immutable Decision + pre-Agent UNCHANGED
│   ├── Hash-bound Source Resume Projection             [完成 P2a4a]
│   │   ├── ResumeCandidateRepository.get only
│   │   ├── managed artifact bytes re-hashed before parse
│   │   ├── PDF page/line and DOCX structural locators
│   │   ├── stable section/block/bullet IDs
│   │   └── immutable typed projection / zero Agent or OCR
│   ├── CandidateEvidence Snapshot                      [完成 P2a4b]
│   │   ├── Plan → latest complete Selection
│   │   ├── selected Candidate → latest complete Projection
│   │   ├── exact block text + typed source locator
│   │   ├── PERSONAL / RESUME_TAILORING only
│   │   ├── USER_PROVIDED_DOCUMENT_STATEMENT
│   │   └── immutable snapshot / CREATED or UNCHANGED
│   └── Evidence-bound TailoredResumeDraft              [完成 P2a4c]
│       ├── full Plan/Job/Selection/Projection/Evidence binding check
│       ├── static versioned Resume Tailoring Agent policy
│       ├── verbatim Plan user instructions, never policy edits
│       ├── Action Verb + Details + evidenced Outcome
│       ├── bounded Agent at most once per new binding
│       ├── deterministic evidence/JD/verb output validation
│       ├── DEFERRED_INSUFFICIENT_EVIDENCE / DEFERRED_NEEDS_HUMAN
│       └── immutable draft + pre-Agent UNCHANGED replay
├── Resume Fact QA                                     [完成 P2a5]
│   ├── independent of the tailoring validator
│   ├── full binding check → BLOCKED_BINDING_MISMATCH
│   ├── deterministic references/tokens/coverage first
│   ├── deterministic block → zero Agent calls
│   ├── bounded Agent at most once, bullets + evidence only
│   ├── ownership/maturity/impact/causality/scope judgments
│   ├── PASSED / BLOCKED / DEFERRED, never edits the draft
│   └── immutable result + pre-Agent UNCHANGED replay
├── Managed Resume Document Versions                   [部分]
│   ├── Trusted LaTeX Resume Version Registry          [完成 P2a6a]
│   │   ├── explicit inline source or in-home .tex only
│   │   ├── SHA-256 over actual managed bytes
│   │   ├── subject-isolated managed source copy
│   │   ├── deterministic capability rejection
│   │   ├── many versions and families, no ACTIVE one
│   │   ├── immutable CREATED / UNCHANGED records
│   │   └── typed get + stable list_selectable
│   ├── Version Lineage                                [完成 P2a6a]
│   │   ├── parent must exist under the same subject
│   │   ├── child inherits the parent root family
│   │   └── parentless version derives a stable family
│   ├── Automatic Base LaTeX Version Selection         [完成 P2a6b]
│   │   ├── PASSED FactQA bound to this exact draft only
│   │   ├── candidates only from list_selectable()
│   │   ├── candidate FactQA provenance re-verified
│   │   ├── version metadata only, never .tex content
│   │   ├── user-required ID → only → source-resume match
│   │   ├── no candidate → managed template, zero Agent
│   │   ├── bounded Agent once on a genuine tie
│   │   ├── unusable Agent answer → managed template
│   │   ├── unsatisfiable user requirement → defer
│   │   └── immutable decision + pre-Agent UNCHANGED replay
│   ├── TailoredDraft → LaTeX Construction             [完成 P2a6c]
│   │   ├── obeys P2a6b, never re-selects a version
│   │   ├── controlled JobopsSection / JobopsBullet markers
│   │   ├── one delimited content region per document
│   │   ├── layout from history, content only from Draft
│   │   ├── template render and region replacement: zero Agent
│   │   ├── unmarked base → bounded Agent at most once
│   │   ├── deterministic fidelity + stale-content validation
│   │   ├── unsafe output → DEFERRED_NEEDS_HUMAN
│   │   ├── unreadable base → DEFERRED_SOURCE_UNREADABLE
│   │   └── AI_REVISED child or SYSTEM_TEMPLATE_DERIVED root
│   ├── Managed Default Template                       [最小完成 P2a6c]
│   │   └── managed-resume-one-page-v1, no catalogue
│   └── Conversational Version Override                [计划 后续]
├── Sandboxed LaTeX Compilation                        [完成 P2a7]
│   ├── full construction/version binding re-check
│   ├── source re-read, re-hashed, capability rescanned
│   ├── one allowlisted engine, no shell, fixed argv
│   ├── fresh temp cwd, minimal deterministic env
│   ├── timeout, rlimits, capped logs and outputs
│   ├── describe() cheap, compile() at most once
│   ├── unmanaged dependency → DEFERRED_SOURCE_INCOMPLETE
│   ├── missing engine → DEFERRED_COMPILER_UNAVAILABLE
│   ├── LaTeX error/timeout → DEFERRED_COMPILATION_ERROR
│   ├── deterministic PDF validation, page count recorded
│   ├── managed PDF artifact hashed from stored bytes
│   └── immutable record + pre-compile UNCHANGED replay
├── Prepared Resume Material Publication               [完成 P2a9]
│   ├── direct PASSED Visual QA or successful P2a8b run
│   ├── exactly one source, never both
│   ├── full plan/draft/factQA/version/compilation recheck
│   ├── managed PDF re-read, re-hashed, page count re-parsed
│   ├── records the existing artifact, never copies it
│   ├── NOT_READY for unapproved or mismatched chains
│   ├── FAILED for missing, corrupt or drifted artifacts
│   ├── never falls back to an older or unreviewed PDF
│   ├── immutable material + UNCHANGED replay
│   └── find_current_for_plan by publication time, not mtime
├── Plan-scoped Material Manifest                      [完成 P2b1]
│   ├── separate from the legacy MaterialManifest
│   ├── v1 compatibility read, no rewrite or inferred size
│   ├── v2 RESUME entry from PreparedResumeMaterial
│   ├── full plan/job/provenance binding recheck
│   ├── managed PDF hash + byte size + page count
│   ├── references the artifact, never copies it
│   ├── one entry per role, deterministic order
│   ├── explicit included_roles + assembly_state
│   ├── never claims completeness or Gate A
│   ├── no placeholder or fake entries
│   └── immutable manifest + UNCHANGED replay
├── Cover Letter                                       [完成 P2b2a–P2b2d]
│   ├── Cover Letter Evidence Snapshot                  [完成 P2b2a]
│   │   ├── independent of Resume Tailoring evidence scope
│   │   ├── own COVER_LETTER scope, never reused or inherited
│   │   ├── evidence only from SourceResumeProjection
│   │   ├── exact text, source ID and typed locator preserved
│   │   ├── no JD read, no Agent, no cover letter generated
│   │   ├── empty projection → DEFERRED_NO_EVIDENCE
│   │   └── immutable snapshot + UNCHANGED replay
│   ├── Evidence-bound Cover Letter Draft               [完成 P2b2b]
│   │   ├── full Plan/JobPosting/EvidenceSnapshot binding check
│   │   ├── static versioned Cover Letter Agent policy
│   │   ├── verbatim Plan user instructions, never policy edits
│   │   ├── bounded Agent at most once per new binding
│   │   ├── evidence-only candidate claims, JD never used as proof
│   │   ├── verbatim JD alignment, placeholder rejection
│   │   ├── DEFERRED_INSUFFICIENT_EVIDENCE / DEFERRED_NEEDS_HUMAN
│   │   └── immutable draft + pre-Agent UNCHANGED replay
│   ├── Evidence-bound Cover Letter Fact QA              [完成 P2b2c]
│   │   ├── independent of P2b2b's private validator
│   │   ├── full Plan/JobPosting/EvidenceSnapshot/Draft binding check
│   │   ├── deterministic checks first, zero Agent calls when blocking
│   │   ├── bounded QA Agent at most once, only if nothing was blocked
│   │   ├── Agent judges responsibility/deployment/impact/motivation exaggeration only
│   │   ├── every Agent finding re-verified against known paragraph/evidence/JD
│   │   ├── UNCERTAIN or illegal output → DEFERRED_NEEDS_HUMAN, no persist
│   │   ├── never rewrites or modifies the Draft
│   │   └── immutable result + UNCHANGED replay
│   └── Cover Letter Document Publication               [完成 P2b2d]
│       ├── PASSED Fact QA + full Plan/Job/Draft binding recheck
│       ├── managed-cover-letter-one-page-v1, no catalogue
│       ├── greeting + ordered paragraphs + closing only
│       ├── one-pass escaping + stable paragraph-ID markers
│       ├── subject-isolated managed UTF-8 source + actual-byte hash
│       ├── existing LatexCompilerPort, zero Agent/subprocess duplication
│       ├── unavailable/error/overflow typed deferrals
│       ├── exactly one page + exact visible-text projection
│       ├── immutable material + pre-compile UNCHANGED replay
│       └── no PlanMaterialManifest inclusion
├── Manifest Cover Letter Inclusion                    [完成 P2b2e]
│   ├── prior immutable v2 PlanMaterialManifest
│   ├── preserved RESUME entry, field-for-field
│   ├── Cover Letter hash + byte size + page count
│   ├── v1 prior → typed NOT_READY, never in-place migration
│   ├── ordered RESUME → COVER_LETTER entries
│   ├── immutable lineage-bound manifest version
│   └── no Answers / Gate A / Browser / ATS state
├── Preparation-to-Execution Material Contract         [完成 P2c0]
│   ├── explicit Manifest v1/v2 parsing and identity
│   ├── v2 artifact_byte_size for every PDF entry
│   ├── optional typed managed Cover Letter PDF reference
│   ├── legacy Cover Letter text remains unchanged
│   └── no PDF upload / selection / conversion behavior
├── Plan-scoped Application Bundle Assembly             [完成 P2c1]
│   ├── exact Plan / Job / v2 Manifest / AnswerSet binding
│   ├── managed Resume + Cover Letter PDF revalidation
│   ├── blocking unresolved → NOT_READY; optional skips retained
│   ├── existing ApplicationBundle + CanonicalApplicationAnswers
│   ├── immutable provenance record + deterministic replay/current read
│   └── no SemanticMapper / Gate / Browser / ATS / Engine
├── Recoverable Application Bundle Envelope              [完成 P2c1b]
│   ├── complete existing ApplicationBundle snapshot
│   ├── managed materials + canonical answers + profile + policy
│   ├── AssemblyRecord / bundle hash binding
│   ├── subject-isolated immutable save + typed recovery
│   └── no backfill / factory / Gate / Browser / ATS / Engine
├── Canonical Document Upload Mapping                    [完成 P2c2]
│   ├── FormIR FILE + shared RESUME / COVER_LETTER_FILE keys
│   ├── at-most-once typed upload plan before file mutation
│   ├── subject path + symlink + hash + size + PDF validation
│   ├── required failure / optional skip / UNKNOWN fail-safe
│   ├── shared BaseATSAdapter fill and read-back support
│   └── legacy Resume path + Cover Letter text unchanged
├── Plan-scoped Gate A + Non-submit Engine Integration   [完成 P2c3]
│   ├── recover exact P2c1b ApplicationBundle
│   ├── formal Gate A before Browser / Engine
│   ├── one Browser lease + one Engine Review call
│   ├── request_submit=False + empty review approval
│   ├── typed runtime-input / Browser defer
│   └── immutable record + zero-call replay / no Gate B / no submit
├── Gate B Submission Authorization                      [完成 P2c4]
│   ├── exact P2c3 Review + P2c1b Bundle binding
│   ├── existing gate_b_actor / submit_authority policy
│   ├── AUTOMATIC or review-scoped EXPLICIT_USER
│   ├── validation / unresolved / submission fail-closure
│   ├── immutable Decision + deterministic replay/history
│   └── zero Browser / Engine / ATS / permit / submit intent
├── Plan-scoped Submission Permit Contract               [完成 P2c5a]
│   ├── explicit versioned submission bindings
│   ├── subject / Plan / Bundle / Review / Decision / execution / adapter
│   ├── SUBMIT_APPLICATION-only action scope
│   ├── persisted verifiable Gate A consumption reference
│   ├── stable signer metadata without private-key exposure
│   ├── subject-isolated opaque bearer-token reference
│   └── legacy permit serialization and ApplicationBundle bindings unchanged
├── Plan-scoped Submission Permit Issuance               [完成 P2c5b]
│   ├── AUTHORIZED Decision + valid Gate A consumption only
│   ├── policy-owned TTL + PermitService signer metadata
│   ├── full token stored only in OpaquePermitTokenStore
│   ├── immutable permit record stores reference + token hash
│   ├── valid replay → UNCHANGED; expiry follows explicit policy
│   └── zero Browser / Engine / ATS / submit intent
├── Authorized Submission Execution                      [完成 P2c6]
│   ├── recover exact P2c1b ApplicationBundle
│   ├── verify unexpired, unused, plan-scoped permit before Browser
│   ├── one Browser lease + one Engine review replay
│   ├── latest Review must equal permit-approved Review
│   ├── permit consumed at existing submit point of no return
│   ├── existing EventLedger intent + adapter submit once
│   ├── verified evidence or terminal SUBMISSION_UNCERTAIN
│   └── immutable record + successful zero-call replay / no auto-retry
├── Single-job Automated Application Execution           [完成 P2c7]
│   ├── exact serial P2c3 → P2c4 → P2c5b → P2c6 public calls
│   ├── same subject / Assembly / explicit now
│   ├── CREATED + compatible UNCHANGED continuation
│   ├── typed defer / block / failure ordered-prefix stop
│   ├── terminal uncertainty, never automatic retry
│   ├── immutable stage lineage + terminal zero-call replay
│   └── no direct Gate / permit / Browser / Engine / ATS access
├── Application Answers                               [部分]
│   ├── Unified Canonical Answer Taxonomy              [完成 P2b3a]
│   │   ├── one versioned typed key registry
│   │   ├── value type + sensitivity + automation metadata
│   │   ├── FormIR / SemanticMapper / ApplicationBundle references
│   │   ├── explicit legacy alias normalization
│   │   └── UNKNOWN remains fail-safe
│   └── Application Answers Preparation                [完成 P2b3b]
│       ├── CandidateVault typed trusted-record projection
│       ├── subject-bound deterministic ApplicationFactSnapshot
│       ├── canonical typed answers + supporting fact IDs
│       ├── safe-skip and human-required unresolved items
│       ├── demographic decline and attestation boundaries
│       ├── immutable identity/replay/history repository
│       └── no FormIR / SemanticMapper / Browser / ATS / Gate
├── Single-job Automated Preparation                   [完成 P2b4]
│   ├── existing ApplicationPlan ownership check
│   ├── exact serial P2a3 → P2b3b public-Slice recipe
│   ├── same subject / Plan / explicit now for every stage
│   ├── CREATED + UNCHANGED continuation
│   ├── Visual QA pass-skip or P2a8b final lineage
│   ├── typed defer/failure short-circuit, no rollback/retry
│   ├── completed zero-call replay + immutable Run history
│   └── no Human Queue / batch / Gate / Browser / ATS
├── Current Human Attention Queue                      [完成 P2b5]
│   ├── subject-scoped Run list + deterministic current per Plan
│   ├── current DEFERRED / FAILED projection
│   ├── completed Run blocking AnswerSet expansion
│   ├── USER fact / choice / attestation / manual review
│   ├── OPERATOR system / integrity / unknown reason
│   ├── stable identity, priority/audience/kind ordering
│   └── zero store / write / retry / resolution
├── Selective Batch Preparation                        [完成 P2b6]
│   ├── explicit allowlist or bounded subject Plan list
│   ├── one fixed P2b5 snapshot + current-attention skip
│   ├── deterministic serial P2b4 calls, max concurrency one
│   ├── per-Plan defer/failure isolation and typed summary
│   └── zero batch store / retry / checkpoint / Scheduler
├── Resume Visual QA                                   [完成 P2a8a]
│   ├── full compilation/version/draft binding re-check
│   ├── PDF re-read, re-hashed, page count re-verified
│   ├── versioned page policy, never inferred from prose
│   ├── deterministic checks first, zero render on block
│   ├── fixed-DPI rendering in stable page order
│   ├── renderer unavailable → DEFERRED_RENDERER_UNAVAILABLE
│   ├── bounded Agent once, images + findings + policy only
│   ├── severity derived from type, not from the Agent
│   ├── advisory alone never blocks PASSED
│   ├── blocking → REVISION_REQUIRED, nothing modified
│   └── immutable result + pre-render UNCHANGED replay
├── Bounded Layout Revision                            [完成 P2a8b]
│   ├── PASSED → NOT_REQUIRED, zero side effects
│   ├── DEFERRED → DEFERRED_NEEDS_HUMAN
│   ├── bounded serial attempts, V1 maximum three
│   ├── one Revision Agent call per attempt
│   ├── source + pages + findings + policies + instructions
│   ├── content region byte-identical, markers unchanged
│   ├── font/margin floors, no hiding tricks
│   ├── P2a7 and P2a8a via public entry points only
│   ├── compile stop or QA defer halts the run
│   ├── AI_REVISED child per attempt, same root family
│   ├── exhausted → DEFERRED_ATTEMPTS_EXHAUSTED with lineage
│   └── immutable run + pre-work UNCHANGED replay
├── Conversational Human Resolution                    [计划]
│   └── typed upstream update then P2b4 rerun
├── Material manifest                                  [完成]
│   └── load_material_manifest(
│           home: PrivateHome,
│           job: JobSpec,
│       ) -> MaterialManifest
│
├── Tier material loading                              [完成]
│   └── build_tier_materials(
│           *,
│           home: PrivateHome,
│           job: JobSpec,
│           policy: PolicyDecision,
│           fallback_resume: Path,
│       ) -> MaterialBundle
│
└── Approval Gate A                                    [完成]
    └── 由 JobApplicationEngine.execute(...) 执行和校验
```

## 5. Application Execution

```text
Application Execution                                  [完成]
│
├── Plan-scoped Non-submit Execution                   [完成 P2c3]
│   └── execute_non_submit_application(...)
│       → NonSubmitApplicationExecutionRecord
│
├── Gate B Submission Authorization                    [完成 P2c4]
│   └── decide_submission_authorization(...)
│       → SubmissionAuthorizationDecision
│
├── Submission Permit                                  [完成 P2c5a/P2c5b]
│   ├── issue_submission_permit(...)
│   │   → SubmissionPermitRecord
│   └── OpaquePermitTokenStore
│       → subject-isolated token recovery
│
├── Authorized Submission Execution                    [完成 P2c6]
│   └── async execute_authorized_submission(
│           command: ExecuteAuthorizedSubmissionCommand,
│           ...
│       ) -> ExecuteAuthorizedSubmissionResult
│
├── Single-job Automated Application Execution          [完成 P2c7]
│   └── async run_application_execution(
│           command: RunApplicationExecutionCommand,
│           ...
│       ) -> RunApplicationExecutionResult
│
├── Application Engine                                 [完成]
│   └── async JobApplicationEngine.execute(
│           *,
│           page: Any,
│           bundle: ApplicationBundle,
│           request_submit: bool = False,
│           approve_gate_a: bool = False,
│           approved_review_hash: str = "",
│           submission_permit_token: str = "",
│           submission_permit_bindings:
│               PlanScopedSubmissionPermitBindings | None = None,
│           credential_store: CredentialStore | None = None,
│           mailbox_verifier: MailboxVerifier | None = None,
│           brain: Any = None,
│           platform_hint: str = "",
│           tenant: str = "",
│           lease_ttl_seconds: float = 1800.0,
│           browser_lease: Lease | None = None,
│           private_home: PrivateHome | None = None,
│       ) -> ApplicationOutcome
│
├── ATS Routing                                        [完成]
│   ├── AdapterRegistry.route_name(
│   │       url: str,
│   │       platform_hint: str = "",
│   │   ) -> str
│   └── async AdapterRegistry.run(
│           request: AdapterRunRequest,
│       ) -> ApplicationOutcome
│
├── Deterministic ATS Adapters                         [完成]
│   ├── Greenhouse
│   ├── Lever
│   ├── Ashby
│   ├── Jobvite
│   ├── Workday
│   └── async BaseATSAdapter.run(
│           context: ApplicationContext,
│       ) -> ApplicationOutcome
│
├── Semantic Mapper                                    [完成]
│   └── async SemanticMapper.map_controls(
│           requests: tuple[MappingRequest, ...],
│       ) -> tuple[MappingResponse, ...]
│
├── Fill / Read-back                                   [完成]
│   ├── async BaseATSAdapter.fill(
│   │       page: Any,
│   │       context: ApplicationContext,
│   │       form: FormIR,
│   │   ) -> FillReport
│   └── async BaseATSAdapter.validate(
│           page: Any,
│           form: FormIR,
│           fill: FillReport,
│       ) -> ValidationReport
│
├── Review / Gate B                                    [完成]
│   └── async BaseATSAdapter.prepare_review(
│           page: Any,
│           context: ApplicationContext,
│           form: FormIR,
│           fill: FillReport,
│           validation: ValidationReport,
│       ) -> ReviewDigest
│
├── Submit                                             [完成]
│   └── async BaseATSAdapter.submit(
│           page: Any,
│           context: ApplicationContext,
│           review: ReviewDigest,
│       ) -> bool
│
└── Submission Evidence / Outcome                      [完成]
    └── async BaseATSAdapter.verify_submission(
            page: Any,
            context: ApplicationContext,
        ) -> SubmissionEvidence
```

## 6. 数据与基础设施

```text
Data & Infrastructure
│
├── Private Home                                       [完成]
│   ├── PrivateHome.discover(...) -> PrivateHome
│   └── PrivateHome.ensure() -> PrivatePaths
│
├── Event Ledger                                       [完成]
│   ├── EventLedger.create_run(...) -> RunRecord
│   ├── EventLedger.append_event(...) -> EventRecord
│   └── EventLedger.create_submission_intent(
│           ...
│       ) -> SubmissionIntent
│
├── Browser Broker                                     [完成]
│   └── async lease_browser_session(...)
│       -> AsyncIterator[LeasedBrowser]
│
├── Keychain / Mailbox                                 [完成]
├── Model Provider                                     [部分]
└── Documents / Artifacts                              [部分]
```

这里的 `...` 表示基础设施方法参数较多；权威参数定义仍以源码为准。业务层调用方不应直接依赖其存储参数。

## 7. V1 主流程

```text
Frontend / Scheduler
  ↓
Public Job Read
  ├── URL → SourceJobObservation
  └── Named Search
      → CandidateSet
      → 用户选择 candidate
      → SourceJobObservation
  ↓
WAITING_FOR_ACTION
  ↓ 用户选择 ADD_JOB / REQUEST_APPLICATION
  ↓
JobIntakeProposal
  ↓
run_discovery(...)
  ↓
去重与更新
  ↓ accepted
AcceptedJobIntent persistence
  ↓
Job Prioritization
  ↓
按 P0–P3 选择材料策略
  ↓
Application Preparation
  ↓
ApplicationPlan 与审批
  ↓
P2c1 ApplicationBundle Assembly
  ↓
P2c1b Recoverable Bundle Envelope
  ↓
P2c3 Gate A + Non-submit Review
  ↓
P2c4 Gate B Authorization
  ↓
P2c5b Short-lived Submission Permit
  ↓
P2c6 Review Replay
  ↓ exact Review match
Point-of-no-return Permit Consumption
  ↓
Submission Intent → Submit Once → Verification Evidence
  ↓
人工接管或记录结果
```

用户明确要求申请时也不能跳过中间流程：

```text
Conversational Intake
  ├── URL → PublicJobReader
  └── Named Search → CandidateSet → 用户选择 → PublicJobReader
  ↓
SourceJobObservation
  ↓
WAITING_FOR_ACTION
  ↓ 用户确认 REQUEST_APPLICATION
  ↓
JobIntakeProposal
  ↓
run_discovery(...)
  ↓
AcceptedJobIntent persistence
  ↓
Job Prioritization
  ↓
ApplicationPlan
  ↓
Approval Policy
  ↓
Bundle Assembly + Recoverable Envelope
  ↓
Gate A + Non-submit Review
  ↓
Gate B Authorization + Submission Permit
  ↓
Authorized Submit Once + Evidence Verification
```

## 8. 关键边界

```text
Conversational Intake
  ├── 可以：调用 PublicJobReader、JobSearch、JobDiscovery 和 AcceptedJobIntent Port
  ├── 可以：管理 CandidateSet、候选选择、add/apply 和 JobIntakeProposal
  └── 不可以：调用具体 Connector/Repository、CSV、Private Home 或 ATS

JobSearchPort
  ├── 可以：返回 0 / 1 / 多个 SearchCandidate
  ├── 必须：0 个候选仍返回成功
  └── 不可以：读取完整 JD、选择候选、持久化或调用 Discovery

Candidate Selection
  ├── 必须：用户明确提供 candidate_set_id + candidate_id
  ├── 可以：调用 PublicJobReader 并创建 WAITING_FOR_ACTION PendingIntake
  └── 不可以：重新搜索、自动 add/apply 或调用 Discovery

PublicJobReader
  ├── 可以：读取并返回 SourceJobObservation
  └── 不可以：调用 run_discovery、持久化或执行申请

SourceJobObservation
  └── 是外部事实观察，不是正式 JobPosting

Model
  ├── 可以：提出受限的结构化判断
  └── 不可以：直接执行高风险操作

Discovery caller
  └── 必须：通过 run_discovery(...) 写入岗位

Bounded Agent fallback
  └── 最多受限读取与字段提取；普通代码验证后才能进入 Discovery

Legacy collectors
  └── 状态：[旧版]，存在不等于 V1 支持
```

## 9. 旧版迁移面

```text
Legacy                                                 [旧版]
│
├── main.py
├── 旧 Dashboard discovery
├── utils.discovery / collectors
├── applications.db
└── profile.yaml
```
