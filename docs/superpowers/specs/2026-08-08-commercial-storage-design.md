# 商业级存储扩展与向量检索设计

日期:2026-08-08
状态:已确认(架构优先路径)

## 背景与目标

当前项目为单机 SQLite 架构:用户数据、会话、记忆、知识库检索(FTS5)全部在本地
SQLite 单文件,单 worker 运行。商业级别评估结论:

- 数据规模:知识库扩展至万级文档后 FTS5 关键词召回退化
- 用户规模:单 SQLite 文件 + 单 worker 无法水平扩展
- 检索质量:无语义召回,同义词/语义变体弱

本设计将四个商业级缺口按"架构优先"顺序落地,本次合并实现前两个:

1. **存储扩展**:SQLite → PostgreSQL 多租户共享(RLS 隔离)
2. **检索升级**:FTS5 → Milvus 向量检索 + PG tsvector 兜底

成本/延迟工程(LLM 重排缓存)与可观测性(指标/日志/SLA)为后续独立 cycle。

## 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 部署形态 | 多租户共享 PG | 商业多租户 SaaS 目标 |
| 存量数据 | 迁移现有数据 | 保留 demo 学生/会话/记忆连续性 |
| 测试基础设施 | Docker 本地 PG + Milvus | 测试跑真实服务,不引入双后端不一致 |
| 检索层 | Milvus 向量 + tsvector 兜底 | 语义召回 + 精确词召回兼顾;离线承诺保持 |
| Embedding 来源 | NVIDIA API(在线) | 复用现有 LLM 通道;无 key 自动降级 |
| 向量库 | Milvus(standalone:etcd+minio) | 用户选择;商业级 HNSW/混合检索 |

## 架构总览

```
浏览器 → FastAPI (main.py)
  ├── PostgreSQL     权威存储:学生/会话/记忆/内容注册表(tenant_id + RLS)
  ├── Milvus         向量检索:内容 embedding(每租户一个 partition)
  └── NVIDIA API     embedding + LLM 决策
降级链:Milvus 不可用 → PG tsvector 兜底(PG 原生全文搜索)
      无 API key  → 纯 tsvector(离线承诺保持)
```

组件:
- `app/infrastructure/pg.py`:PG 连接池(psycopg)、`connect()` 替代 `database.py`
- `app/knowledge/vector_backend.py`:`VectorBackend` 实现 `KnowledgeBackend` 协议
  (Milvus 检索 + tsvector 兜底)
- `app/knowledge/local_backend.py`:保留为 tsvector 兜底实现,与 vector_backend
  共用协议;调用方只依赖 `KnowledgeBackend` 协议
- 内容导入:embedding 在导入时生成并存 Milvus;查询时 query embedding 一次
  NVIDIA 调用
- 检索管线固定顺序不变(`filter → 检索 → 技能图扩展 → WEIGHTS_V1 rerank →
  citation 校验`,见 ARCHITECTURE.md §12.2),只替换检索步骤

## 数据模型与多租户

PG 表改造(migrations `0008_*` 起):

| 现有 SQLite 表 | PG 改造 |
|---|---|
| `students` | + `tenant_id TEXT NOT NULL` |
| `learning_sessions` | + `tenant_id` |
| `episodic_memory` / `memory_outbox` | + `tenant_id` |
| `knowledge_fts`(FTS5) | 删除,替换为 `content_items` + tsvector 列 |
| `schema_migrations` | 保持,迁移器升级 |

多租户策略:
- 用户数据表 `tenant_id TEXT NOT NULL` + 复合索引 `(tenant_id, ...)`
- RLS:`ROW LEVEL SECURITY`,策略 `tenant_id = current_setting('app.tenant_id')`,
  应用层每请求设置会话变量
- 内容数据(`content_items`)不加租户列:发布内容全局共享,租户只能检索不能改
- Milvus collection `bridgesat_content`,partition 按租户隔离
  (`partition_{tenant_id}`),query 带 partition 过滤

