# 高新医疗UDID查询系统 - 部署交付报告

## 项目概述

**项目名称**: 高新医疗UDID医疗器械智能查询系统
**项目类型**: 基于AI的医疗器械唯一标识查询与匹配平台
**技术栈**: Python + Flask + SQLite + FAISS + OpenAI/DashScope API
**交付日期**: 2026-02-02
**版本**: v1.0

---

## 一、系统架构

### 1.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户层 (Frontend)                               │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │udid_viewer   │  │   admin.html │  │  login.html  │  │sync_monitor  │    │
│  │  (查询界面)   │  │   (管理后台)  │  │   (登录页)    │  │  (监控面板)   │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
└─────────┼─────────────────┼─────────────────┼─────────────────┼────────────┘
          │                 │                 │                 │
          └─────────────────┴────────┬────────┴─────────────────┘
                                     │ HTTP/HTTPS
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            服务层 (Backend)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                        udid_server.py (Flask)                        │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ │  │
│  │  │  /api/search │ │ /api/ai-match│ │ /api/upload  │ │ /api/config  │ │  │
│  │  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘ │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                    │                                        │
│  ┌─────────────────────────────────┼──────────────────────────────────────┐│
│  │                                 ▼                                       ││
│  │  ┌─────────────────────────────────────────────────────────────────┐   ││
│  │  │                    udid_hybrid_system.py                         │   ││
│  │  │              (LocalDataLake - 本地数据湖核心)                     │   ││
│  │  └─────────────────────────────────────────────────────────────────┘   ││
│  └────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            AI服务层 (AI Services)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │  ai_service.py   │  │embedding_service │  │ embedding_batch  │          │
│  │   (智能匹配)      │  │   (向量检索)      │  │   (批量处理)      │          │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘          │
│           │                     │                     │                    │
│           └─────────────────────┴────────┬────────────┘                    │
│                                          │                                 │
│           ┌──────────────────────────────┼──────────────────────────────┐  │
│           │                              ▼                              │  │
│           │  ┌───────────────────────────────────────────────────────┐  │  │
│           │  │              embedding_faiss.py (FAISS索引)            │  │  │
│           │  │         (Facebook AI Similarity Search)                │  │  │
│           │  └───────────────────────────────────────────────────────┘  │  │
│           └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            数据层 (Data Layer)                               │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────┐  ┌──────────────────────────────────────┐ │
│  │  udid_hybrid_lake.db         │  │     data/faiss_index/                │ │
│  │  (SQLite主数据库 ~15GB)       │  │     ├─ index.faiss (346MB)          │ │
│  │  ├─ products (300万条)        │  │     └─ id_map.pkl (60MB)            │ │
│  │  ├─ embeddings (向量数据)     │  │                                      │ │
│  │  ├─ sync_log (同步日志)       │  └──────────────────────────────────────┘ │
│  │  └─ system_config (配置)      │                                           │
│  └──────────────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          外部数据源 (External Sources)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────┐  ┌──────────────────────────────────────┐ │
│  │   国家药监局UDI数据平台        │  │      阿里云DashScope API             │ │
│  │   (udi.nmpa.gov.cn)          │  │      (文本嵌入服务)                   │ │
│  │   ├─ RSS数据订阅              │  │                                      │ │
│  │   ├─ 每日增量包               │  │      OpenAI兼容API                   │ │
│  │   └─ XML数据文件              │  │      (AI智能匹配)                     │ │
│  └──────────────────────────────┘  └──────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心组件说明

| 组件 | 文件 | 功能 | 代码行数 | 评分 |
|------|------|------|----------|------|
| 主服务器 | udid_server.py | Flask Web服务、API路由、认证 | 2,643 | C |
| 数据湖 | udid_hybrid_system.py | 数据库操作、数据导入 | 644 | B |
| AI服务 | ai_service.py | 智能匹配、AI评分 | 537 | B |
| 向量服务 | embedding_service.py | 向量检索、混合搜索 | 1,557 | B+ |
| 批量处理 | embedding_batch.py | Batch API批量向量生成 | 1,870 | A- |
| 数据同步 | udid_sync.py | 药监局数据同步 | 837 | B |
| FAISS索引 | embedding_faiss.py | 向量索引管理 | 453 | A- |
| 自动同步 | auto_sync.py | 自动化同步服务 | 680 | B+ |

