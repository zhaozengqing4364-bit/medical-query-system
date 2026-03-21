# PostgreSQL Cutover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将高新医疗系统的运行时主数据库从 SQLite 切换为 PostgreSQL，并保持 API 契约、同步链路、向量链路与审计可追溯性不回退。

**Architecture:** 采用“先兼容、再切流、最后下线”三阶段方案：先引入统一 DB 访问抽象和双后端能力（SQLite/PostgreSQL），再按读路径→写路径逐步切换，最后以配置开关切主并保留快速回滚。短期保留 FAISS 文件索引，PostgreSQL 承担业务主数据与队列状态。

**Tech Stack:** Python 3.9+, Flask, psycopg2-binary, PostgreSQL 15+, pg_trgm, zhparser/tsvector, existing FAISS.

---

## Scope & Non-Goals

- In Scope:
  - 运行时数据库连接切换到 PostgreSQL
  - `udid_server/sync_server/auto_sync/embedding_*` 的 DB 读写路径切换
  - 部署、监控、备份脚本切换
  - 迁移验证与回滚方案
- Out of Scope:
  - 本次不引入 ORM 重构（保持最小侵入）
  - 本次不移除 FAISS（先保持现状）
  - 本次不做功能新增

## Risk Gates（必须通过后进入下一阶段）

1. 数据一致性 Gate：核心表计数与抽样一致率 100%
2. API 契约 Gate：`/api/stats` `/api/search` `/api/ai-match` `/api/sync/*` 响应结构一致
3. 同步链路 Gate：`pending -> processing -> completed/failed` 状态机可复盘
4. 回滚 Gate：15 分钟内可切回 SQLite 并恢复服务

## Task 1: Baseline Snapshot（只读盘点与冻结）

**Files:**
- Read: `udid_server.py`, `udid_hybrid_system.py`, `sync_server.py`, `auto_sync.py`, `embedding_service.py`, `embedding_batch.py`, `embedding_faiss.py`
- Read: `deploy.sh`, `monitor.sh`, `backup.sh`, `requirements.txt`
- Update: `docs/修复计划.md`

**Step 1: 生成 SQLite 依赖清单**
- Run: `rg -n "sqlite3|udid_hybrid_lake\.db|sqlite_master|PRAGMA|MATCH \?|\?\)" *.py *.sh`
- Expected: 形成按模块分组清单

**Step 2: 形成 PostgreSQL 差异清单**
- Run: `rg -n "psycopg|postgres|DATABASE_URL|%s" *.py scripts/*`
- Expected: 明确已有可复用脚本与未接入点

**Step 3: 回写修复台账（规划项）**
- 在 `docs/修复计划.md` 增加 PostgreSQL 切换任务组（BUG-050+）

**Step 4: Commit**
```bash
git add docs/修复计划.md docs/plans/2026-03-02-postgresql-cutover-plan.md
git commit -m "plan: add postgresql cutover plan and task ledger entries"
```

## Task 2: Introduce DB Backend Switch（双后端开关）

**Files:**
- Create: `db_backend.py`
- Modify: `udid_server.py`, `sync_server.py`, `auto_sync.py`, `embedding_service.py`, `embedding_batch.py`, `embedding_faiss.py`, `udid_hybrid_system.py`
- Modify: `.env.template`, `config.json.template`

**Step 1: 新建统一后端配置模块**
- `db_backend.py` 提供：
  - `DB_BACKEND` (`sqlite`/`postgres`)
  - `get_sqlite_path()`
  - `get_postgres_dsn()`
  - `is_postgres()`
  - fail-fast 弱值检查（沿用 AGENTS 安全约束）

**Step 2: 先改连接初始化，不改业务 SQL**
- 每个模块先集中化 `connect()`，仍允许 SQLite 跑通
- Postgres 分支先最小实现连接+基础 ping

**Step 3: 配置模板补全**
- `.env.template` 增加：`DB_BACKEND`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`

**Step 4: Verify**
- Run: `python3 -m py_compile db_backend.py udid_server.py sync_server.py auto_sync.py embedding_service.py embedding_batch.py embedding_faiss.py udid_hybrid_system.py`
- Run: `python3 -c "import db_backend; print(db_backend.is_postgres())"`

**Step 5: Commit**
```bash
git add db_backend.py .env.template config.json.template *.py
git commit -m "refactor: add db backend switch and centralized connection config"
```

## Task 3: Read Path Migration（查询链路切换）

**Files:**
- Modify: `udid_server.py`, `embedding_service.py`
- Optional: `scripts/setup_postgres.sql`

**Step 1: 替换 SQLite 专属元查询**
- `sqlite_master` 检查改 PostgreSQL catalog 等价查询

**Step 2: 替换 FTS 查询分支**
- SQLite `products_fts MATCH ?` 分支改为 PostgreSQL `to_tsvector/plainto_tsquery` 分支
- 保留 SQLite 分支用于回滚

**Step 3: 占位符兼容层**
- SQLite 使用 `?`
- PostgreSQL 使用 `%s`
- 引入最小 query builder/adapter 避免字符串散落

**Step 4: Verify（只读 API）**
- `/api/stats` `/api/search` `/api/ai-match` 对 SQLite 与 PostgreSQL 双环境对比
- 响应字段、分页、排序一致

**Step 5: Commit**
```bash
git add udid_server.py embedding_service.py
 git commit -m "feat: add postgres-compatible read/query paths with sqlite fallback"
