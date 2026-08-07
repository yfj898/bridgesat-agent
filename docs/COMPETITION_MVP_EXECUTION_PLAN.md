# BridgeSAT 可提交比赛 MVP 实施计划（数学闭环优先）

## 1. 当前仓库真实状态审计

### 已完成

- 设计层：架构、教学、记忆一致性、同步、安全、API、评估和数据治理规范完整，且相互一致地规定 SQLite 为权威事实源、Mnemis 为可重建增强索引。
- 后端骨架：FastAPI、SQLite 学生表、诊断和简单自适应策略可运行。
- 前端骨架：静态移动端 PWA、Service Worker shell 缓存、诊断页面和在线状态提示。
- 数据治理：`config/sources.yaml` 是 fail-closed 注册表；受限来源被禁止采集；安全获取器、候选清洗、去重、技能映射、许可/年龄预检已实现。
- 数据产物：396 条唯一候选，其中 DeepMind 96、Gutenberg 100、LOC 100、GSM8K 100；89 条位于 `ready_for_rewrite`；GSM8K 已隔离；审核输出校验脚本通过，且学生批准数为 0。
- 静态校验：`python scripts/validate_review_outputs.py` 与 Python 编译检查通过。

### 部分完成

- 适应策略仅是无状态规则：重复错误会插入微课，但没有事件、会话、误概念证据、掌握度置信度或跨会话记忆。
- SQLite 仅保存 `students.mastery_json`；没有迁移、投影、并发版本、审计或恢复机制。
- PWA 仅缓存 HTTP 响应；没有 IndexedDB、离线答题、本地策略、事件队列或恢复。
- 安全采集器具备主机白名单、私网阻断、限流和哈希，但产品内容发布、RAG、审核和撤回链路尚未实现。
- 现有测试覆盖诊断、简单自适应和采集预检；当前环境未安装 `pytest`，因此未能执行完整测试套件。

### 未完成及架构差距

- 没有 `learning_events`、`agent_events`、状态机、answer attempts、Beta mastery、misconception evidence、episodes、semantic facts、intervention statistics、outbox 或 Mnemis adapter。
- 代码只有 `linear_equations`、`ratios`、`reading_inference` 三个旧技能名，与规范 taxonomy 不一致。
- `app/content/questions.json` 的 6 道 starter 题缺少版本、来源、许可快照、审核人、误概念映射和内容哈希；不得继续作为学生内容加载。
- `GET /v1/questions` 直接返回答案；当前 API 没有学生令牌、设备绑定或对象级权限控制。
- PWA 使用了拼接 `innerHTML`，虽当前字段受服务端控制，仍不应成为正式内容渲染方式。
- 没有 FTS5、内容包、引用校验、RAG、同步协议、删除传播、日志脱敏或安全响应头。
- 当前目录没有 `.git` 元数据；开始实现前应确认正确的版本控制根目录，或在获得单独授权后初始化版本控制。

本次 MVP 按已确认的“数学闭环优先”执行：发布 55 道人工批准的原创数学题，覆盖四个数学技能；阅读内容和 8 技能/80–120 题目标明确列为后续范围，不在比赛演示中暗示已支持。

## 2. 冻结的 MVP 边界与内容契约

- 将现有 starter 题移入隔离区，不再由生产 API 或 PWA 加载；只有 `published` 内容包中的项目可面向学生。
- 从 `data/reviewed/routes/ready_for_rewrite.jsonl` 选择全部 55 条具有主技能映射的数学候选：
  - `linear_equations`: 12
  - `systems_equations`: 12
  - `ratios_percentages`: 13
  - `functions_models`: 18