---

## 二、部署准备清单

### 2.1 服务器要求

#### 硬件配置

| 组件 | 最低配置 | 推荐配置 | 说明 |
|------|----------|----------|------|
| CPU | 4核 | 8核+ | 向量检索和AI匹配需要计算资源 |
| 内存 | 8GB | 32GB+ | FAISS索引加载需要大内存 |
| 磁盘 | 50GB SSD | 100GB+ NVMe | 数据库存储+索引+日志 |
| 网络 | 10Mbps | 100Mbps+ | 数据同步需要下载大文件 |

#### 软件环境

| 软件 | 版本要求 | 安装命令 |
|------|----------|----------|
| Ubuntu/Debian | 20.04+ | - |
| Python | 3.9+ | `apt install python3 python3-pip python3-venv` |
| Nginx | 1.18+ | `apt install nginx` |
| SQLite3 | 3.35+ | `apt install sqlite3` |
| Git | 2.30+ | `apt install git` |

### 2.2 文件清单

#### 必需文件

```
部署包/
├── udid_server.py              # 主服务器
├── udid_hybrid_system.py       # 数据湖核心
├── ai_service.py               # AI服务
├── embedding_service.py        # 向量服务
├── embedding_batch.py          # 批量处理
├── embedding_faiss.py          # FAISS索引
├── udid_sync.py                # 数据同步
├── auto_sync.py                # 自动同步
├── sync_server.py              # 同步监控服务器
├── requirements.txt            # Python依赖
├── config.json                 # 应用配置
├── .env                        # 环境变量（需手动创建）
├── deploy.sh                   # 部署脚本
├── monitor.sh                  # 监控脚本
├── backup.sh                   # 备份脚本
├── start.sh                    # 启动脚本
├── udid_viewer.html            # 查询界面
├── admin.html                  # 管理后台
├── login.html                  # 登录页面
├── sync_monitor.html           # 监控面板
└── data/                       # 数据目录
    ├── udid_hybrid_lake.db     # 主数据库（约15GB）
    ├── faiss_index/            # 向量索引
    │   ├── index.faiss
    │   └── id_map.pkl
    └── embedding_batch/        # 批量处理数据
```

#### 配置文件

**config.json 模板**:
```json
{
  "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "model": "qwen-max",
  "embedding_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "embedding_model": "text-embedding-v3",
  "embedding_dim": 1024,
  "embedding_batch_size": 10,
  "ai_cache_ttl_sec": 300,
  "ai_max_retries": 3,
  "ai_timeout_sec": 60,
  "ai_score_threshold": 70,
  "search_recall_multiplier": 20,
  "max_search_results": 100
}
```

**.env 模板**:
```bash
# AI服务配置
AI_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AI_MODEL=qwen-max
EMBEDDING_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v3

# API密钥（从数据库读取，此处仅作备用）
# AI_API_KEY=your_api_key_here
# EMBEDDING_API_KEY=your_embedding_api_key_here

# 同步服务配置
SYNC_API_KEY=your_random_32char_string_here

# Flask密钥（必须修改！）
SECRET_KEY=your_secret_key_here_change_in_production

# 可选：自定义端口
# PORT=8080
```

### 2.3 依赖清单

**requirements.txt**:
```
flask>=2.3.0,<3.0.0
flask-cors>=4.0.0,<5.0.0
gunicorn>=21.0.0
requests>=2.28.0,<3.0.0
pandas>=1.5.0,<2.0.0
numpy>=1.24.0,<2.0.0
jieba>=0.42.0
lxml>=4.9.0
defusedxml>=0.7.0
python-dotenv>=1.0.0
faiss-cpu>=1.7.4
```

> ⚠️ **重要**: 原requirements.txt缺少`faiss-cpu`和`gunicorn`，生产环境必须添加

---

## 三、部署步骤详解

### 3.1 服务器初始化

