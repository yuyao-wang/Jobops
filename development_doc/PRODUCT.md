# Jobops Product

## 产品目标

Jobops 是一个 privacy-first job application operating system。它把岗位获取、优先级判断、申请材料准备、ATS 执行、人工接管和结果记录连接成一条可恢复的工作流。

Jobops 的目标不是最大化自动点击次数，而是在不虚构候选人事实、不绕过安全控制、不重复提交的前提下，提高高质量申请的处理速度。

产品必须始终保持以下定位：

- 它不是单纯的 ATS form filler。
- 它不是可自由浏览、调用工具和提交申请的 autonomous Agent platform。
- 模型只处理需要语义判断或受约束生成的步骤；业务状态和副作用由确定性代码控制。

## 核心用户流程

```text
更新岗位
→ 标准化与去重
→ 分析 JD 并计算 Priority
→ 按 Priority 生成 ApplicationPlan
→ 选择或准备 Resume / Cover Letter / answer proposals
→ 审批申请材料
→ 执行 ATS 申请
→ 人工接管或验证提交结果
→ 记录状态与证据
```

四条业务数据流组成这一条用户流程：

- Job Discovery：manual trigger → configured provider feeds + optional authorized
  search index + optional alert inbox + user Clipper/pasted URL → unverified
  subject `JobLead` → canonical employer/ATS resolution → normalized,
  de-duplicated `JobPosting` list。
- Prioritization：用户编辑并批准求职策略 → job revision / candidate summary / deterministic facts → AI `PriorityProposal` → validation → explainable `PriorityDecision`。
- Preparation：`ApplicationPlan` → selected `CandidateEvidence` / base resume → constrained materials → validation → approval by the policy-required actor。
- Execution：approved bundle → ATS fill/read-back → Review/Gate B → single submit/evidence；required unresolved controls only pass through value-free mapping before local value resolution。

当前实现只把显式手动 Refresh 接入生产组合。未来 scheduled/daily discovery
必须复用同一业务入口且只改变 trigger；当前不能把 daily ingestion 或 digest
描述为已实现能力。

## MVP 范围

V1 要完成所有核心业务域的端到端功能，而不是覆盖所有外部平台。

- Job Discovery
  - SearchProfile。
  - 手动和定时更新共用同一入口。
  - 手动 URL/JD 和 Private Home CSV intake。
  - 配置化的 Greenhouse、Ashby public job-board 与 Lever public postings
    feed；Glassdoor 只在合法 partner API 凭证存在时启用，Jobvite 只在
    企业已购买 Job Feed API 且配置 key/secret 时启用。未配置凭证的来源
    不出现在可执行 SearchProfile source 中。安全默认配置不带任何
    示例 company tenant；只有用户实际配置的 board/feed 才会被搜索。
  - 多来源发现先写入 subject-scoped `JobLead`，而不是直接写正式职位。
    当前来源包括：经授权的 Web Search API、LinkedIn/Indeed/employer job
    alert 邮件、本机 Web Clipper 当前页，以及用户粘贴的 URL。Lead 只保存
    去掉凭证、追踪参数和 fragment 的 canonical source URL、有限提示、
    时间、置信度和来源谱系；它不是经过验证的职位事实。
  - 可选的 Brave Search API adapter 根据当前已批准的 ROLE、LOCATION 和
    freshness preference，按有界请求预算搜索 LinkedIn、Indeed、Glassdoor、
    Greenhouse、Lever、Ashby、Jobvite、Workday、SmartRecruiters、iCIMS、
    SuccessFactors 和 company careers 索引结果。该能力只有配置合法 API key
    并显式确认结果存储权利后才启用；它不依赖先配置 company
    feed，没有配置时不得伪装成已搜索全网。
  - 本机只读 Job Alert Inbox 可在用户明确授权、凭证保存在 Keychain 且邮箱
    范围受限时解析 LinkedIn、Indeed 或 employer/ATS alert。凭证 account
    必须与 recipient 一致，且只接受由配置的 receiving `authserv-id`
    投影为 DMARC PASS 且 SPF/DKIM 至少一项 PASS 的邮件。它不持久化原始
    邮件正文，也不把发件人或摘要当作权威职位事实。当前是通用 IMAP adapter，
    尚不是 Gmail OAuth connector，也没有独立的每日调度器。
  - JobOps Web Clipper 只在用户点击扩展按钮后读取当前标签页的 URL、标题和
    当前选中文本；它不搜索、不翻页、不批量读取 DOM，也没有任意站点 host
    permission。Dashboard 再次展示这些字段并要求用户显式确认后才保存 Lead。
  - Canonical resolver 只自动读取 recognized ATS URL；未知域名即使带
    `/jobs` 或 `/careers` 路径也只是 `UNKNOWN_WEB` Lead。对
    LinkedIn/Indeed/Glassdoor/unknown-web 线索先尝试已配置 company feed，
    再用经授权的 Web Search API 查找相同 company + title 的官方页面。自动
    解析只接受唯一且提示匹配的 `SourceJobObservation`；零个或多个候选都保留为
    `NEEDS_USER`。用户可在 **Needs your review** 中粘贴一个最终的公开
    employer/ATS URL；Jobops 验证该页后才把同一 Lead 迁移为
    `RESOLVED` 并进入现有正式 JobPosting/membership 边界。
  - Jobs 的主要单岗位入口是 AI-guided conversational finder：用户可输入
    company/title/location、自然语言线索或一个 public employer/ATS URL。
    隔离且无工具的 AI 只提取显式线索和歧义，最多请求一轮澄清；配置化的
    provider ports 执行查找，用户选择候选并显式 `ADD_JOB` 后才进入正式
    Discovery。它不是全网搜索承诺。
  - LinkedIn、Indeed 是一级 discovery source，但不是一级 authoritative data
    source。服务器不读取其登录态页面、不保存平台 Cookie，也不执行自动搜索、
    翻页或批量抓取。用户可通过 alert、搜索索引、粘贴 URL 或当前页 Clipper
    提供线索；登录、CAPTCHA、MFA、rate limit 或 anti-bot 始终由用户处理。
  - 标准化、跨运行去重、更新和 partial failure 记录。