- 剩余 34 条 prerequisite-only 候选不进入正式题包；可作为后续先修微课灵感，但不得直接发布。
- 每条 DeepMind 候选只保留为“概念来源 lineage”；自动草稿不得复用原题表述、数值组合或选项结构。生成器以候选模块、技能和子技能为种子，重新构造题干、数值、情境与解题路径。
- 发布包目标：55 道原创四选一数学题、每个技能至少 2 个微课和 2 个 worked example；所有内容均人工审核后发布。
- 更新 `README.md`、`docs/PEDAGOGY_SPEC.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/ROADMAP.md`，把“数学闭环优先”标注为实际比赛范围，保留八技能 taxonomy 作为后续扩展，不再把它陈述为已交付能力。

正式题目 schema 固定为：

```json
{
  "id": "math.linear_equations.001",
  "version": 1,
  "domain": "math",
  "target_skill": "linear_equations",
  "target_subskill": "sign_handling",
  "required_prerequisites": ["integer_operations"],
  "difficulty": 2,
  "prompt": "原创 SAT 风格题干",
  "choices": [{"id": "A", "text": "..."}, {"id": "B", "text": "..."}, {"id": "C", "text": "..."}, {"id": "D", "text": "..."}],
  "answer_choice_id": "C",
  "misconception_map": {"A": "sign_error", "B": "inverse_operation_error", "D": "arithmetic_error"},
  "hints": [{"level": 1, "text": "..."}, {"level": 2, "text": "..."}, {"level": 3, "text": "..."}],
  "worked_explanation": "...",
  "estimated_seconds": 75,
  "source_lineage": {},
  "license": {},
  "review_status": "published",
  "reviewers": {},
  "content_hash": "sha256:..."
}
```

`id` 永久稳定；修改已发布内容时创建新 `version`，绝不覆盖历史版本。`content_hash` 对规范化正文和教学字段计算，不包含审核时间等可变字段。

## 3. 数据库与迁移顺序

采用自带迁移运行器：`app/infrastructure/migration_runner.py` + `app/infrastructure/migrations/NNNN_*.py`。每次迁移写入 `schema_migrations`，在破坏性变更前创建备份；应用拒绝连接到高于支持版本的数据库。

| 顺序 | 迁移 | 表与目的 |
|---|---|---|
| 0001 | `bootstrap_legacy_students` | `schema_migrations`；保留旧 `students` 数据，将旧 mastery 映射到可审计的初始投影。 |
| 0002 | `content_registry` | `skills`、`skill_prerequisites`、`content_sources`、`content_items`、`content_item_versions`、`content_reviews`、`content_packs`、`content_pack_items`。 |
| 0003 | `learning_session_core` | `students` 扩展、`student_tokens`、`student_skill_states`、`study_plans`、`study_sessions`、`session_items`、`answer_attempts`、`learning_events`、`agent_events`、`misconception_evidence`。 |
| 0004 | `episodic_memory` | `learning_episodes`、`student_memory_facts`、`intervention_stats`。 |
| 0005 | `memory_outbox` | `memory_outbox`，用于 SQLite 提交后异步索引、归档和删除。 |
| 0006 | `offline_sync` | `registered_devices`、`device_sync_events`、`session_branches`、`student_snapshots`。 |
| 0007 | `knowledge_fts` | 已发布内容的 FTS5 虚表、触发器和重建元数据；它是内容索引，不是事实源。 |

关键约束：

- `learning_events.event_id` 为主键，所有状态变化先追加事件、再在同一事务更新投影。
- `agent_events` 保存前后状态、bounded action、reason code/text、policy/content/taxonomy 版本、引用内容、episode IDs、在线/离线来源。
- `student_skill_states` 保存 `alpha`、`beta`、mastery、confidence、evidence_count、streak、review_due_at；服务端按教学规范重算，客户端不得提交 mastery。
- `learning_episodes` 只保存有有效上下文、观察、已实际展示的 intervention、不同题目的 outcome、版本和证据事件的候选；仅 `validated` episode 可被长期召回或索引。
- `intervention_stats` 按 `(student_id, skill, misconception, intervention, difficulty_band)` 聚合 immediate、short-term、delayed 窗口。
- 所有 learner-memory 查询必须带学生范围；Mnemis 的索引键、缓存键和删除请求也必须带该范围。

