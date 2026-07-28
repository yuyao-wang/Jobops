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
│   └── I2 add/apply Resolution                        [完成]
│       ├── 生成 typed JobIntakeProposal
│       ├── 原子消费 Pending；同 action 重放结果
│       ├── 调用 injected JobDiscoveryPort
│       └── resolve_pending_intake(
│               request: ResolvePendingIntakeRequest,
│               *,
│               pending_store: InMemoryPendingIntakeStore,
│               discovery_port,
│           ) -> ResolvePendingIntakeResponse
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
现有 I2 → Job Discovery
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
  └── DiscoveryRun
```

## 3. Job Prioritization

```text
Job Prioritization                                     [部分：P1a–P1c 完成]
│
├── Editable Policy                                    [完成 P1a]
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
│   ├── Draft：进程内短期状态
│   └── Approved Policy：Private Home 版本化持久化
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
├── Job Analysis                                       [计划 后续]
└── Reprioritization / Queue                           [计划 P1d]
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
```

边界：

```text
Priority Agent   -X→ Repository / ATS / Browser / Application Preparation
Validation Gate  -X→ 再次调用 Agent
PriorityDecision -X→ 自动进入申请队列（P1d 尚未实现）
```

## 4. Application Preparation

```text
Application Preparation                                [部分]
│
├── CandidateEvidence
├── Resume selection / tailoring
├── Cover Letter
├── Application answers
├── Fact QA
├── Visual QA
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
├── Application Engine                                 [完成]
│   └── async JobApplicationEngine.execute(
│           *,
│           page: Any,
│           bundle: ApplicationBundle,
│           request_submit: bool = False,
│           approve_gate_a: bool = False,
│           approved_review_hash: str = "",
│           credential_store: CredentialStore | None = None,
│           mailbox_verifier: MailboxVerifier | None = None,
│           brain: Any = None,
│           platform_hint: str = "",
│           tenant: str = "",
│           lease_ttl_seconds: float = 1800.0,
│           browser_lease: Lease | None = None,
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
  ↓
Job Prioritization
  ↓
按 P0–P3 选择材料策略
  ↓
Application Preparation
  ↓
ApplicationPlan 与审批
  ↓
Application Execution
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
Job Prioritization
  ↓
ApplicationPlan
  ↓
Approval Policy
  ↓
Application Execution
```

## 8. 关键边界

```text
Conversational Intake
  ├── 可以：调用 PublicJobReader、JobSearch、JobDiscovery Port
  ├── 可以：管理 CandidateSet、候选选择、add/apply 和 JobIntakeProposal
  └── 不可以：调用具体 Connector、Repository、CSV、Private Home 或 ATS

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