- Job Prioritization
  - 用户可以用自然语言创建、审核和修改当前求职策略。
  - Profile 中只有一个自然语言偏好入口。隔离、无工具的 NLP interpreter
    只提取 typed draft；用户在摘要面板审查后批准，hard constraints 还需要
    单独显式确认。草稿和 SearchProfile 都不等于申请或提交授权。
  - 每次批准产生不可变、版本化的 `PrioritizationPolicy`；修改策略产生新版本。
  - Profile 直接把每条 soft preference 显示为可编辑字段。保存是一次无模型的
    精确版本更新；ROLE 行同时提供下一次 Refresh 的 title phrases，其他行继续
    由 Priority 使用。Hard constraints 仍需在 reviewed NLP flow 中明确确认。
  - 用户可在 policy draft 中审核并编辑哪些 P0–P3 等级可直接进入申请准备、哪些需要显式 promote；该 admission snapshot 随 policy 一同版本化，且不等于申请或提交授权。
  - JD 结构化分析。
  - 普通代码计算岗位年龄等 deterministic facts，并验证 approved hard constraints。
  - AI 根据当前 approved policy、CandidateSummary 和岗位事实综合建议 P0–P3。
  - `PriorityProposal` 必须覆盖 work authorization、citizenship/residency、student status 和 security clearance，并经过 schema、candidate fact、evidence、hard-constraint 和 prompt-injection validation 后才能成为决定。
  - Student-only 岗位默认作为降低优先级或需要确认的信号；只有用户批准的 hard constraint 才允许正式排除。
  - 可解释的 PriorityDecision 和 ApplicationPlan。
- Application Preparation
  - 从用户明确选择的 RUNNABLE job 创建不可变、可审计的 `ApplicationPlan`；plan-scoped 用户要求不写入全局 Agent policy。
  - automation-first：AI 和确定性代码默认异步完成安全的选择、改写、问答、Fact QA、Visual QA 与组装步骤。
  - 只有缺少可信事实、本人/法律确认、安全挑战或明确高风险审批才进入 item-scoped Human Attention；当前职位暂停时继续处理其他职位。
  - CandidateEvidence 和已审批 ResumeVersion。
  - P0/P1 定制材料，P2 复用材料，P3 暂缓。
  - Resume、Cover Letter、ATS custom-question answer proposals、事实验证、render/visual QA。
  - plan-scoped execution bundle 在装配成功时保存可恢复、hash-verified
    的 immutable envelope；历史 assembly 不自动 backfill。
  - 材料预览、修改、Gate A request 和 policy-required approval。
- Application Execution
  - plan-scoped P2c1 bundle 经正式 Gate A 后可进入一次 non-submit
    Browser/Engine execution，并在 Review 或 typed runtime handoff 停止。
  - Gate B 对持久化 Review 做离线、plan-scoped 授权判断；只有正式
    autonomy policy 或同一 review 的显式用户授权可产生 authorization。
  - Greenhouse、Lever、Ashby、Jobvite、Workday 的 deterministic adapter path。
  - Generic Adapter only when local rules/cache plus the four-key provider-neutral SemanticMapper resolve every required control; otherwise handoff。
  - login/registration、Review、Gate B、handoff、resume 和 evidence。
  - `SUBMITTED_VERIFIED`、`SUBMIT_UNKNOWN` 和 duplicate-submit protection。