## 4. 依赖排序的实施阶段

| 阶段 | 输入 | 主要文件 | 输出与通过标准 |
|---|---|---|---|
| 0. 范围和隔离 | 当前骨架、55 个数学候选 | 修改规范状态文档；新增 `content/` 目录；隔离 `app/content/questions.json` | 生产加载器只接受已发布内容包；范围声明与实际内容一致。 |
| 1. 事件与 SQLite 记忆基线 | 迁移框架、教学规范 | `app/infrastructure/`、`app/domain/`、`app/agent/`、`app/memory/`、`tests/golden/` | 两会话黄金测试通过；这是第一批连续编码任务。 |
| 2. 正式数学内容 | 55 条候选、source registry | `content/`、`scripts/select_math_candidates.py`、`scripts/generate_math_drafts.py`、`scripts/validate_content.py`、`scripts/build_content_pack.py` | 55 条原创、审核批准、可验证内容和一个可校验数学包。 |
| 3. SQLite RAG 基线 | 已发布内容包、taxonomy | `app/knowledge/`、内容导入脚本、`evals/retrieval/` | FTS5 + 先修扩展 + citation/license 校验达到 100% 元数据覆盖。 |
| 4. 离线与同步 | 稳定事件 schema、内容包、session API | `web/offline/`、`web/sw.js`、`app/api/sync.py`、`app/domain/sync.py`、`tests/sync/` | 完整离线、刷新恢复、幂等同步、乱序和版本冲突测试通过。 |
| 5. Outbox 与 Mnemis | SQLite memory 基线、outbox 表 | `app/memory/mnemis_backend.py`、`fallback_backend.py`、worker、`evals/memory/` | Mnemis 仅在增强模式启用；超时回退和消融报告可复现。 |
| 6. 安全、评估和演示 | 全部 MVP 功能 | `tests/security/`、`evals/`、`scripts/seed_demo.py`、`docs/` | 所有关键黄金、安全、离线、恢复和内容审核门禁通过；生成比赛证据包。 |

## 5. 第一批实际编码任务：连续完成事件到两会话黄金测试

按以下顺序实现，不在此阶段引入 Mnemis、RAG 或 IndexedDB：

1. 新建迁移运行器、SQLite connection/transaction factory 和 0001–0004 迁移。
2. 将扁平 `app/models.py` 拆为 `app/domain/events.py`、`sessions.py`、`learner.py`、`memory.py`；保留 `app/main.py` 为唯一 FastAPI 入口。
3. 实现 immutable `learning_events` 和 `agent_events` repository；同一事务中完成事件追加、状态投影、attempt、misconception evidence 和 mastery 更新。
4. 实现状态机：  
   `NEW → PROFILE_READY → DIAGNOSTIC_ACTIVE → DIAGNOSTIC_COMPLETE → PLAN_READY → QUESTION_ACTIVE`；  
   `QUESTION_ACTIVE ↔ HINT_ACTIVE`，`QUESTION_ACTIVE → ANSWER_EVALUATED → {WORKED_EXAMPLE_ACTIVE | MICRO_LESSON_ACTIVE | PRACTICE_ADAPTED | QUESTION_ACTIVE}`；  
   任意活动状态可 `PAUSED`，完成路径为 `SESSION_SUMMARY → SESSION_COMPLETED`。非法 transition 返回稳定错误码且不写投影。
