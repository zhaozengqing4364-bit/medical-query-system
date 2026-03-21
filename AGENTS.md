# AGENTS.md — 高新医疗 UDID 查询系统

  > Version: v4.0
  > Last Updated: 2026-03-02
  > Scope: 项目根目录及全部子目录
  > Runtime: Codex CLI / Claude Code / Multi-Agent

  ## 1. 项目哲学与核心约束（Philosophy & Guardrails）

  ### 1.1 核心哲学
  - YOU MUST 把代码与运行结果当作唯一事实来源，文档必须追随代码真实状态。
  - YOU MUST 在实现前明确目标、约束、验收标准；目标不清时先澄清，禁止盲改。
  - YOU MUST 维持 Fail-Fast：发现关键前提失效时立即停止、报错、回写，不做静默兜底。
  - YOU MUST 让每次修改可追溯到任务标识与变更标识（commit/PR/变更集）。
  - NEVER 用“看起来成功”替代真实成功（例如伪造同步完成、吞异常后继续流程）。

  ### 1.2 不可突破约束
  - YOU MUST 保留医疗数据链路的审计性：同步、导入、向量构建、检索均需可复盘。
  - YOU MUST 强制密钥安全：`SECRET_KEY`、`SYNC_API_KEY`、`ADMIN_DEFAULT_PASSWORD` 弱值即拒绝启动。
  - YOU MUST 维护 API 契约稳定：错误码、字段语义、鉴权边界不可随意漂移。
  - NEVER 默认执行破坏性操作（drop/truncate/kill -9/强制覆盖）。
  - NEVER 在前端拼接不受信任 HTML（`innerHTML` + 未转义数据）。

  ### 1.3 IN SANDBOX 规则
  - IN SANDBOX 先做只读检查，再做最小修改，再做可验证回归。
  - IN SANDBOX 禁止输出真实密钥、生产凭据、用户敏感数据样本。
  - IN SANDBOX 若需高风险命令，先提供可替代方案与风险说明。
  - 反例：为“图省事”直接删除锁文件或强制 `kill -9` 占用进程。

  ### 1.4 规范回写触发（强制）
  - 技术路径变化（复用替代自研、同步链路改道、存储策略变更）。
  - 关键假设失效（API 响应结构、DB schema、权限模型不成立）。
  - 出现计划外约束（性能、安全、兼容性、发布窗口）。
  - 任务边界变化（新增/删减子任务影响交付范围）。

  ## 2. 技术栈与架构地图（Tech Stack & Architecture）

  ### 2.1 技术栈（当前基线）
  - Python `>=3.9`
  - Flask `>=2.3,<3.0`
  - Flask-CORS `>=4.0,<5.0`
  - Gunicorn `>=21,<23`
  - Requests `>=2.28,<3.0`
  - Pandas `>=1.5,<2.0`
  - NumPy `>=1.24,<2.0`
  - FAISS CPU `>=1.7.4`
  - SQLite 主库（当前生产基线）+ PostgreSQL 迁移规划（候选）

  ### 2.2 架构地图（@引用语法）
  - Web/API 入口：`@udid_server.py`
  - 同步监控服务：`@sync_server.py`
  - 自动同步编排：`@auto_sync.py`
  - 数据湖与队列：`@udid_hybrid_system.py`
  - 向量与检索：`@embedding_service.py` `@embedding_batch.py` `@embedding_faiss.py`
  - 外部同步：`@udid_sync.py`
  - 前端页面：`@udid_viewer.html` `@admin.html` `@login.html` `@sync_monitor.html`
  - 部署脚本：`@deploy.sh` `@start_sync_server.sh` `@nginx-gaoxin-medical.conf`

  ### 2.3 文档指针（主文件仅放指针）
  - 修复台账：`@docs/修复计划.md`
  - 审查清单：`@docs/审查问题.md`
  - 部署参考：`@DEPLOYMENT_GUIDE.md`
  - 迁移规划：`@MIGRATION_PLAN.md`
  - 原则：AGENTS 主文件只放约束与路由，细节沉淀到上述文档。

  ### 2.4 Cascading Rules（层级加载）
  - 根目录 AGENTS 约束优先于通用偏好。
  - 子目录若新增 AGENTS，仅允许“收紧规则”，不允许削弱安全基线。
  - 冲突处理：`System > Developer > User > 深层 AGENTS > 浅层 AGENTS`。

  ## 3. 代码风格与约束（Code Style & Constraints）

  ### 3.1 Python 约束
  - YOU MUST 使用显式异常边界；可预期错误返回业务错误，不可预期错误统一内部错误。
  - YOU MUST 保持函数职责单一；超过 80-120 行优先拆分辅助函数。
  - YOU MUST 对外部 I/O（HTTP、文件、DB）设置超时、重试策略或清晰失败路径。
  - NEVER 在 `except Exception` 中无条件吞错并继续关键流程。
  - 推荐：新增逻辑优先写成可测试纯函数，再接入副作用层。

  ### 3.2 API 与鉴权约束
  - 所有 `/api/*` 路由必须声明鉴权策略：匿名/登录/管理员/API-Key。
  - 修改状态的请求必须走 CSRF 或等价防护（session 模式）。
  - 错误响应统一结构：`{"success": false, "error": "..."}`。
  - 反例：同步失败仍返回“成功”文案，或接口语义名实不符。

  ### 3.3 前端安全约束
  - 用户可控内容使用 `textContent` 或安全模板渲染。
  - 禁止内联事件拼接（`onclick="...${user}..."`）。
  - 逐步推进 CSP/SRI/本地静态资源替换，避免 CDN 供应链漂移。

  ### 3.4 数据与并发约束
  - SQLite 路径保持串行化或显式锁策略，避免隐式并发写冲突。
  - 队列状态流转必须原子化：`pending -> processing -> completed/failed`。
  - 文件锁遵循“不删锁文件、仅释放锁”策略，避免 inode 竞态。

  ## 4. 已知坑与修复方案（Pitfalls & Fixes）

  ### P0（必须立即阻断）
  - 弱密钥/默认口令：启动即失败，禁止自动降级为弱安全。
  - 同步鉴权 fail-open：`SYNC_API_KEY` 缺失不得放行。
  - 存储型或属性型 XSS：发现即修，且补充回归用例。
  - 反例：将占位值写入 `.env` 后继续上线。

  ### P1（高优先，必须在当前迭代闭环）
  - 队列非原子认领导致重复处理与成本膨胀。
  - 批处理状态失真（例如“导入失败却标记 imported”）。
  - 索引与映射非原子落盘导致快照撕裂。
  - 同步接口契约名实不符（`full/data/vectors` 实际同路径）。

  ### P2（结构优化项）
  - SQLite 到 PostgreSQL 迁移前，必须先冻结接口契约并做一致性校验。
  - 前端 CSP 与静态资源本地化需纳入发布流水线而非人工步骤。
  - 配置中心化与 schema 校验应逐步替代散落 `.env` 读取。

  ### Sandbox 注意事项
  - IN SANDBOX 禁止直接处理 15GB 生产库做破坏性实验。
  - 先在副本库/样本库验证迁移、重建、批处理再推广。
  - 对“可能耗时很长”的任务使用 awaiter 子代理并保留超时策略。

  ## 5. Checklist 与权限（Checklist & Permissions）

  ### 5.1 新项目快速启动 Checklist
  - [ ] 复制 `.env.template` 到 `.env` 并填写强密钥。
  - [ ] 验证 `SECRET_KEY`/`SYNC_API_KEY`/`ADMIN_DEFAULT_PASSWORD` 非占位值。
  - [ ] 启动 `udid_server.py` 与 `sync_server.py`，确认端口 `8080/8888`。
  - [ ] 运行关键健康检查：`/api/stats`、`/api/sync/status`、登录链路。
  - [ ] 抽样验证同步、向量、检索全链路日志可追溯。

  ### 5.2 提交前 Checklist
  - [ ] 变更是否触发规范回写？若是，先更新文档再提交代码。
  - [ ] 是否新增/修改了鉴权边界、错误码、响应结构？
  - [ ] 是否覆盖至少 1 条失败路径测试（网络失败、空值、权限失败）？
  - [ ] 是否确认未引入明文密钥、调试后门、破坏性默认行为？

  ### 5.3 多 Agent 分配（Codex）
  - Agent A（后端 API）：`udid_server.py`、认证、契约校验。
  - Agent B（同步链路）：`sync_server.py`、`auto_sync.py`、锁与状态机。
  - Agent C（向量链路）：`embedding_*`、索引一致性、批处理恢复。
  - Agent D（前端安全）：`*.html` 渲染安全、交互回归。
  - Agent E（部署运维）：`deploy.sh`、Nginx、systemd、日志轮转。
  - 主代理职责：统一验收口径、冲突裁决、最终集成验证。

  ## 6. 工作流与工具（Workflows & Tools）

  ### 6.1 标准工作流
  - 1) `Discover`：先读 `@docs/修复计划.md` 与目标模块代码。
  - 2) `Plan`：任务 >5 次工具调用时进入 Plan Mode 并写执行计划。
  - 3) `Implement`：小步提交，保持原子变更与单一责任。
  - 4) `Verify`：运行最小可证明验证（接口、脚本、关键路径）。
  - 5) `Writeback`：更新受影响文档与 Checklist 状态。

  ### 6.2 Context 管理 9 条（2026）
  - 1. 先事实后结论：优先代码、日志、真实响应，不用想象状态。
  - 2. Progressive Disclosure：先加载主文件，再按需加载引用文档。
  - 3. 主文件只放指针：细节放 `docs/`，避免主上下文膨胀。
  - 4. 每 30-45 分钟重置任务上下文，保留“最小事实摘要”。
  - 5. 复杂任务先拆子代理，避免单代理串行过载。
  - 6. 子代理输出必须结构化（结论/证据/风险/建议）。
  - 7. Skill 优先复用：能走 skill 不手搓重复流程。
  - 8. 真实数据优先：关键判断基于真实样本与运行日志。
  - 9. 上下文占用 >70% 时立即做摘要压缩并清理无关分支。

  ### 6.3 Codex CLI / Worktree / 子代理
  - 推荐命令：`rg` 检索、`git worktree add` 隔离并行任务、`git diff --stat` 快速核对。
  - 大改动必须使用 worktree；禁止多主题混在同一变更集。
  - 长任务、测试、监控等待必须使用 awaiter 子代理。
  - 可并行任务优先并行执行，减少主线程阻塞。

  ### 6.4 Skill 路由约定
  - 需求不清：`$requirement-analyzer` 或 `$requirement-architect`
  - 方案设计：`$writing-plans` / `$adaptive-architecture-governor`
  - 实施执行：`$executing-plans` / `$subagent-driven-development`
  - 回归审查：`$requesting-code-review` / `$parallel-code-review`
  - 完工核验：`$verification-before-completion`

  ### 6.5 gstack
  - 所有网页浏览一律使用 gstack 的 `/browse` 技能。
  - NEVER 使用 `mcp__claude-in-chrome__*` 工具。
  - 当前可用 gstack 技能：`/office-hours`、`/plan-ceo-review`、`/plan-eng-review`、`/plan-design-review`、`/design-consultation`、`/review`、`/ship`、`/browse`、`/qa`、`/qa-only`、`/design-review`、`/setup-browser-cookies`、`/retro`、`/investigate`、`/document-release`、`/codex`、`/careful`、`/freeze`、`/guard`、`/unfreeze`、`/gstack-upgrade`。
  - 如果 gstack 技能不起作用，运行 `cd .claude/skills/gstack && ./setup` 以构建二进制文件并注册技能。

  ## 自我进化协议（Self-Evolution Protocol）——活文档核心

  YOU MUST 执行自我进化循环：
  每当以下任意情况发生时（任务结束、新坑发现、用户反馈、git commit 分析后、每月最后一天、sandbox 日志异常、上下文占用 >70%），你
  必须主动：
  1. 分析最近 7 天 fix 模式、MCP 路由记录、子代理输出、用户反馈。
  2. 生成“AGENTS.md vX.XX 更新建议”完整版本（包含新增约束、Pitfalls、Checklist、Skills 片段、上下文管理优化）。
  3. 以清晰 diff 格式输出新旧对比 + 版本变更记录。
  4. 推荐是否需要新建 `.codex/skills/xxx.md` 并用 `$skill-creator` 实现。
  5. 提醒用户：“请 review 并 commit 此更新，让 AGENTS.md 继续进化”。
  6. 如果用户确认，则直接输出完整更新后的 AGENTS.md 供复制保存到项目根目录。