- Product surfaces
  - Guided Dashboard navigation is limited to Home, Jobs, Applications,
    Profile, and Settings.
  - Home explains the product, derives one contextual next step from formal
    read models, presents the four-stage user pipeline, and prioritizes Human
    Attention above job refresh or automation.
  - First-run guidance is derived from verified profile, enabled SearchProfile,
    and subject Job Library state; it does not persist a separate onboarding
    flag.
  - Jobs owns the manual Refresh action, match explanations, filters, and
    subject-scoped library status. It exposes a compact **Find a specific job**
    input with Add and Clear actions; provider sources and SearchProfiles remain
    runtime configuration, not a user-facing Dashboard form. The input accepts
    a public URL or named job clues, permits at most one clarification, then requires candidate and
    add/apply-intent choices. Both paths converge on the existing normalized
    JobPosting and SubjectJobLibraryMembership boundaries; model output cannot
    write the library directly.
    Jobs shows only a read-only description and list of the currently approved
    preferences; it has no preference input, edit, approval, or save controls.
    It also keeps unresolved `JobLead` records in a separate **Needs your
    review** section. Those rows retain their source attribution and never
    count as matched, high-priority, ready, or application-capable jobs. Each
    row lets the authenticated subject submit one final public employer/ATS URL
    for verification; platform URLs still stop without a server-side read.
    Refresh starts asynchronously and exposes subject-scoped SEARCHING,
    IMPORTING and PRIORITIZING progress. It distinguishes provider requests
    that returned a valid zero-match result from requests that failed, reports
    filtered candidates before and after canonical-URL de-duplication, and
    reloads the formal Jobs projection incrementally as candidate imports become durable
    instead of waiting for serial AI Priority evaluation to finish.
    With an active policy, legacy per-title SearchProfiles no longer form many
    independent exact-title constraints: each configured company source is read
    once and all approved ROLE title phrases are OR-matched locally. This is
    exhaustive only within configured company feeds and the 1,000-result
    per-source safety bound; it is not a market-wide platform search claim.
    Applications owns five-stage user progress, attention, readiness,
    submitted, uncertain, and system-issue groupings. **Continue automatic
    applications** is a non-blocking session action: the explicit click enables
    `AUTO_REQUEST_APPLICATION` for the subject's enabled SearchProfiles,
    refreshes the library and Priority, then advances a finite ordered snapshot
    one job at a time. The page restores and polls the current session after a
    reload and shows every terminal outcome instead of silently discarding
    `COMPLETED`, `NOOP`, or `UNCHANGED`.
    A visible **Stop automatic applications** control requests a cooperative
    stop. The current job is allowed to reach a persisted safe checkpoint, no
    next job starts, and no submission click is cancelled or retried. Review,
    CAPTCHA, MFA, login, Gate, and uncertain-submission boundaries still stop
    for human attention. A non-empty library containing `NOT_EVALUATED`, high
    match, or ready jobs points Home to this action rather than reporting
    `ALL_CAUGHT_UP`. A verified formal job remains visible as `NOT_EVALUATED`
    when no ACTIVE PrioritizationPolicy exists; this visibility does not make
    the job high-match, runnable, or application-authorized.
    A persisted `REVIEW_READY` application that is waiting only for Gate B is
    shown in **Ready**, not as a system failure. **Review and submit** opens an
    exact application summary (job, ATS, included materials, prepared-answer
    count, unresolved-control count, review time and review fingerprint).
    **Confirm and submit** is the action-time explicit authorization for that
    application and that reviewed version only. The server rereads every
    binding before execution, rejects stale or duplicate confirmation, and
    never retries a submission whose evidence is uncertain.
    While Private Home CSV remains the supported compatibility queue, its
    current `REVIEW_READY` records appear in the same **Ready** tab and use the
    same modal and confirmation wording. The frontend delegates that click to
    the existing `submit-reviewed` engine through the server-owned Keychain
    and Chromium runtime; it does not create a second submission workflow.
  - Profile owns **What kind of job do you want?**. It exposes verified facts,
    source summaries, editable fields for every approved soft preference, and
    the reviewed NLP editor for adding or restructuring preferences and
    confirming hard constraints. Exact edits are saved as a new approved
    PrioritizationPolicy version. Profile also exposes answers and review
    capability without reading legacy `profile.yaml`.
  - Settings contains server-managed AI, Browser, and Automation information;
    advanced and destructive legacy actions are removed from primary
    navigation and cannot bypass authenticated production controllers.
  - The production Dashboard is a single-machine, loopback-only surface. On a
    first local load it establishes the server-configured subject session
    through the same-origin authentication endpoint; browser-supplied subject
    IDs never select another candidate partition.
  - A subject with no Job Library memberships receives a formal `EMPTY` Jobs
    view without first requiring a PrioritizationPolicy or CandidateSummary.
    Authentication/configuration failures and partial Automation outcomes are
    shown as failures or attention states rather than as empty product data.