5. 实现 bounded action schema：`ASK_QUESTION`、三档 hint、`SHOW_MICRO_LESSON`、`SHOW_WORKED_EXAMPLE`、`RETRY_SAME_SKILL`、`LOWER_DIFFICULTY`、`RAISE_DIFFICULTY`、`SWITCH_TO_PREREQUISITE`、`SCHEDULE_REVIEW`、`END_WITH_REVIEW`、`END_SESSION`。
6. 按 `PEDAGOGY_SPEC.md` 实现 weighted Beta 更新、hint/repeat/intervention multiplier、误概念状态门槛和版本化策略。
7. 实现 intervention event、outcome window 关联、episode builder、SQLite episodic recall、semantic fact 形成和 intervention aggregation。
8. 实现 memory-aware policy：当前同技能 `sign_error` 出现时，若存在一个已验证且 outcome 成功的同学生 episode，可在第二个当前错误前选择 `SHOW_WORKED_EXAMPLE`；该行为标为“基于已验证 episode 的短期复用”，不得伪称为已满足三次观察的稳定事实。
9. 新增 `tests/golden/test_two_session_memory.py`：
   - Session 1：两个不同题目的 `sign_error` → `SHOW_WORKED_EXAMPLE` → 后续不同 transfer item 无提示答对；
   - 构建并验证 episode；
   - Session 2：相似 sign-error 的首次观察即召回该 episode，memory-aware policy 比 no-memory baseline 更早选择 `SHOW_WORKED_EXAMPLE`；
   - 断言 decision 保存 episode ID、`RECALLED_SUCCESSFUL_EPISODE`、reason text、policy version；
   - 强制 Mnemis unavailable 时 SQLite recall 仍产生允许的动作。  

通过条件：新库空数据库迁移成功；旧 student 数据迁移不丢失；重复 event 不重复更新；非法状态迁移不改变投影；两会话黄金测试 100% 通过。

## 6. 正式内容处理、审核和发布管线

### 文件与模块

```text
content/
  schemas/item-v1.json
  taxonomy/skills-v1.json
  taxonomy/prerequisites-v1.json
  candidates/math-selection-v1.jsonl
  drafts/math-v1.jsonl
  reviews/math-v1.csv
  approved/math-v1.jsonl
  packs/bridgesat-math-0.1.0/manifest.json
  packs/bridgesat-math-0.1.0/items.jsonl
  packs/bridgesat-math-0.1.0/lessons.jsonl
```

新增：

- `scripts/select_math_candidates.py`
- `scripts/generate_math_drafts.py`
- `scripts/validate_content.py`
- `scripts/build_content_pack.py`
- `scripts/import_content_pack.py`
- `tests/test_content_pipeline.py`

### 选择、生成和验证规则

- `select_math_candidates.py` 只接受 `source_id=deepmind_mathematics_dataset`、`review_route=ready_for_rewrite`、`role=question_candidate`、四个目标技能且映射置信度为 1.0 的记录；生成不可变 selection manifest。
- 草稿生成器按 lineage ID 决定性生成新的系数、上下文、四选项、干扰项、提示和 worked explanation；输出 `draft`，永不输出 `approved`。
- 每类题使用误概念模板生成干扰项，例如 sign error、错误逆运算、遗漏分配、比例倒置、单位换算、函数输入替换错误；验证四个选项文本不同、三个 distractor 各有合法映射、只有一个正确选项。
- 数学验证器使用固定版本的 `sympy` 进行精确有理数/方程/方程组/函数计算；将题目 author metadata 中的规范表达式与正确选项逐一比对，并拒绝多解、无解、近似歧义、未映射 distractor 和重复表述。
- rewrite similarity gate 比对候选原文与草稿的 token/n-gram 相似度；超过阈值阻断，要求重写。
- 审核 CSV/JSONL 必填：教育审核人、答案审核人、许可证审核人、可访问性审核人、审核时间、结论、备注、source lineage 确认和 release 批次。
- `build_content_pack.py` 仅接收所有审核字段完整且状态为 `approved` 的项目；生成 manifest、项目哈希、source/license manifest、schema version、minimum app version、撤回列表。
- 发布状态按 `draft → schema_validated → educational_review → license_review → approved → published`；撤回只影响新选题与新包，历史版本仍可审计。