鉴权与租户解析:当前 `TokenStore` 只有 demo token。新增请求中间件:
`Authorization: Bearer <token>` → 解析 `tenant_id` → 设置 `app.tenant_id` +
RLS 上下文。demo 学生默认归 `tenant_demo`。

迁移器 `scripts/migrate_sqlite_to_pg.py`:读现有 SQLite 全部表 → 写入 PG,
所有行标 `tenant_demo`;幂等可重复运行;迁移前自动备份 SQLite 文件。

## 检索管线与向量检索

```
查询 → license/audience 过滤 + skill/misconception 过滤(SQL)
  → 向量路:query embedding(NVIDIA)→ Milvus search(tenant partition)
     命中不足/失败 → 兜底路:PG tsvector
  → 两路候选合并 → 技能图一到两跳扩展(不变)
  → WEIGHTS_V1 确定性 rerank(fts_rank 改为 vector_rank + tsvector_rank)
  → citation/version/license 校验(不变)
  → approved content 或显式 no-result
```

关键设计点:
1. 内容索引时生成 embedding:`index_pack()` 导入时逐条调 NVIDIA embedding
   写入 Milvus;SQLite 不存 embedding(权威在 PG + Milvus)
2. golden eval 8 条必须全部通过——新增 `evals/retrieval/vector_golden.jsonl`
   (向量路)与 `tsvector_golden.jsonl`(兜底路),两路独立评估
3. HOW_TO_CUES/STOPWORDS 逻辑保留;`未知主题 → 空结果` 靠 filter 后候选为空
   保证,不靠 FTS 无匹配
4. 配置:`BRIDGESAT_EMBEDDING_MODEL`(默认 `nvidia/nv-embed-qa-4`)、
   `BRIDGESAT_MILVUS_URI`(默认 `http://localhost:19530`)
5. 离线语义:无 key 或 Milvus 不可达 → 自动 tsvector 兜底,与现有 FTS5
   语义等价(tsvector 兼容词形变化)

Milvus 连接:`pymilvus` 客户端,启动时健康检查,不可达抛
`VectorUnavailableError` → 触发兜底。测试用真实 Milvus 容器。

## 测试策略、降级与部署

`docker-compose.yml`:
- `postgres`(16)
- `milvus-etcd` / `milvus-minio` / `milvus-standalone`

- `scripts/dev_env.py`:起服务并等待健康;测试 fixture 检测可达性,不可达跳过
  集成类测试并提示
- 278 个现有测试改造:依赖 `database_path` 的 fixture 连真实 PG,每测试独立
  事务/清理
- CI 化:检测 docker,`--with-docker` 全量模式 / `--without-docker` 降级模式

降级矩阵(全部有测试):

| 场景 | 行为 |
|---|---|
| Milvus 不可达 | `VectorUnavailableError` → tsvector 兜底 |
| embedding API 无 key | 不发索引 embedding;检索走 tsvector |
| PG 不可达 | 启动失败(权威库,不降级) |
| Milvus 恢复 | 自动恢复向量路,不重启 |

配置(`.env.example` 更新):`BRIDGESAT_DB=postgres://...`、
`BRIDGESAT_MILVUS_URI`、`BRIDGESAT_EMBEDDING_MODEL`、
`BRIDGESAT_EMBEDDING_API_KEY`(默认复用 LLM key)。

## 实施里程碑

- **M1 基建**:docker-compose + pg 连接池 + 迁移器骨架 + fixture 改造
- **M2 数据迁移**:全表迁移 + RLS + 租户中间件 + 迁移后 golden 测试绿
- **M3 向量检索**:vector_backend + Milvus 集成 + 双路评估绿
- **M4 清理**:删 SQLite 后端路径、文档更新、全量回归 278+ 绿

## 后续 cycle(本次不做)

- 成本/延迟:LLM 重排缓存、embedding 批处理、降频
- 可观测性:指标(检索延迟/命中率)、日志、健康检查端点、SLA 监控
