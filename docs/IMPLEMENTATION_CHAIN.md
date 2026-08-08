# BridgeSAT Agent — 实现链路、技术栈与功能演示

版本快照：`6da9b14 feat(gate6): submission-ready`（gate1–gate6 全部落地）。
代码侧工作全部完成并通过验证；尚未完成项仅为人工活动（可访问性走查、真人
可用性研究、演示录制）。

---

## 1. 实现链路（按交付顺序）

主计划 `docs/COMPETITION_MVP_EXECUTION_PLAN.md`，技术契约
`docs/IMPLEMENTATION_PLAN.md`（§14 交付日程），逐日执行记录
`docs/WORKLOG.md`。

| 阶段 | 名称 | 交付内容 | 关键产物 | 完成标志 |
|---|---|---|---|---|
| Phase 1 | 内容生成与治理 | 可治理内容管线：选择 → 草稿 → 精确数学校验（sympy）→ 模拟正式审核台账 → 打包 + checksum | 55 道原创四选一数学题、16 课时/例题（8 微课 + 8 例题）、`content/packs/bridgesat-math-0.1.0`、隔离 `content/quarantined/` | 内容审核 889/889 |
| Phase 2 | 学习闭环 | 诊断 → 计划 → 教学 → 观察 → 误解识别 → 选择/解释下一步 → 记录 | `app/engine.py`（诊断/计划/掌握度）、`app/agent/policy.py`（决策）、session 状态机、mastery Beta 更新、misconception 证据 | policy 黄金测试 24/24 |
| Phase 3 | 两会话记忆 | 事件日志 + episodic 记忆 + 策略有效性聚合 + 记忆感知策略 | migration 0004、`episode_builder`、`sqlite_backend`、`tests/golden/test_two_session_memory.py` | 两会话黄金测试通过 |
| Phase 4 | 治理化 RAG | 内容注册表 + 技能层级 + SQLite FTS5 + 引用/许可校验 + 受限来源排除 | migration 0005、`app/knowledge/`（router/local_backend/hierarchy/citations） | dev recall@1 1.0，受限来源 0 命中 |
| Gate 4 | 离线优先同步 | 不可变事件日志 + 幂等批次 + 版本绑定评分 + 快照 + IndexedDB 客户端 | migration 0006、`app/sync/`（protocol/service/router/versioned_scoring/content_packs）、`web/offline-core.js` + `web/offline.js` + `web/sw.js`、Node 测试套件 | 离线/弱网场景 10/10，JS/Python 完整性哈希逐字节一致 |
| Stage 5 | 记忆治理（Mnemis 网关） | memory outbox（幂等投递 + 死信重放）、Mnemis 适配器 + 确定性 stub、SQLite 降级、删除协议、一致性指标 | migration 0007、`app/memory/`（outbox/worker/mnemis_backend/mnemis_stub/fallback_backend/deletion/metrics）、`scripts/rebuild_memory_index.py` 等 | 消融：similar_sqlite / mnemis_dual recall@3 = 1.00，fallback 成功 |
| Phase 6 | 安全与评估 | 安全加固（XSS、安全响应头）、安全测试套件、7 组评估 + 编排器、demo seed、证据包 | `tests/security/`（10 组）、`evals/run_all.py`、`scripts/seed_demo.py`、`reports/`、`docs/EVIDENCE_PACK.md` | 244 测试通过，`evals.run_all` 全 [ok] |
| 提交前 | 恢复能力与收尾 | 迁移前自动备份、SQLite 恢复、投影重建（事件重放）、FTS/Mnemis 重建、README 重写 | `migration_runner` 备份、`scripts/restore_sqlite_backup.py`、`scripts/rebuild_learner_projections.py` | 备份/恢复/重建测试 6 项，干净安装复测通过 |

串行约束严格执行：迁移 → event/session 投影 → Beta/误解 → episode →
SQLite 召回 → 两会话黄金测试 → outbox → Mnemis；内容草稿、IndexedDB UI、
RAG 按许可并行汇合。

---

## 2. 技术栈与选型原因

### 2.1 必需技术（提交要求，IMPLEMENTATION_PLAN §3.1）

| 层 | 技术 | 选择原因 |
|---|---|---|
| API | FastAPI + Pydantic v2 + uvicorn | 项目既有基础；类型契约 + 自动文档，快速迭代；学习闭环完全本地确定性，无外部 LLM 依赖 |
| 权威数据 | SQLite + 版本化迁移（0001–0007） | 本地优先、可复现、重启安全；单文件便于备份/恢复与离线打包；迁移器自动应用并支持迁移前备份 |
| 学生端 | 移动优先 PWA（无构建步骤、零运行时依赖） | 弱网与离线需求；`web/app.js` + `web/sw.js`（pack 缓存 cache-first）；XSS 修复（textContent）+ 安全响应头 |
| 离线数据 | IndexedDB（7 个 store） | 会话、内容包、记忆快照、待发事件队列的浏览器持久化 |
| 核心检索 | SQLite FTS5 + 技能层级元数据 | 快（实测 p95 2.3 ms）、确定性、易打包离线；层级提供教育语义与可解释性 |
| 知识结构 | 经审核的 skill/subskill/prerequisite 图 | 教育专用层级，支撑误解→课时映射与干预归因 |
| 核心记忆 | 事件日志 + episodes + 聚合（SQLite） | 可靠的跨会话行为；不可变日志可重放重建任意投影 |
| 高级记忆 | Mnemis 适配器（HTTP 注入、800 ms 超时、降级） | 相似/全局长期召回；stub 与 SQLite fallback 保证主线永不依赖外部服务 |
| 评估 | 黄金轨迹 + 检索/记忆消融 + 分层标签 | 证明 Agent 行为而非只证明 UI；结果按 synthetic/controlled/human 如实标注 |