## 7. API、兼容与后端重构

保持单一 FastAPI 模块化单体，不引入微服务。新增 `app/api/students.py`、`diagnostics.py`、`sessions.py`、`content.py`、`memory.py`、`sync.py`、`reports.py`，由 `app/main.py` 注册。

- `POST /v1/students` 保留路径，但创建 pseudonymous learner 和一次性返回的随机 bearer token；数据库只保存 token hash。
- 新增 `POST /v1/diagnostics/start`、`POST /v1/diagnostics/{id}/events`、`POST /v1/sessions`、`GET /v1/sessions/{id}`、`POST /v1/sessions/{id}/events`、pause/resume/complete。
- 新 session event endpoint 只接受白名单事件并返回 server-derived snapshot 与下一条 bounded decision。
- 新增 `GET /v1/content/packs`、`GET /v1/content/packs/{pack_id}`、`POST /v1/knowledge/retrieve`、`GET /v1/memory/profile`、`GET /v1/memory/decisions/{event_id}`、`POST /v1/sync/events`、`GET /v1/sync/snapshot`。
- 现有 `POST /v1/diagnostics` 与 `POST /v1/adapt` 保留一个版本作为 deprecated compatibility shim，内部转换为新事件流；新版 PWA 不调用它们。
- 现有 `GET /v1/questions` 不再返回 starter 内容或答案；过渡期仅返回已发布的公开题面，响应增加弃用标记。离线答案键只位于经校验的 formative content pack 中。
- 所有状态写入要求 `Idempotency-Key`；错误统一使用 API 规范格式；学生范围从 bearer token 推导，忽略请求体中越权的 student ID。

## 8. RAG 基线与增强门槛

实现 `app/knowledge/local_backend.py`、`hierarchy.py`、`citations.py`、`router.py`，检索顺序固定为：

```text
review_status=published + audience filter
→ license/source filter
→ skill/subskill/misconception filter
→ SQLite FTS5
→ 最多两跳 prerequisite expansion
→ deterministic reranking
→ citation/version/license validation
→ approved content 或明确的无结果
```

- 检索结果必须携带 content ID/version、source lineage、许可证、审核状态和 citation label；任何字段缺失即排除。
- 默认 reranker 使用规范中的 skill、misconception、difficulty、content-type、prerequisite、offline-availability 和 recently-shown 权重；权重有版本且仅在开发集调参。
- `evals/retrieval/` 分离开发集与最终黄金集，测量 Recall@1/3、MRR、延迟、citation coverage、license coverage、restricted-source exclusion。
- embedding、LightRAG、A-RAG、RAG-Anything 都实现为可禁用 adapter，默认 local mode 不加载。

启用条件：

- LightRAG 或 embedding 只有在最终黄金集上 Recall@3 高于 FTS5+hierarchy，且 citation/license coverage 无回退、延迟符合预算、可重复安装时启用。
- LightRAG 故障、超时或断路器打开时直接回到 SQLite FTS5，不影响答题、掌握度或会话。
- A-RAG 仅用于多技能计划和根因分析，最多 4 次检索、2 次 semantic search；不得用于普通题。
- RAG-Anything 仅用于拥有明确许可且已人工审核的多模态导入；学生请求不触发抓取或解析。
- GSM8K 永远不进入 `content/`、FTS、prompt 示例、离线包或学生检索路径。

## 9. 前端、IndexedDB 与同步

新增 `web/offline/db.js`、`evaluator.js`、`policy.js`、`events.js`、`sync.js`；修改 `web/app.js`、`web/sw.js`、`web/index.html`，所有动态文本使用 DOM text APIs，不使用不可信 `innerHTML`。

IndexedDB stores：

- `profile_snapshot`
- `active_session`
- `skill_state_snapshot`
- `strategy_memory_snapshot`
- `content_packs`
- `pending_events`
- `acknowledged_events`
- `sync_state`