- Durable operation
  - 所有入口使用相同的业务服务和状态规则。
  - 私有数据、材料、queue、events 和 browser state 保存在 Private Home。
  - fixture metrics 与 live metrics 分开记录。

## 明确不做什么

- 不承诺支持所有 job source、ATS 或自建申请网站。
- 不让运行时模型自由调用 browser、filesystem、MCP、email 或提交工具。
- 不允许模型输出的 `PriorityProposal` 直接改变 Priority、数据库状态、审批结果或 browser action。
- 不把未经处理的前端 policy 文本当作 system instructions；approved policy 始终是受控业务数据。
- 不把 raw/full-page text、候选人值、凭据、cookie、完整简历或完整 HTML 发送给 SemanticMapper。
- 不虚构或推断身份、工作许可、经历、教育、日期、指标、薪资或 self-identification。
- 不绕过 CAPTCHA、MFA、anti-bot、account lock 或 mailbox security warning。
- 不把 Submit click 当作成功，也不自动重试未知提交。
- 不默认启用 outreach、cold email、LinkedIn networking 或 follow-up。
- 不把 LinkedIn、Indeed 或 Glassdoor 搜索页实现成服务器端、无人值守、批量
  翻页的 scraper，也不把搜索索引、alert 邮件或 Clipper 摘要宣传成平台官方
  集成或经过验证的职位。
- 当前不提供 Google/Bing search adapter、Gmail OAuth、每日 scheduler/digest、
  LinkedIn/Indeed Saved Jobs 批量导入，也没有安装扩展后访问真实平台的
  自动验收。当前经授权搜索 adapter 只是可选 Brave Search API。
- 不为了“Agent 化”引入通用 Agent runtime、多 Agent 编排或自由工具循环。

## 成功标准

- 用户可以从一次岗位更新开始，完成 Priority、材料、Review、提交或 handoff，并看到准确的下一步。
- 手动、定时、CLI、UI 和 Codex 入口调用同一套业务规则，不产生平行状态。
- 每个 JobPosting 分开保存 `source_platform` 和 `ats_type`，重复岗位不会产生重复申请。
- 每个未验证来源先产生 `JobLead`；只有解析到唯一、可读取且提示匹配的官方
  employer/ATS observation 后才允许创建 JobPosting 和 subject membership。
- 用户可以查看当前 active `PrioritizationPolicy`，修改后得到新版本，并可在后续 Slice 中重新评估已有岗位。
- 每个 PriorityDecision 绑定 job revision/content hash、policy ID/version、candidate summary version 和 agent/prompt/model version。
- 每个 PriorityDecision 解释为什么值得优先、匹配了哪些偏好、存在哪些顾虑，以及是否违反 approved hard constraint。
- 每个 PriorityDecision 都保留完整 eligibility evidence coverage；缺失学生身份不能被静默忽略，soft eligibility concern 也不能被擅自升级为 `EXCLUDED`。
- Priority 判断只产生业务决定；它不直接启动材料生成或申请执行。
- 所有生成 claim 都能追溯到允许使用的 CandidateEvidence；没有 unsupported fact 进入材料或 ATS answer。
- ATS 只能上传当前 job/revision 已审批且 hash 一致的 MaterialPackage。
- supported deterministic ATS 的正常路径 model calls 必须等于 `0`；sanitized fixture Review arrival 不低于 95%。
- SemanticMapper 每个 ApplicationRun 最多一次 dispatch，永远不接收候选人值；无结果时安全 handoff。
- `SUBMITTED_VERIFIED` 的 eligible EvidenceRef coverage 为 100%；`SUBMIT_UNKNOWN` 永不自动 retry。
- 候选人数据不进入 Git、argv、日志或测试 fixture；秘密不进入任何 model prompt。
- 每个模型能力只接收完成该任务所需且明确允许的最小 evidence；SemanticMapper 接收零候选人值。
- 每个已修复的生产失败都有 sanitized regression fixture 和自动测试。
- The local Dashboard must distinguish an unauthenticated session, forbidden
  origin, missing endpoint, unavailable production composition, partial
  Automation result, and a legitimate empty Job Library. Static UI checks or
  in-process route tests alone do not establish real-browser E2E completion.