依赖清单（`pyproject.toml`）：仅 fastapi、pydantic、PyYAML、uvicorn；
dev 仅 httpx、jsonschema、pytest、sympy。对称性设计：JS 端 SHA-256、
canonical JSON、Beta 掌握度更新与 Python 端逐字节一致。

### 2.2 条件增强（有测量收益才启用）

| 技术 | 启用条件 | 本项目结论 |
|---|---|---|
| LightRAG | 在线关系检索优于本地混合检索 | 未启用：FTS5+hierarchy 已达标（golden recall@1 0.875） |
| A-RAG 工具 | 复杂规划需要迭代检索 | 未启用：策略在 token/步骤预算内正确 |
| RAG-Anything | 有经批准的多模态文档 | 未启用：比赛内容全是结构化 JSON/Markdown |
| Embedding 重排 | Recall@3/干预匹配有增益 | 未启用：仅增延迟，增益可忽略 |
| Mnemis（enhanced 模式） | 真实端点可用时 | 已实现网关 + 消融；默认 local 模式走 SQLite |

### 2.3 明确不做（范围护栏）

多 Agent 编排、GraphRAG、HippoRAG、无限制爬取、未审核 LLM 出题、云上唯一
评分、教师后台、图数据库作为权威学生记录、College Board/Khan/OpenStax
采集或 RAG（`reference_only`）、GSM8K 产品化（仅评估）。

---

## 3. 核心闭环的实现映射

```
diagnose → plan → teach → observe → misconception → recall → retrieve → choose/explain → record
```

| 步骤 | 实现位置 | 说明 |
|---|---|---|
| 诊断 | `app/engine.py: score_diagnostic` + `/v1/diagnostics` | 四技能诊断，Beta 更新 |
| 计划 | `app/engine.py: build_plan` + `/v1/adapt` | 限时计划、难度控制、缺口排序 |
| 教学 | session 状态机 `app/domain/sessions.py` + 内容包 | 题目 + 提示 + approved worked example |
| 观察 | 事件模型 `app/domain/events.py`（13 类事件） | 不可变追加日志 |
| 误解识别 | `app/agent/policy.py` misconception 映射 | sign_error 等 reason code + 证据表 |
| 记忆召回 | `episode_builder` + `sqlite_backend`（similar cohort）| 两会话：第二次相似错误即召回 episode |
| 内容检索 | `app/knowledge/router.py`（FTS5 + hierarchy + citations）| 引用/许可过滤、受限来源排除 |
| 选择与解释 | `policy.decide_next_action`（干预、难度、提示门控）| 确定性决策 + 可解释动作 |
| 记录 | event_store + outbox（同事务投递）| 投影、episode、Mnemis 投递均源自事件日志 |
| 离线继续 | `web/offline-core.js` 本地策略 + 事件队列 | 断网完成题目/请求提示/刷新恢复 |

---

## 4. 功能演示