`sync_state` 保存稳定 device ID、最后 device sequence、server cursor、base snapshot version 和激活内容包版本。每个离线 event 使用 `crypto.randomUUID()`、单设备单调 `device_sequence`、pack/question/version、依赖 event IDs、monotonic timestamp 和 integrity hash。

离线能力：

- 从经校验的内容包渲染题目、提示、微课和 worked example。
- 本地四选一判分、临时 Beta projection、紧凑 bounded policy、近期 strategy snapshot。
- 页面刷新后从 `active_session` 恢复，未确认事件绝不丢弃。
- 重新联网后每批最多 100 个事件上传；服务端按 event ID 去重、按内容版本判分、处理 dependencies 和 parallel branches，返回 ack、拒绝、冲突和新 snapshot。
- 已知旧版本按原 answer key 判分；未知版本拒绝；安全撤回版本停止判分并生成 remediation event。

浏览器自动化测试使用锁定版本的 Playwright，仅作为开发依赖，不引入前端构建步骤；覆盖完全离线、弱网、刷新恢复、重复上传、乱序、服务器重启和内容版本冲突。

## 10. Outbox、Mnemis、删除与安全治理

- 0005 后，episode/fact 的 SQLite 写入与 `memory_outbox` 插入必须位于同一事务。
- in-process worker 处理 `pending → processing → indexed/retrying/dead_letter`；采用规范重试时间表和稳定 idempotency key。
- `SQLiteStudentMemory` 永远可用；`MnemisStudentMemory` 仅在 `BRIDGESAT_MODE=enhanced` 配置且健康检查通过时调用；`FallbackStudentMemory` 顺序为 Mnemis 800 ms → SQLite → 离线 snapshot。
- Mnemis 只接收 validated episodes 和已证据化 facts；不得写答案、mastery、状态机或权威事实。提供 rebuild-one-student、rebuild-all、parity-check 和 replay-dead-letter 脚本。
- memory ablation 比较 no-memory、recent SQLite、similar SQLite、Mnemis System-1、Mnemis dual-route，输出 episode recall、next-action accuracy、intervention accuracy、fallback success 与延迟。
- 学生删除先停止新写入，再删除/墓碑化 SQLite 数据、写 deletion outbox、删除 Mnemis 数据并验证不可检索后才报告完成。
- 强制学生 scope、repository-level filters、Mnemis scope filters、CSP、受控 CORS、请求体限制、日志脱敏、秘密扫描与安全 headers。
- 外部内容与学生自由文本一律是数据，不能成为系统指令；LLM 仅可在可选模式下改写已批准说明或提出低置信建议，不能修改答案、mastery、审核状态、状态机或事实。

## 11. 测试、黄金场景与验收

新增测试分层：

- `tests/test_migrations.py`：空库、旧库、幂等迁移、schema 版本拒绝。
- `tests/test_session_state_machine.py`、`test_events.py`、`test_mastery.py`、`test_misconceptions.py`、`test_policy.py`。
- `tests/test_episode_builder.py`、`test_sqlite_memory.py`、`tests/golden/test_two_session_memory.py`。
- `tests/test_content_pipeline.py`：55 项 selection、schema、唯一答案、四选项、hash、reviewer、lineage、rewrite gate、审批阻断。
- `tests/test_retrieval.py`：审核/许可过滤、FTS、先修扩展、citation validation、无可用内容。
- `tests/test_sync.py`：重复、乱序、dependency、旧版题、未知版本、分支和重启。
- `tests/security/`：跨学生访问、prompt injection、memory poisoning、伪造离线事件、XSS、SSRF、删除传播。
- `web/tests/`：离线全流程、刷新、弱网和可访问性核心路径。
- `evals/`：policy、memory、retrieval、offline/sync、content audit，并生成规范要求的 JSON/Markdown 报告。

阶段门槛：

