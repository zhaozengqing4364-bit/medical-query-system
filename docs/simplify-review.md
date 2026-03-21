---
name: simplify-review
description: |
  代码审查与优化技能 - 审查变更代码以实现复用、提升质量、提高效率，并修复发现的问题。

  触发场景：
  1. 用户要求"简化代码"、"优化代码"、"代码审查"
  2. 用户要求"/simplify"或提到简化
  3. 需要检查代码复用、质量和效率时
  4. PR 合并前的代码清理

  核心功能：
  - 识别 git 变更
  - 启动三个并行审查代理（代码复用、代码质量、性能效率）
  - 汇总发现并修复问题
---
核心流程（增强版）
text1. 项目发现与全局索引 (Discovery & Indexing) 
   ↓
2. 分层并行子代理群审查 (Hierarchical Swarm Review)
   ↓
3. 跨模块合成、去重、优先级排序 (Synthesis & Prioritization)
   ↓
4. 安全分批修复 + 验证 (Remediation & Validation)
   ↓
5. 全面报告 + 重构路线图 (Holistic Report & Roadmap)
Phase 1: 项目发现与全局索引（Project Discovery Swarm）
Step 1.1：确定审查范围
Bashgit diff --cached --name-only          # 暂存变更
git diff HEAD --name-only              # 未暂存变更
git status --porcelain

有变更 → hybrid 模式（变更文件高优先 + 全项目上下文）
无变更或用户要求 full-project → full-project 模式

Step 1.2：启动发现子代理群（并行 4-6 个子代理）

ProjectMapper：生成目录树、语言统计、模块划分、入口点识别
GlobalUtilityIndexer：扫描所有 utils/, lib/, helpers/, common/, constants、types、hooks 等，构建可复用资产索引（函数签名、模式指纹）
DependencyGraphAgent：解析 package.json / Cargo.toml / go.mod / requirements.txt 等，生成依赖图
HotPathIdentifier：git log --since="6 months" --oneline --stat + 大文件 + 高复杂度文件识别
PartitioningStrategist：根据项目规模智能分块（按目录 / 按文件类型 / 按业务模块），输出分区列表

输出：JSON 项目地图（project_map.json） + 分区列表（chunks: [{id, files, type, priority}])
Phase 2: 分层并行子代理群审查（Parallel Review Swarms）
主协调器 启动 3 个 Review Lead（并行）：
1. Reuse Lead → 启动 N 个 Reuse Sub-Agents（并行）
每个子代理负责 1-2 个分区 + 全局 Utility Index
增强后的 Prompt（关键部分）：
text你是全项目代码复用审查专家。
你拥有完整的项目地图和全局可复用资产索引。
任务：
1. 在你负责的分区内寻找重复逻辑
2. 与全局索引对比，标记任何可直接替换为现有工具的内联代码
3. 发现跨模块的潜在新抽象机会
输出严格 JSON（每条 issue 带 file、line、confidence、suggestion）
2. Quality Lead → 启动 N 个 Quality Sub-Agents
关注冗余状态、参数膨胀、抽象泄漏、字符串类型代码、一致性违反等
3. Efficiency Lead → 启动 N 个 Efficiency Sub-Agents
重点检查热路径、N+1、串行可并行、内存泄漏、过度加载等
所有子代理共享：

完整 project_map
GlobalUtilityIndex
其他子代理已发现的初步问题（动态同步，减少重复）

并发控制：Orchestrator 使用 semaphore 限制最大并行数（默认 20，可配置）
Phase 3: 跨模块合成与智能决策

Synthesis Agent（单个高能力子代理）接收所有子代理 JSON 输出
任务：
去重 & 合并同类问题
全局相关性分析（e.g. 同一模式在 12 个文件中重复 → 建议新建 shared util）
优先级分类：
Critical（功能/安全/崩溃风险）
High（架构一致性、重构收益大）
Medium（质量/可维护性）
Low（纯优化）

生成重构路线图（分阶段、分 PR）


Phase 4: 安全分批修复

Fix Executor 按优先级 + 依赖顺序分批应用修复
每批修复后：
运行语法/类型检查
运行相关单元测试（如果存在）
回滚机制（git stash / patch revert）

仅自动修复 High 以下；Critical 标记为“需人工确认”

Phase 5: 报告总结（增强版）
Markdown## Simplify 全项目审查报告
**审查模式**：full-project / hybrid  
**项目规模**：X 个文件 / Y 个模块 / Z 行代码  
**扫描耗时**：XX 秒（并行加速 X 倍）

### 发现概览
| 类别         | 问题数 | 已修复 | 待确认 | 重构收益预估 |
|--------------|--------|--------|--------|--------------|
| 代码复用     | 42     | 38     | 4      | -1200 行重复代码 |
| 代码质量     | 67     | 61     | 6      | 提升可维护性 35% |
| 效率问题     | 19     | 17     | 2      | 预计性能提升 18% |

### 关键发现（Top 5）
1. **全局重复工具函数** → 建议新建 `utils/string.ts`
2. **13 处参数膨胀** → 重构为配置对象
...

### 重构路线图（推荐 3 个 PR）
PR #1：提取公共工具库（预计减少 800 行）
PR #2：...

### 跳过/误报说明
- ...

### 下一步建议
- 执行 `git diff` 查看所有变更
- 运行全量测试
- 考虑引入新的 lint 规则...
快速开始示例（TypeScript / JavaScript）
TypeScript// ==================== 全项目并行简化审查 ====================
const orchestrator = new SimplifyOrchestrator({
  mode: "full-project",           // 或 "hybrid", "change-focused"
  maxConcurrency: 20,
  autoFix: true
});

const result = await orchestrator.run({
  projectRoot: ".",
  focusDirs: ["src/", "lib/"],    // 可选聚焦
  exclude: ["node_modules", "dist"]
});

// 输出完整报告 + 重构路线图
console.log(result.reportMarkdown);
console.log("建议 PR 列表：", result.roadmap);
常见问题 & 边缘情况处理（增强考虑）
Q: 项目极大（>10k 文件）怎么办？
A: 自动分层（先目录级 → 再重点模块），使用文件摘要（前 200 行 + 函数签名）+ 按需完整读取
Q: 多语言 / Monorepo？
A: Discovery Agent 自动识别语言，分别启动对应子代理群（Python、TS、Go 等）
Q: 子代理 JSON 格式错误？
A: 自动重试 1 次 + fallback 到文本解析 + 记录日志
Q: 修复引入新问题？
A: 每批修复后自动运行 tsc --noEmit / eslint / pytest 等，失败立即回滚
Q: 想只审查某个模块？
A: 支持 --module=auth 或 focusDirs 参数
Q: 性能开销？
A: 所有审查均为只读 + 并行，实际耗时通常 15-90 秒（取决于项目大小）