```

## Task 4: Write Path Migration（同步/导入/队列写入切换）

**Files:**
- Modify: `udid_hybrid_system.py`, `sync_server.py`, `auto_sync.py`, `embedding_batch.py`

**Step 1: 事务语义对齐**
- 将 SQLite 事务边界映射到 PostgreSQL 显式事务
- 确保异常路径 fail-fast，不吞错

**Step 2: 队列认领原子化（PostgreSQL）**
- 使用 `UPDATE ... SET status='processing' ... WHERE id IN (...) AND status='pending' RETURNING ...`
- 或 `FOR UPDATE SKIP LOCKED` 模式

**Step 3: system_config/sync_log/queue 表写入切换**
- 保证审计字段完整写入（created_at/updated_at/data_date）

**Step 4: Verify（写路径）**
- 跑一次完整 `sync -> import -> embedding queue -> search`
- 验证状态流转与日志可复盘

**Step 5: Commit**
```bash
git add udid_hybrid_system.py sync_server.py auto_sync.py embedding_batch.py
 git commit -m "feat: migrate write paths and queue state transitions to postgres"
```

## Task 5: Ops Migration（部署/监控/备份切换）

**Files:**
- Modify: `deploy.sh`, `monitor.sh`, `backup.sh`, `gaoxin-medical.service`, `start_sync_server.sh`
- Modify: `DEPLOYMENT_GUIDE.md`, `MIGRATION_PLAN.md`

**Step 1: 部署依赖更新**
- 安装 `postgresql-client`
- 连接健康检查由 `sqlite3` 改 `psql -c "select 1"`

**Step 2: 备份策略切换**
- SQLite `.backup` 改 `pg_dump` + 压缩
- 保留 FAISS 备份

**Step 3: 监控指标切换**
- 数据库可用性、慢查询、连接池告警

**Step 4: Verify（运维脚本）**
- 本地 dry-run + staging 验证

**Step 5: Commit**
```bash
git add deploy.sh monitor.sh backup.sh DEPLOYMENT_GUIDE.md MIGRATION_PLAN.md
 git commit -m "chore: migrate deployment, backup and monitoring to postgres"
```

## Task 6: Cutover & Rollback Drill（切流与回滚演练）

**Files:**
- Modify: `.env`, runtime service config
- Update: `docs/修复计划.md`, `docs/审查问题.md`

**Step 1: Staging 切流**
- `DB_BACKEND=postgres`
- 跑回归接口 + 同步链路

**Step 2: Production 切流窗口**
- 冻结写入
- 最后一次增量迁移
- 切换环境变量并滚动重启

**Step 3: 验收门禁**
- 关键接口成功率
- 队列状态一致性
- 抽样数据一致率

**Step 4: 回滚演练（必须实际执行一次）**
- 切回 `DB_BACKEND=sqlite`
- 恢复服务
- 记录耗时与损失窗口

**Step 5: Commit**
```bash
git add docs/修复计划.md docs/审查问题.md
 git commit -m "docs: finalize postgres cutover validation and rollback evidence"
```

## Test Matrix（最小可证明验证）

1. Connection:
- `psql` 连通性
- 应用启动健康检查

2. API Contract:
- `/api/stats`
- `/api/search`（分页、筛选、深分页）
- `/api/ai-match`
- `/api/sync/status` `/api/sync/progress`

3. Data Pipeline:
- RSS 拉取
- XML 导入
- embedding queue 处理
- FAISS 构建/查询

4. Security:
- 鉴权边界不回退
- CSRF 不回退
- 弱密钥仍 fail-fast

## Rollback Plan (Hard Requirement)

- Trigger:
  - 错误率 > 2%
  - 核心 API 连续 5 分钟不可用
  - 队列状态出现不可恢复漂移
- Actions:
  1. 切 `DB_BACKEND=sqlite`
  2. 重启 `udid_server` + `sync_server`
  3. 重新打开写入
  4. 记录事故窗口与差异数据

## Decision Items（执行前需你确认）

1. 切流策略：
- A. 单次切流（停机短、实现快）
- B. 双写过渡（风险低、实现重）【推荐】

2. PostgreSQL 环境：
- A. 本机 `localhost:5432`
- B. 独立数据库主机（请提供地址）

3. 中文检索能力：
- A. 必须 `zhparser`
- B. 先 `pg_trgm + simple`，后续再上 `zhparser`

4. 回滚窗口目标：
- A. 15 分钟
- B. 30 分钟

