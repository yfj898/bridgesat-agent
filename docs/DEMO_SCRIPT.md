# BridgeSAT Agent — 比赛演示脚本（7 步，全部可复现）

> 本脚本对应 `IMPLEMENTATION_CHAIN.md` 第 6 节的 7 步演示路径。用干净环境
> 从零走完，每一步给出现成命令与预期输出。所有步骤在 local memory mode 下
> 零外部依赖，RAG/同步/安全演示均已包含在回归套件中。
>
> 前置：Python ≥ 3.11、Node ≥ 18、已安装 `pip install -e '.[dev]'`。

## 0. 干净环境（一次性准备）

```bash
rm -rf data/bridgesat.db
mkdir -p data
python scripts/import_content_pack.py        # 导入 approved 55 items + 16 lessons，构建 FTS5
python scripts/seed_demo.py                  # 捏造虚构 learner + 两会话记忆叙事
```

预期：import 打印“55 approved、16 lessons 已入库、FTS 索引完成”；
seed 打印 `Seeded demo student ...` 与 mastery 汇总。

---

## 1. 学生完成短诊断，得到限时数学计划

```bash
uvicorn app.main:app --reload                # http://127.0.0.1:8000
```

新开 3 个终端分别验证（`curl`）：

```bash
# 创建学生，拿回 Bearer token
curl -s -X POST localhost:8000/v1/students \
  -H 'content-type: application/json' \
  -d '{"name":"Demo Student","daily_minutes":20,"target_score":1200}'
# → {"id":"...","token":"..."}，把 token 存到 $TOKEN

# 短诊断（示例答案对应 pack 的正确答案；先看 /v1/questions 拿真实 id）
curl -s -X POST localhost:8000/v1/diagnostics \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"answers":[{"question_id":"math.linear_equations.001","selected_answer":"11","hint_level":0}]}'
# → mastery 五技能，weakest 按答错/未测分布（weakest_skills 前二进计划）
# → mastery 五技能，weakest 为 linear_equations（weakest_skill）

curl -s -X POST localhost:8000/v1/adapt \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"skill":"linear_equations","was_correct":false}'
# → 下次题目难度上升，提示门控在迟到阶段关闭
```

> 无 token 时 `/v1/diagnostics`、`/v1/adapt`、`/v1/sync/*` 全部 401。scope
> 一律取自 token：`/v1/sync/events` body 里的 `student_id` 与 token 不一致
> 时返回 403(该字段已从 diagnostics/adapt 的 body 中移除，不再需要)。
> 可用 `curl -i` 看状态码。

---

## 2. Session 1 学习闭环（sign_error 叙事）

用已 seed 的 demo 学生会话（两会话内存故事在 seed 里已生成，token 会随
`seed_demo` 一起打印；若已 seed 过则用脚本新打印的 token 或沿用首次的）：

```bash
export DEMO_TOKEN=<seed_demo 打印的 token>
curl -s "localhost:8000/v1/sync/snapshot" \
  -H "Authorization: Bearer $DEMO_TOKEN" | python3 -m json.tool
```

snapshot 展示：

- `device_sequence` 单调递增（`demo_device` 已推进）；
- `episode` 里 `linear_equations` 的 `reason_code=sign_error` 有 approved worked
  example；transfer item（`math.linear_equations.003`）在无提示下答对；
- `memory_snapshot` 中带 `source_lineage` / `license` / `review_status`。

对照：`tests/golden/test_two_session_memory.py` 与 `reports/offline_sync_eval.json`。

> 注意：seed 的学生是演示专用,不带 API 创建记录；如需从诊断开始，用第 1 步
> 新建的学生走完整流程。

---

## 3. Session 2 首次相似错误即召回（两会话证明）

打开 PWA（浏览器访问 `http://127.0.0.1:8000`），会话 2 遇到同样的
`sign_error` 作答：

- 页面展示被召回 episode（同样的 `reason_code`）、以及“此前 worked example
  有效”的副词解释；策略选择 `intervention`（示例答案 clips）。
- 复现命令（同 seed 叙事）：

```bash
python -m evals.run_all   # 全量评估，其中 similar_sqlite 召回@3 与 next-action=1.00
```

若想看无记忆 baseline 的并排表：查看 `reports/final_summary.md` 里的
表格（no_memory 0.00 vs similar 1.00）。

---

## 4. 离线 / 弱网 / 恢复 / 幂等同步

（在 PWA 或直接走 API）

1. 断网：答题 -> 请求提示 -> 刷新页面 -> 会话与待发事件从 IndexedDB 恢复。
2. 恢复联网 -> 触发一次同步（页面“Sync now”按钮）；
3. 重复点击同步：不重复计分（`event_id` 幂等去重），
   `accepted_event_ids` 不再增长、`duplicate_event_ids` 回显。

验证：`node --test web/tests/*.test.js`（21 个 Web 核心流用例）、
`tests/test_sync_protocol.py`、`reports/offline_sync_eval.json`。

---

## 5. RAG 治理与受限来源零命中

```bash
python scripts/run_retrieval_evals.py   # → reports/rag_eval.json
# 断言：College Board/Khan/OpenStax 受限来源命中数 = 0；
#       引用/许可覆盖率 1.0
```

页面上每个干预卡片展示 source lineage、license、review status 三要素。

---

## 6. 会话记忆闭环：Mnemis 不可用时 SQLite fallback

```bash
# enhanced 模式（有 Mnemis 网关）
BRIDGESAT_MODE=enhanced uvicorn app.main:app --reload
# 把 Mnemis 停掉或制造 800ms 超时 -> 自动降级 SQLite similar cohort，
# 学习闭环不中断，检索到同事件（metrics 里 fallback rate=1.00）

python -m evals.run_all   # 全量评估 [ok]
```

验证：`tests/test_fallback_memory.py`、`tests/test_mnemis_backend.py`、
`tests/security/test_timeout_fallback.py`。

---

## 7. 恢复能力 / 证据 / 全套验证命令（一键）

```bash
# 备份与恢复
python scripts/restore_sqlite_backup.py --backup data/backups/<file> --target data/bridgesat.db

# 重建投影（故障后自愈）
python scripts/rebuild_learner_projections.py   # demo 库：13 事件 → 17 行投影

# 重建 memory 索引（幂等 + 死信重放）
python scripts/rebuild_memory_index.py
python scripts/verify_memory_parity.py         # 逐学生比对，exit 0 放行

# 死信重放（修复根因后）
python scripts/replay_dead_letter.py [--db data/bridgesat.db]

# 全套验证（4 组）
python -m pytest                             # 244 passed
python -m evals.run_all                       # 7 组评估全部 [ok]
node --test web/tests/*.test.js               # 21 passed, 0 failed
bash scripts/audit_secrets.sh                 # 密钥扫描 clean
```

最后回到 `docs/EVIDENCE_PACK.md` & `README.md`，确认“已测量”表格与 `reports/` 一致。

---

## 常见失败与诊断

| 现象 | 检查 |
|---|---|
| `import_content_pack` 报错找不到 `data/bridgesat.db` | 先 `mkdir -p data` 或给 `--db` |
| 401 全部接口 | token 需要先 `POST /v1/students` 获取并在 Header 携带 |
| `seed_demo` 提示已 seed | 已幂等，可直接进入第 3 步 |
| sure me 物理性能指标全绿但 PWA 空白 | 关掉 AdBlock / 用无痕窗口，/v1/questions 必须 200 |