```bash
# 1. 更新系统
sudo apt update && sudo apt upgrade -y

# 2. 安装基础依赖
sudo apt install -y python3 python3-pip python3-venv git nginx sqlite3

# 3. 创建专用用户
sudo useradd -r -s /bin/false gaoxin || true

# 4. 创建目录结构
sudo mkdir -p /opt/gaoxin_medical
sudo mkdir -p /backup/gaoxin_medical
sudo mkdir -p /var/log/gaoxin_medical

# 5. 设置权限
sudo chown -R gaoxin:gaoxin /opt/gaoxin_medical
sudo chown -R gaoxin:gaoxin /backup/gaoxin_medical
sudo chown -R gaoxin:gaoxin /var/log/gaoxin_medical
```

### 3.2 应用部署

```bash
# 1. 切换到专用用户
sudo su - gaoxin

# 2. 进入项目目录
cd /opt/gaoxin_medical

# 3. 上传代码（通过scp或git）
# scp -r /local/path/* gaoxin@server:/opt/gaoxin_medical/

# 4. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 5. 安装依赖
pip install --upgrade pip
pip install -r requirements.txt

# 6. 创建配置文件
cp config.json.template config.json
nano config.json  # 根据实际环境修改

# 7. 创建环境变量文件
cp .env.template .env
nano .env  # 必须修改SECRET_KEY和SYNC_API_KEY

# 8. 生成安全密钥（如果.env中未设置）
export SECRET_KEY=$(openssl rand -hex 32)
export SYNC_API_KEY=$(openssl rand -hex 16)
echo "SECRET_KEY=$SECRET_KEY" >> .env
echo "SYNC_API_KEY=$SYNC_API_KEY" >> .env

# 9. 上传数据库文件（首次部署）
# 将 udid_hybrid_lake.db 上传到 data/ 目录
# 将 faiss_index/ 目录上传到 data/ 目录

# 10. 设置文件权限
chmod 600 .env config.json
chmod -R 755 data/
chmod +x *.sh
```

### 3.3 数据库初始化

```bash
# 1. 检查数据库完整性
sqlite3 data/udid_hybrid_lake.db "PRAGMA integrity_check;"

# 2. 优化数据库（首次部署）
sqlite3 data/udid_hybrid_lake.db "VACUUM;"

# 3. 启用WAL模式（提升并发性能）
sqlite3 data/udid_hybrid_lake.db "PRAGMA journal_mode=WAL;"
sqlite3 data/udid_hybrid_lake.db "PRAGMA synchronous=NORMAL;"

# 4. 创建必要的目录
mkdir -p data/embedding_batch
mkdir -p logs
```

### 3.4 Systemd服务配置

创建 `/etc/systemd/system/gaoxin-medical.service`:

```ini
[Unit]
Description=高新医疗UDID查询系统
After=network.target

[Service]
Type=simple
User=gaoxin
Group=gaoxin
WorkingDirectory=/opt/gaoxin_medical
Environment=PATH=/opt/gaoxin_medical/venv/bin
Environment=PYTHONPATH=/opt/gaoxin_medical
Environment=SECRET_KEY=your_generated_secret_key
Environment=SYNC_API_KEY=your_generated_sync_key

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/gaoxin_medical/data /opt/gaoxin_medical/logs /var/log/gaoxin_medical

ExecStart=/opt/gaoxin_medical/venv/bin/gunicorn \
    -w 4 \
    -k sync \
    -b 127.0.0.1:8080 \
    --timeout 120 \
    --access-logfile /var/log/gaoxin_medical/access.log \
    --error-logfile /var/log/gaoxin_medical/error.log \
    --capture-output \
    --enable-stdio-inheritance \
    udid_server:app

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动服务:
```bash
sudo systemctl daemon-reload
sudo systemctl enable gaoxin-medical
sudo systemctl start gaoxin-medical
sudo systemctl status gaoxin-medical
```

### 3.5 Nginx反向代理配置

创建 `/etc/nginx/sites-available/gaoxin-medical`:

```nginx
server {
    listen 80;
    server_name your-domain.com;  # 替换为实际域名

    # 安全响应头
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

    # 日志配置
    access_log /var/log/gaoxin_medical/nginx_access.log;
    error_log /var/log/gaoxin_medical/nginx_error.log;

    # 静态文件缓存
    location ~* \.(html|htm)$ {
        root /opt/gaoxin_medical;
        expires -1;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg)$ {
        root /opt/gaoxin_medical;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # API代理
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # 主页面
    location / {
        root /opt/gaoxin_medical;
        try_files $uri $uri/ /udid_viewer.html;
    }
}
```

启用配置:
```bash
sudo ln -s /etc/nginx/sites-available/gaoxin-medical /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 3.6 HTTPS配置（Let's Encrypt）

```bash
# 1. 安装Certbot
sudo apt install certbot python3-certbot-nginx

# 2. 获取证书
sudo certbot --nginx -d your-domain.com

# 3. 自动续期测试
sudo certbot renew --dry-run
```

### 3.7 防火墙配置

```bash
# 1. 配置UFW
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow http
sudo ufw allow https

# 2. 限制8080端口仅允许本地访问（通过Nginx代理）
sudo ufw deny 8080

# 3. 启用防火墙
sudo ufw enable
```

---

## 四、监控与运维

### 4.1 健康检查

**monitor.sh** 已配置:
```bash
#!/bin/bash
# 保存为 /opt/gaoxin_medical/monitor.sh

LOG_FILE="/var/log/gaoxin_medical/monitor.log"
ALERT_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"

# 检查服务状态
if ! systemctl is-active --quiet gaoxin-medical; then
    echo "$(date): 服务异常，尝试重启..." >> $LOG_FILE
    sudo systemctl restart gaoxin-medical
    # 发送告警
    curl -s -X POST "$ALERT_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d '{"msgtype": "text", "text": {"content": "高新医疗系统服务异常，已尝试自动重启"}}'
fi

# 检查磁盘空间
DISK_USAGE=$(df /opt/gaoxin_medical | tail -1 | awk '{print $5}' | sed 's/%//')
if [ $DISK_USAGE -gt 85 ]; then
    echo "$(date): 磁盘使用率 ${DISK_USAGE}%" >> $LOG_FILE
fi

# 检查API可用性
if ! curl -sf http://127.0.0.1:8080/api/stats > /dev/null; then
    echo "$(date): API无响应" >> $LOG_FILE
fi
```

添加到crontab:
```bash
*/5 * * * * /opt/gaoxin_medical/monitor.sh
```

### 4.2 自动备份

**backup.sh** 已配置:
```bash
#!/bin/bash
# 保存为 /opt/gaoxin_medical/backup.sh

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/gaoxin_medical"
PROJECT_DIR="/opt/gaoxin_medical"
RETENTION_DAYS=7

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 1. 数据库在线备份（使用SQLite备份API）
sqlite3 "$PROJECT_DIR/udid_hybrid_lake.db" ".backup '$BACKUP_DIR/db_$DATE.db'"
gzip "$BACKUP_DIR/db_$DATE.db"
chmod 600 "$BACKUP_DIR/db_$DATE.db.gz"

# 2. 备份配置文件
cp "$PROJECT_DIR/config.json" "$BACKUP_DIR/config_$DATE.json"
cp "$PROJECT_DIR/.env" "$BACKUP_DIR/env_$DATE"

# 3. 备份FAISS索引
cp "$PROJECT_DIR/data/faiss_index/index.faiss" "$BACKUP_DIR/index_$DATE.faiss"
cp "$PROJECT_DIR/data/faiss_index/id_map.pkl" "$BACKUP_DIR/id_map_$DATE.pkl"

# 4. 创建完整备份包
tar czf "$BACKUP_DIR/full_$DATE.tar.gz" -C "$BACKUP_DIR" \
    "db_$DATE.db.gz" "config_$DATE.json" "env_$DATE" \
    "index_$DATE.faiss" "id_map_$DATE.pkl"

# 5. 清理旧备份
find "$BACKUP_DIR" -name "*.gz" -mtime +$RETENTION_DAYS -delete
find "$BACKUP_DIR" -name "full_*.tar.gz" -mtime +$RETENTION_DAYS -delete

echo "$(date): 备份完成 full_$DATE.tar.gz"
```

添加到crontab:
```bash
0 2 * * * /opt/gaoxin_medical/backup.sh >> /var/log/gaoxin_medical/backup.log 2>&1
```

### 4.3 日志轮转

创建 `/etc/logrotate.d/gaoxin-medical`:
```
/var/log/gaoxin_medical/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 gaoxin gaoxin
    sharedscripts
    postrotate
        systemctl reload gaoxin-medical
    endscript
}
```

### 4.4 定时同步任务

```bash
# 每天凌晨2点执行数据同步
0 2 * * * cd /opt/gaoxin_medical && python auto_sync.py >> /var/log/gaoxin_medical/sync.log 2>&1
```

---

## 五、安全检查清单

### 5.1 部署前检查

| 检查项 | 状态 | 说明 |
|--------|------|------|
| [ ] SECRET_KEY已修改 | ⬜ | 不能为默认值或硬编码值 |
| [ ] SYNC_API_KEY已修改 | ⬜ | 随机生成32字符字符串 |
| [ ] API密钥已配置 | ⬜ | 从数据库或环境变量读取 |
| [ ] 数据库权限正确 | ⬜ | 600权限，gaoxin用户所有 |
| [ ] 防火墙已配置 | ⬜ | 仅开放80/443端口 |
| [ ] HTTPS已启用 | ⬜ | 使用Let's Encrypt证书 |
| [ ] 日志目录可写 | ⬜ | /var/log/gaoxin_medical |
| [ ] 备份目录可写 | ⬜ | /backup/gaoxin_medical |

### 5.2 安全加固建议

#### 高优先级（必须修复）

1. **修复SQL注入风险** (udid_server.py:997-1010)
   - FTS查询构建使用参数化查询
   - 对用户输入进行严格验证

2. **修复XSS漏洞** (前端HTML文件)
   - 所有innerHTML替换为textContent
   - 实现escapeHtml函数并统一使用

3. **修复XXE漏洞** (udid_sync.py:188)
   ```python
   from xml.etree.ElementTree import XMLParser
   parser = XMLParser()
   parser.entity_declaration_handler = lambda *args: None
   root = ET.fromstring(content, parser=parser)
   ```

4. **修复Pickle安全风险** (embedding_faiss.py:84)
   - 将pickle替换为JSON存储
   - 添加文件完整性校验

5. **会话安全配置** (udid_server.py:55)
   ```python
   app.config['SESSION_COOKIE_SECURE'] = True
   app.config['SESSION_COOKIE_HTTPONLY'] = True
   app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
   ```

#### 中优先级（建议修复）

1. 添加请求频率限制
2. 实现CSRF Token验证
3. 添加验证码防止暴力破解
4. 实现API密钥轮换机制
5. 添加操作审计日志

#### 低优先级（可选优化）

1. 代码模块化重构
2. 添加单元测试覆盖
3. 实现数据库连接池
4. 添加Redis缓存层

---

## 六、代码审查汇总

### 6.1 各模块评分

| 模块 | 评分 | 主要风险 | 优先级 |
|------|------|----------|--------|
| udid_server.py | C | SQL注入、会话安全、代码组织 | 高 |
| udid_hybrid_system.py | B | XXE、路径遍历、线程安全 | 高 |
| ai_service.py | B | 内存泄漏、并发控制 | 中 |
| embedding_service.py | B+ | 连接管理、SQL注入 | 中 |
| embedding_batch.py | A- | 并发访问、状态管理 | 低 |
| udid_sync.py | B | XXE、文件完整性 | 高 |
| embedding_faiss.py | A- | Pickle安全、版本兼容 | 高 |
| auto_sync.py | B+ | 锁机制、日志管理 | 中 |
| 前端HTML | B | XSS、CSRF、认证检查 | 高 |
| deploy.sh | B+ | 密钥持久化、权限分离 | 中 |

### 6.2 关键问题统计

| 类别 | 数量 | 高优先级 | 中优先级 | 低优先级 |
|------|------|----------|----------|----------|
| 安全漏洞 | 12 | 8 | 3 | 1 |
| 性能问题 | 8 | 2 | 4 | 2 |
| 代码质量 | 15 | 1 | 6 | 8 |
| 配置问题 | 6 | 3 | 2 | 1 |
| **总计** | **41** | **14** | **15** | **12** |

---

## 七、故障排查指南

### 7.1 常见问题

#### 服务无法启动

```bash
# 检查日志
sudo journalctl -u gaoxin-medical -f

# 检查端口占用
sudo lsof -i :8080

# 检查权限
ls -la /opt/gaoxin_medical/data/

# 测试手动启动
cd /opt/gaoxin_medical
source venv/bin/activate
python udid_server.py
```

#### 数据库锁定

```bash
# 检查WAL文件
ls -la data/udid_hybrid_lake.db*

# 修复数据库
sqlite3 data/udid_hybrid_lake.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 检查锁定进程
fuser data/udid_hybrid_lake.db
```

#### FAISS索引加载失败

```bash
# 检查索引文件
ls -la data/faiss_index/

# 检查Python环境
python -c "import faiss; print(faiss.__version__)"

# 重新生成索引
python embedding_faiss.py --rebuild
```

### 7.2 性能调优

```bash
# 1. 数据库优化
sqlite3 data/udid_hybrid_lake.db "PRAGMA optimize;"

# 2. 分析查询性能
sqlite3 data/udid_hybrid_lake.db "ANALYZE;"

# 3. 调整gunicorn工作进程
# 编辑 /etc/systemd/system/gaoxin-medical.service
# -w 4 改为 -w $(($(nproc) * 2 + 1))

# 4. 增加系统文件描述符限制
echo "fs.file-max = 65535" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

---

## 八、交付清单

### 8.1 交付物

| 序号 | 交付物 | 说明 | 状态 |
|------|--------|------|------|
| 1 | 源代码 | 完整项目代码 | ✅ |
| 2 | 数据库文件 | udid_hybrid_lake.db (~15GB) | ✅ |
| 3 | 向量索引 | FAISS索引文件 | ✅ |
| 4 | 部署文档 | 本交付报告 | ✅ |
| 5 | 配置文件模板 | config.json.template, .env.template | ✅ |
| 6 | 部署脚本 | deploy.sh, monitor.sh, backup.sh | ✅ |
| 7 | 运维手册 | 故障排查、监控指南 | ✅ |

### 8.2 客户需准备

| 序号 | 项目 | 说明 |
|------|------|------|
| 1 | 云服务器 | 推荐配置：8核32GB，100GB SSD |
| 2 | 域名 | 用于HTTPS访问 |
| 3 | SSL证书 | 或使用Let's Encrypt免费证书 |
| 4 | API密钥 | 阿里云DashScope或OpenAI API Key |
| 5 | 告警Webhook | 钉钉/企业微信机器人地址（可选） |

### 8.3 培训内容

1. **系统管理培训**
   - 用户和权限管理
   - 系统配置修改
   - 数据同步操作

2. **运维培训**
   - 日志查看和分析
   - 备份和恢复操作
   - 常见问题处理

3. **安全培训**
   - 密钥管理
   - 访问控制
   - 安全更新

---

## 九、附录

### 9.1 API端点清单

| 端点 | 方法 | 说明 | 认证 |
|------|------|------|------|
| /api/stats | GET | 数据库统计 | 否 |
| /api/search | GET | 关键词搜索 | 否 |
| /api/ai-match | POST | AI智能匹配 | 否 |
| /api/upload | POST | XML文件上传 | 是 |
| /api/sync | POST | 触发同步 | 是 |
| /api/config | GET/POST | 配置管理 | 是 |
| /api/users | GET/POST/DELETE | 用户管理 | 是 |
| /api/login | POST | 用户登录 | 否 |
| /api/logout | POST | 用户登出 | 是 |

### 9.2 环境变量参考

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| SECRET_KEY | 是 | - | Flask会话密钥 |
| SYNC_API_KEY | 是 | - | 同步服务API密钥 |
| AI_API_KEY | 否 | - | AI服务API密钥 |
| EMBEDDING_API_KEY | 否 | - | 嵌入服务API密钥 |
| PORT | 否 | 8080 | 服务端口 |
| FLASK_ENV | 否 | production | 运行环境 |

### 9.3 联系支持

如有问题，请联系开发团队获取支持。

---

**报告生成时间**: 2026-02-02
**报告版本**: v1.0
**机密级别**: 客户机密