- 两会话记忆、同步、安全关键场景均为 100%。
- policy 黄金轨迹至少 20 条，整体至少 90%。
- 已发布 RAG 内容 citation/license coverage 为 100%，受限来源召回为 0。
- offline core-flow completion、duplicate-sync protection、restart recovery 均为 100%。
- local policy p95 <150 ms，FTS5 p95 <200 ms，session restore <500 ms；Mnemis 超时不阻断流程。

## 12. 并行关系、风险、范围和最终演示

### 并行与串行

- 必须串行：迁移 → event/session projection → Beta/misconception → episode → SQLite recall → 两会话黄金测试 → outbox → Mnemis。
- 可并行：正式内容草稿与人工审核可在阶段 1 后并行；IndexedDB UI 可在 event schema 冻结后并行；RAG 在第一批已发布内容包出现后开始；Mnemis 可做受限可行性调研，但不得进入主路径。
- 必须最后汇合：发布包、RAG、离线同步、安全和全量评估完成后，才进行比赛演示录制。

### 主要风险与回退

- 人工审核未完成：不发布未审核内容；演示不能假装正式内容已批准。
- Mnemis 不可复现、超时或无提升：关闭增强模式，使用 SQLite episodic/strategy memory 完成比赛主线。
- LightRAG/embedding 无测量收益：保留 FTS5+hierarchy，绝不把它们设为依赖。
- 当前无 Git 元数据或测试依赖：先确认 VCS 根目录，安装锁定 dev dependencies 后再建立可信测试基线。
- 内容或答案发现错误：withdraw 版本、生成 revocation manifest、阻止新会话选择，保留审计。
- 离线事件异常：保留本地未确认事件，服务端拒绝单条而不丢弃同批有效事件。

### MVP、增强项和明确不做项

- MVP：55 道人工批准原创数学题、SQLite 事件/记忆、两会话记忆证明、FTS5 RAG、IndexedDB 离线、幂等同步、安全与评估。
- 条件增强：Mnemis、embedding、LightRAG、A-RAG complex planner、RAG-Anything。
- 不做：微服务、多 Agent、独立大型向量数据库、未审核自动发布、College Board/Khan/OpenStax 采集或 RAG、GSM8K 产品使用、外部 LLM 作为核心闭环依赖、完整八技能承诺、教师后台。

### 最终 Demo 路径

1. 从干净环境运行迁移、导入已批准 `bridgesat-math` 包、种子化虚构 learner。
2. 学生完成短诊断，得到限时数学计划。
3. Session 1 连续两次出现 `sign_error`；Agent 记录理由并展示 approved worked example；transfer item 无提示答对，完成 episode。
4. 新开 Session 2，首次相似错误即展示被召回的 episode、reason code 和“此前 worked example 有效”的解释；与 no-memory baseline 的动作并排展示。
5. 在浏览器断网后完成一题、请求提示、刷新并恢复；联网后同步，重复点击同步不重复计分。
6. 展示 retrieved intervention 的 source lineage、license、review status；切换 Mnemis 不可用，显示 SQLite fallback 仍完成相同行动。
7. 展示生成的 policy、memory、RAG、offline/sync 与 security 报告。

### 提交前检查清单

- 所有发布项目有稳定 ID/version、审核人、来源、许可、hash 和批准状态。
- 未审核、GSM8K、受限来源和 starter 内容均无法进入学生端、FTS 或内容包。
- 迁移、重建 projection、重建 FTS、Mnemis rebuild/fallback、备份恢复均有通过证据。
- 所有关键测试、黄金评估、离线/弱网测试、安全测试和可访问性检查通过。
- local mode 无外部模型、Mnemis、向量服务或网络仍可完成主学习闭环。
- README、范围声明、运行命令、数据来源、已测量结果与未测量目标一致。
- 干净安装、demo seed、内容包 checksum、演示录制路径和公开链接均已复核。