### 4.1 快速启动与自检

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python scripts/import_content_pack.py   # 构建 FTS5 索引（55 items + 16 lessons）
python scripts/seed_demo.py             # 幂等演示种子
uvicorn app.main:app --reload           # http://127.0.0.1:8000
pytest                                  # 244 passed
python -m evals.run_all                 # 全部评估报告 [ok]
node --test web/tests/*.test.js         # 21 passed, 0 failed
```

### 4.2 API 演示（local mode，零外部依赖）

```bash
# 1. 创建学生并拿回 Bearer token（后续受保护接口都需携带）
TOKEN=$(curl -s -X POST localhost:8000/v1/students \
  -H 'content-type: application/json' \
  -d '{"name":"Demo Student","daily_minutes":20,"target_score":1200}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8000/health
curl -s localhost:8000/v1/questions | python3 -m json.tool | head
curl -s -X POST localhost:8000/v1/diagnostics \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"answers":[{"question_id":"math.linear_equations.001","selected_answer":"11","hint_level":0}]}'
curl -s -X POST localhost:8000/v1/adapt \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"skill":"linear_equations","was_correct":false}'
# 离线同步面：设备注册 / 事件批次 / 快照（scope 一律取自 token）
curl -s -X POST localhost:8000/v1/sync/devices \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"device_name":"demo-device"}'
curl -s localhost:8000/v1/content-packs
curl -s "localhost:8000/v1/sync/snapshot" -H "Authorization: Bearer $TOKEN"
```

### 4.3 两会话记忆演示（Stage 3 核心主张）

- Session 1：连续两次 `sign_error` → Agent 记录理由、展示 approved worked
  example；transfer item 无提示答对，完成 episode。
- Session 2（新会话）：首次相似错误即召回被引 episode、reason code、以及
  "此前 worked example 有效"的解释。
- 验证：`tests/golden/test_two_session_memory.py`；消融报告显示
  `similar_sqlite` 召回@3 与 next-action 均为 1.00，而 `no_memory` 为 0.00。

### 4.4 离线与弱网演示（Gate 4）

- 断网完成一题、请求提示、刷新页面 → 会话与待发事件从 IndexedDB 恢复。
- 恢复联网 → 同步上传；重复点击同步不重复计分（event_id 幂等去重）。
- 服务端用版本绑定评分（`versioned_scoring.py`）：离线答案只按所引题目
  版本判分，未知版本拒绝。
- 验证：`web/tests/offline-core.test.js`、`tests/test_sync_protocol.py`、
  `reports/offline_sync_eval.json`（10/10 场景）。

### 4.5 RAG 治理演示（Phase 4）

- 检索干预内容时展示 source lineage、license、review status 三要素。
- 受限来源（College Board/Khan/OpenStax）命中数 = 0；引用/许可覆盖率 1.0。
- 验证：`scripts/run_retrieval_evals.py` → `reports/rag_eval.json`。

### 4.6 Mnemis 降级演示（Stage 5）

- `BRIDGESAT_MODE=enhanced` 时经 Mnemis 网关召回；关闭 Mnemis 或 800 ms
  超时 → 自动降级 SQLite similar cohort，学习闭环不中断。
- 演示：切换环境变量重启后重复 4.3 的两会话流程，动作一致。
- 验证：`tests/test_fallback_memory.py`、`tests/test_mnemis_backend.py`、
  `tests/security/test_timeout_fallback.py`、消融报告 fallback 成功 1.00。

### 4.7 恢复能力演示（提交前收尾）

- 迁移前自动备份：有待应用迁移的旧库在升级前被复制到 `data/backups/`。
- `python scripts/restore_sqlite_backup.py --backup <file> --target <file>` 恢复。
- `python scripts/rebuild_learner_projections.py` 从事件日志重放重建全部投影
  （实测 demo 库：13 事件 → 17 行投影，快照健康）。
- `python scripts/rebuild_memory_index.py`（幂等投递 + 死信重放）、
  `python scripts/verify_memory_parity.py`（重建后逐学生比对，exit 0 放行）。

---

## 5. 已测量结果（`reports/final_summary.md`，全部可复现）

| 目标 | 类型 | 测量值 |
|---|---|---|
| 黄金轨迹 ≥20、总体 ≥90% | 设计目标 | 24/24（100%），12/12 类别 |
| 安全关键 100% | 设计目标 | 100% |
| 两会话记忆、同步、安全关键 100% | 设计目标 | 100% |
| 离线核心流/重复同步/重启恢复 | 设计目标 | 10/10 场景 |
| 引用/许可覆盖 100%、受限来源 0 命中 | 受控内部测试 | 100% / 0 |
| 内容审核 100% | 受控内部测试 | 889/889 |
| 本地策略 p95 < 150 ms | 受控内部测试 | 0.01 ms（本机） |
| FTS5 p95 < 200 ms | 受控内部测试 | 2.3 ms（本机） |
| 会话恢复 p95 < 500 ms | 受控内部测试 | 3.2 ms（本机） |
| 安全 + 同步套件 | 受控内部测试 | 74 passed |
| Web 核心流测试 | 受控内部测试 | 21 passed, 0 failed |
| 干预 vs 对照组正确率提升 | 合成模拟（非真实效果） | +5.7pp |

未测量（需真人研究）：真实教育效果；可访问性人工走查项
（`reports/accessibility_eval.md` 中标记 manual check 的项）。

---

## 6. 比赛演示路径（7 步，全部可复现）

1. 干净环境：迁移 → 导入 approved 包 → `seed_demo` 种子虚构 learner。
2. 学生完成短诊断，得到限时数学计划（4.2）。
3. Session 1 两次 `sign_error`：记录理由、展示 approved worked example、
   transfer 题无提示答对（4.3）。
4. Session 2 首次相似错误即召回 episode 与解释；与 no-memory baseline
   并排对比（4.3 的消融表）。
5. 断网答题 → 请求提示 → 刷新恢复 → 联网同步 → 重复同步不重复计分（4.4）。
6. 展示干预的 source lineage / license / review status；切换 Mnemis 不可用
   显示 SQLite fallback 完成相同动作（4.5、4.6）。
7. 展示 policy / memory / RAG / offline-sync / security 报告（4.7 命令 + 第 5 节表格）。
