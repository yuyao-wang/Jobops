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

- Job Discovery：manual/scheduled trigger → `SearchProfile` → source collection → normalized, deduplicated `JobPosting` list。
- Prioritization：job revision → JD analysis → hard filters → explainable `PriorityDecision` → ordered queue。
- Preparation：`ApplicationPlan` → selected `CandidateEvidence` / base resume → constrained materials → validation → approval by the policy-required actor。
- Execution：approved bundle → ATS fill/read-back → Review/Gate B → single submit/evidence；required unresolved controls only pass through value-free mapping before local value resolution。

Manual update 和 scheduled update 必须调用同一个业务入口；只允许 `trigger` 不同。

## MVP 范围

V1 要完成所有核心业务域的端到端功能，而不是覆盖所有外部平台。

- Job Discovery
  - SearchProfile。
  - 手动和定时更新共用同一入口。
  - 手动 URL/JD 和 Private Home CSV intake。
  - Greenhouse 与 Lever public board connectors；其他 legacy collectors 在通过同一契约前不属于 V1 supported surface。
  - 标准化、跨运行去重、更新和 partial failure 记录。
- Job Prioritization
  - JD 结构化分析。
  - hard filters。
  - match score、freshness score 和 P0–P3。
  - 可解释的 PriorityDecision 和 ApplicationPlan。
- Application Preparation
  - CandidateEvidence 和已审批 ResumeVersion。
  - P0/P1 定制材料，P2 复用材料，P3 暂缓。
  - Resume、Cover Letter、ATS custom-question answer proposals、事实验证、render/visual QA。
  - 材料预览、修改、Gate A request 和 policy-required approval。
- Application Execution
  - Greenhouse、Lever、Ashby、Jobvite、Workday 的 deterministic adapter path。
  - Generic Adapter only when local rules/cache plus the four-key provider-neutral SemanticMapper resolve every required control; otherwise handoff。
  - login/registration、Review、Gate B、handoff、resume 和 evidence。
  - `SUBMITTED_VERIFIED`、`SUBMIT_UNKNOWN` 和 duplicate-submit protection。
- Product surfaces
  - Search settings / Update Jobs。
  - Job board。
  - Material review。
  - Application run / blocker view。
- Durable operation
  - 所有入口使用相同的业务服务和状态规则。
  - 私有数据、材料、queue、events 和 browser state 保存在 Private Home。
  - fixture metrics 与 live metrics 分开记录。

## 明确不做什么

- 不承诺支持所有 job source、ATS 或自建申请网站。
- 不让运行时模型自由调用 browser、filesystem、MCP、email 或提交工具。
- 不允许模型直接改变 Priority、数据库状态、审批结果或 browser action。
- 不把 raw/full-page text、候选人值、凭据、cookie、完整简历或完整 HTML 发送给 SemanticMapper。
- 不虚构或推断身份、工作许可、经历、教育、日期、指标、薪资或 self-identification。
- 不绕过 CAPTCHA、MFA、anti-bot、account lock 或 mailbox security warning。
- 不把 Submit click 当作成功，也不自动重试未知提交。
- 不默认启用 outreach、cold email、LinkedIn networking 或 follow-up。
- 不为了“Agent 化”引入通用 Agent runtime、多 Agent 编排或自由工具循环。

## 成功标准

- 用户可以从一次岗位更新开始，完成 Priority、材料、Review、提交或 handoff，并看到准确的下一步。
- 手动、定时、CLI、UI 和 Codex 入口调用同一套业务规则，不产生平行状态。
- 每个 JobPosting 分开保存 `source_platform` 和 `ats_type`，重复岗位不会产生重复申请。
- 每个 PriorityDecision 包含 score breakdown、hard-filter result、reason 和 scoring version。
- 所有生成 claim 都能追溯到允许使用的 CandidateEvidence；没有 unsupported fact 进入材料或 ATS answer。
- ATS 只能上传当前 job/revision 已审批且 hash 一致的 MaterialPackage。
- supported deterministic ATS 的正常路径 model calls 必须等于 `0`；sanitized fixture Review arrival 不低于 95%。
- SemanticMapper 每个 ApplicationRun 最多一次 dispatch，永远不接收候选人值；无结果时安全 handoff。
- `SUBMITTED_VERIFIED` 的 eligible EvidenceRef coverage 为 100%；`SUBMIT_UNKNOWN` 永不自动 retry。
- 候选人数据不进入 Git、argv、日志或测试 fixture；秘密不进入任何 model prompt。
- 每个模型能力只接收完成该任务所需且明确允许的最小 evidence；SemanticMapper 接收零候选人值。
- 每个已修复的生产失败都有 sanitized regression fixture 和自动测试。
