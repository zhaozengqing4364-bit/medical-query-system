# UDID 医疗器械智能查询系统 - 部署与移交指南

> 本文档为系统交付文档，指导客户在新电脑上完成系统部署。

---

## 📋 目录

1. [系统概述](#1-系统概述)
2. [交付物清单](#2-交付物清单)
3. [环境要求](#3-环境要求)
4. [安装步骤](#4-安装步骤)
5. [配置说明](#5-配置说明)
6. [启动与使用](#6-启动与使用)
7. [数据管理](#7-数据管理)
8. [常见问题](#8-常见问题)
9. [技术支持](#9-技术支持)

---

## 1. 系统概述

**UDID 医疗器械智能查询系统** 是一个基于 AI 的医疗器械唯一标识查询与匹配系统。

### 核心功能
- **智能搜索** - 支持产品名称、规格型号、企业名称全文检索
- **AI 语义匹配** - 基于大语言模型的需求与产品智能匹配
- **数据同步** - 自动从国家药监局官网下载增量数据
- **多维筛选** - 按分类编码、企业名称、状态等筛选

### 技术架构
| 组件 | 技术 |
|------|------|
| 后端 | Python 3.10+ / Flask |
| 前端 | HTML/CSS/JavaScript / Tailwind CSS |
| 数据库 | SQLite |
| AI 接口 | OpenAI 兼容 API / 阿里云 DashScope |
| 数据源 | 国家药监局 UDID 官网 |

---

## 2. 交付物清单

### 2.1 必须交付的文件

```
高新医疗/                          # 项目根目录
├── 核心程序文件 (必须)
│   ├── udid_server.py            # Flask 后端服务主程序
│   ├── udid_hybrid_system.py     # 数据湖核心模块
│   ├── udid_sync.py              # 数据同步脚本
│   ├── ai_service.py             # AI 匹配服务
│   ├── embedding_service.py      # 向量检索服务
│   ├── embedding_faiss.py        # FAISS 向量索引
│   └── embedding_batch.py        # 批量向量生成
│
├── 前端页面 (必须)
│   ├── udid_viewer.html          # 主界面
│   ├── login.html                # 登录页面
│   └── admin.html                # 管理后台
│
├── 配置文件 (必须)
│   ├── requirements.txt          # Python 依赖清单
│   └── config.json               # API 配置 (需客户重新配置)
│
├── 数据文件 (必须)
│   └── udid_hybrid_lake.db       # SQLite 数据库 (~14GB)
│
├── 向量索引 (性能模式 B 必须)
│   └── data/
│       ├── faiss_index/          # FAISS 索引文件
│       └── embedding_batch/      # 向量批处理文件
│
└── 文档 (推荐)
    ├── README.md                 # 项目说明
    ├── SYSTEM_ARCHITECTURE.md    # 架构文档
    └── DEPLOYMENT_GUIDE.md       # 本部署指南
```

### 2.2 文件大小参考

| 文件/目录 | 大小 | 说明 |
|-----------|------|------|
| `udid_hybrid_lake.db` | ~14 GB | SQLite 数据库，包含所有产品数据和向量 |
| `data/embedding_batch/` | ~1.4 GB | 向量批处理中间文件 |
| 核心程序文件 | ~250 KB | Python 源代码 |
| 前端页面 | ~150 KB | HTML 文件 |
| **总计** | **~16 GB** | 完整交付 |

### 2.3 不需要交付的文件

```
× venv/                  # Python 虚拟环境 (客户需重新创建)
× __pycache__/           # Python 缓存
× *.log                  # 日志文件
× *.bak                  # 备份文件
× .git/                  # Git 版本控制
× .vscode/               # IDE 配置
× specs/                 # 开发规格文档 (可选)
```

### 2.4 性能模式说明（必须阅读）

系统的“智能匹配”分为两种运行模式：

| 模式 | 是否需要 FAISS 索引文件 | 特点 |
|------|--------------------------|------|
| 模式 A（兼容模式） | 否 | 可以不使用 FAISS，系统会走非 FAISS 的向量重排/关键词召回逻辑，性能可能较慢 |
| 模式 B（极致性能） | 是 | 使用 FAISS ANN 索引检索，延迟更低；交付时必须包含 `data/faiss_index/index.faiss` 和 `data/faiss_index/id_map.pkl` |

说明：
- 交付 `udid_hybrid_lake.db` 后，客户通常不需要重新“全库向量化”（数据库内已包含 embeddings 表）。
- 但若要启用模式 B（FAISS），则需要 **交付或重建** FAISS 索引文件。

---

## 3. 环境要求

### 3.1 硬件要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| CPU | 双核 2.0GHz | 四核 2.5GHz+ |
| 内存 | 4 GB | 8 GB+ |
| 硬盘 | 25 GB 可用空间 | 50 GB+ SSD |
| 网络 | 能访问外网 | 稳定宽带连接 |

### 3.2 软件要求

| 软件 | 版本要求 | 说明 |
|------|----------|------|
| 操作系统 | Windows 10/11, macOS 10.15+, Ubuntu 20.04+ | 任选其一 |
| Python | **3.10 或更高版本** | 必须 |
| pip | 最新版本 | Python 包管理器 |
| 浏览器 | Chrome/Edge/Firefox 最新版 | 访问前端界面 |

### 3.3 网络要求

系统需要访问以下外部服务（如需使用 AI 功能）：

| 服务 | 域名 | 用途 |
|------|------|------|
| 国家药监局 UDID | `udi.nmpa.gov.cn` | 数据同步 |
| AI 中转站 | `api.kksj.org` | AI 语义匹配 |
| 阿里云 DashScope | `dashscope.aliyuncs.com` | 向量生成 |

---

## 4. 安装步骤

### 4.1 Windows 系统安装

#### 步骤 1: 安装 Python

1. 访问 https://www.python.org/downloads/
2. 下载 Python 3.10+ 安装包
3. 运行安装程序，**务必勾选 "Add Python to PATH"**
4. 验证安装：
   ```cmd
   python --version
   # 应显示: Python 3.10.x 或更高
   ```

#### 步骤 2: 复制项目文件

1. 将交付的 `高新医疗` 文件夹复制到目标位置，例如：
   ```
   D:\高新医疗\
   ```

2. **重要**: 确保路径中不包含中文或特殊字符（建议使用英文路径）
   ```
   推荐: D:\UDID_System\
   避免: D:\项目\高新医疗\
   ```

#### 步骤 3: 创建虚拟环境

打开命令提示符 (cmd) 或 PowerShell：

```cmd
# 进入项目目录
cd D:\高新医疗

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
venv\Scripts\activate

# 验证激活成功 (命令行前会显示 (venv))
```

#### 步骤 4: 安装依赖

```cmd
# 确保在虚拟环境中
pip install --upgrade pip

# 安装项目依赖
pip install -r requirements.txt
```

#### 步骤 5: 验证安装

```cmd
# 测试导入核心模块
python -c "from udid_hybrid_system import LocalDataLake; print('OK')"
```

---

### 4.2 macOS 系统安装

#### 步骤 1: 安装 Python

macOS 推荐使用 Homebrew 安装：

```bash
# 安装 Homebrew (如果没有)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 Python
brew install python@3.11

# 验证
python3 --version
```

#### 步骤 2: 复制项目文件

```bash
# 复制到用户目录
cp -r /path/to/交付文件/高新医疗 ~/高新医疗
cd ~/高新医疗
```

#### 步骤 3: 创建虚拟环境并安装依赖

```bash
# 创建虚拟环境
python3 -m venv venv

# 激活
source venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 4.3 Linux (Ubuntu) 系统安装

```bash
# 安装 Python 和 pip
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip

# 复制项目
cp -r /path/to/交付文件/高新医疗 ~/高新医疗
cd ~/高新医疗

# 创建虚拟环境
python3.11 -m venv venv
source venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5. 配置说明

### 5.1 API 配置文件

系统需要配置 `config.json` 文件来启用 AI 功能。

#### 配置文件位置
```
高新医疗/config.json
```

#### 配置文件格式

```json
{
  "api_base_url": "https://api.kksj.org/v1",
  "api_key": "your-api-key-here",
  "model": "gemini-3-flash-preview",
  "embedding_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "embedding_api_key": "your-embedding-api-key-here",
  "embedding_model": "text-embedding-v4"
}
```

#### 配置项说明

| 配置项 | 说明 | 获取方式 |
|--------|------|----------|
| `api_base_url` | AI 对话接口地址 | 联系 AI 服务提供商 |
| `api_key` | AI 对话 API 密钥 | 联系 AI 服务提供商 |
| `model` | AI 对话模型名称 | 如 `gpt-4`, `gemini-3-flash` 等 |
| `embedding_api_url` | 向量生成接口地址 | 阿里云 DashScope 控制台 |
| `embedding_api_key` | 向量生成 API 密钥 | 阿里云 DashScope 控制台 |
| `embedding_model` | 向量模型名称 | 推荐 `text-embedding-v4` |

### 5.2 通过界面配置

系统也支持通过 Web 界面配置 API：

1. 启动服务后访问 http://localhost:8080
2. 点击页面右上角 **"设置"** 按钮
3. 填写 API 地址、API Key、模型名称
4. 点击保存

### 5.3 环境变量配置（可选）

为了更安全地管理密钥，可以使用环境变量：

**Windows:**
```cmd
set SECRET_KEY=your-random-secret-key
set ADMIN_API_KEY=your-admin-key
```

**macOS/Linux:**
```bash
export SECRET_KEY="your-random-secret-key"
export ADMIN_API_KEY="your-admin-key"
```

---

## 6. 启动与使用

### 6.1 启动服务

#### Windows

```cmd
cd D:\高新医疗
venv\Scripts\activate
python udid_server.py
```

#### macOS / Linux

```bash
cd ~/高新医疗
source venv/bin/activate
python udid_server.py
```

#### 启动成功提示

```
 * Serving Flask app 'udid_server'
 * Debug mode: off
 * Running on http://0.0.0.0:8080
Press CTRL+C to quit
```

### 6.2 访问系统

打开浏览器，访问：

| 页面 | 地址 | 说明 |
|------|------|------|
| 主界面 | http://localhost:8080 | 产品搜索与匹配 |
| 管理后台 | http://localhost:8080/admin.html | 系统管理 |

### 6.3 基本使用

#### 普通搜索
1. 在搜索框输入关键词（如产品名称、企业名称）
2. 可选：设置筛选条件
3. 点击 **"搜索"** 按钮

#### AI 智能匹配
1. 在"产品名称"输入框输入产品类型
2. 在"参数需求"输入框描述具体需求
3. 点击 **"智能匹配"** 按钮
4. 系统将返回按匹配度排序的结果

### 6.4 后台运行（可选）

#### Windows (使用 start 命令)
```cmd
start /B python udid_server.py > server.log 2>&1
```

#### macOS / Linux (使用 nohup)
```bash
nohup python udid_server.py > server.log 2>&1 &
```

#### 查看后台进程
```bash
# Linux/macOS
ps aux | grep udid_server

# Windows
tasklist | findstr python
```

---

## 7. 数据管理

### 7.1 数据同步

系统支持三种数据同步方式：

#### 方式一：命令行同步

```bash
# 激活虚拟环境后执行

# 列出可下载文件
python udid_sync.py --list

# 下载最新每日更新
python udid_sync.py --daily

# 智能增量同步（推荐）
python udid_sync.py --sync
```

#### 方式二：界面同步

1. 访问主界面
2. 点击页面右上角 **"同步数据"** 按钮
3. 等待同步完成

#### 方式三：手动导入 XML

1. 从 https://udi.nmpa.gov.cn/download.html 下载 XML 文件
2. 在主界面点击 **"导入 XML"** 按钮
3. 选择下载的 XML 文件上传

### 7.2 数据备份

#### 备份数据库

```bash
# 复制数据库文件即可完成备份
cp udid_hybrid_lake.db udid_hybrid_lake_backup_$(date +%Y%m%d).db
```

#### 建议备份策略
- **每周**：完整备份数据库
- **每月**：备份整个项目目录

### 7.3 数据库位置

| 数据库 | 路径 | 说明 |
|--------|------|------|
| 主数据库 | `udid_hybrid_lake.db` | 产品数据和向量 |
| 批处理数据库 | `batch_tasks.db` | 向量批处理任务 |

### 7.4 FAISS 索引（性能模式 B）

#### 7.4.1 交付时必须包含的文件

```
data/faiss_index/index.faiss
data/faiss_index/id_map.pkl
```

若这两个文件缺失，系统将无法使用 FAISS 检索（会提示索引未构建并可能降级到其他检索方式）。

#### 7.4.2 在客户电脑上重建 FAISS 索引（当索引文件未交付或损坏时）

前提：客户电脑已成功安装依赖并具备 `udid_hybrid_lake.db`。

1. 激活虚拟环境
2. 执行构建命令：

```bash
python embedding_faiss.py --build
```

说明：该命令会读取数据库 `embeddings` 表构建索引并写入 `data/faiss_index/`。

#### 7.4.3 验证 FAISS 索引是否可用

```bash
python embedding_faiss.py --stats
```

若输出类似“FAISS 索引向量数: xxx”，说明索引加载成功。

也可进行一次简单检索验证：

```bash
python embedding_faiss.py --search "一次性注射器"
```

#### 7.4.4 依赖说明（重要）

FAISS 需要额外安装 Python 包（不同操作系统可用包不同）。若客户在构建/加载 FAISS 时遇到 `No module named 'faiss'`，需安装对应依赖（例如 `faiss-cpu`），或改用模式 A。

---

## 8. 常见问题

### Q1: 启动时提示 "ModuleNotFoundError"

**原因**: 虚拟环境未激活或依赖未安装

**解决**:
```bash
# 1. 确保激活虚拟环境
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 2. 重新安装依赖
pip install -r requirements.txt
```

### Q2: 端口 8080 被占用

**原因**: 其他程序占用了 8080 端口

**解决**:
```bash
# 查找占用进程
# Windows:
netstat -ano | findstr :8080
# macOS/Linux:
lsof -i :8080

# 或修改 udid_server.py 中的端口号
# 找到 app.run() 行，修改 port 参数
```

### Q3: 数据库文件太大，无法复制

**原因**: SQLite 数据库约 14GB

**解决方案**:
1. 使用移动硬盘或 U 盘传输
2. 使用压缩工具（7-Zip）压缩后传输
3. 使用网盘分享（如百度网盘、阿里云盘）

### Q4: AI 匹配功能不可用

**原因**: API 配置错误或网络问题

**解决**:
1. 检查 `config.json` 中的 API 密钥是否正确
2. 确认网络能访问 API 服务地址
3. 在界面"设置"中重新配置 API

### Q5: 搜索结果为空

**原因**: 数据库未正确复制或损坏

**解决**:
```bash
# 检查数据库是否存在
ls -lh udid_hybrid_lake.db

# 检查数据库统计
python -c "
from udid_hybrid_system import LocalDataLake
lake = LocalDataLake()
print(lake.get_stats())
"
```

### Q6: Windows 下中文路径问题

**原因**: Python 对中文路径支持有限

**解决**: 将项目目录移至英文路径，如 `D:\UDID_System\`

### Q7: macOS 提示"无法验证开发者"

**解决**:
1. 打开"系统偏好设置" > "安全性与隐私"
2. 点击"仍要打开"允许运行

---

## 9. 技术支持

### 9.1 系统信息

| 信息 | 值 |
|------|-----|
| 系统版本 | 1.0.0 |
| 发布日期 | 2026-01-08 |
| Python 版本 | 3.10+ |

### 9.2 日志文件

系统运行日志位于项目根目录：
```
server.log        # 服务运行日志
```

查看日志：
```bash
# 实时查看
tail -f server.log

# Windows
type server.log
```

### 9.3 联系方式

如遇问题，请联系技术支持：

- **邮箱**: [填写您的邮箱]
- **电话**: [填写您的电话]
- **微信**: [填写您的微信]

---

## 附录 A: 快速启动脚本

### Windows 启动脚本 (start.bat)

在项目根目录创建 `start.bat`:

```batch
@echo off
echo 正在启动 UDID 医疗器械智能查询系统...
cd /d %~dp0
call venv\Scripts\activate
python udid_server.py
pause
```

双击 `start.bat` 即可启动系统。

### macOS/Linux 启动脚本 (start.sh)

在项目根目录创建 `start.sh`:

```bash
#!/bin/bash
echo "正在启动 UDID 医疗器械智能查询系统..."
cd "$(dirname "$0")"
source venv/bin/activate
python udid_server.py
```

使用方法:
```bash
chmod +x start.sh
./start.sh
```

---

## 附录 B: 完整打包命令

### 打包（排除不必要文件）

```bash
# macOS/Linux
cd /path/to/高新医疗
tar --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.log' \
    --exclude='*.bak' \
    --exclude='.git' \
    --exclude='.vscode' \
    --exclude='.DS_Store' \
    -czvf ../UDID_System_Delivery.tar.gz .

# Windows (使用 7-Zip)
# 排除 venv, __pycache__, *.log, *.bak, .git, .vscode
```

---

**文档版本**: 1.0  
**最后更新**: 2026-01-22
