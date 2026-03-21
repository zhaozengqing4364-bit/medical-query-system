# 高新医疗系统 PostgreSQL 迁移规划

> 更新日期：2026-03-02  
> 当前状态：代码运行时已默认切换到 `DB_BACKEND=postgres`，SQLite 仅保留回滚通道。

## 一、迁移概览

| 项目 | 内容 |
|------|------|
| **迁移类型** | SQLite → PostgreSQL（保留 FAISS 向量索引） |
| **数据量** | SQLite 15GB + FAISS 346MB |
| **预计停机时间** | 30-60 分钟 |
| **风险等级** | 中（需验证数据一致性） |

---

## 二、迁移前准备

### 2.1 系统检查清单

- [ ] 确认当前系统运行正常
- [ ] 备份现有数据（SQLite + FAISS）
- [ ] 确认服务器磁盘空间 > 30GB
- [ ] 确认 PostgreSQL 安装权限

### 2.2 安装 PostgreSQL + pg_zhparser

#### macOS（开发环境）
```bash
# 安装 PostgreSQL 15
brew install postgresql@15

# 安装 pgvector 和中文分词
brew install pgvector

# 启动服务
brew services start postgresql@15

# 创建数据库
psql postgres -c "CREATE DATABASE udid_db;"
psql postgres -c "CREATE USER udid_user WITH PASSWORD 'your_secure_password';"
psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE udid_db TO udid_user;"

# 安装中文分词插件 pg_zhparser
# 需从源码编译安装（见下方详细步骤）
```

#### Ubuntu/Debian（生产环境）
```bash
# 添加 PostgreSQL 官方源
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
sudo apt-get update

# 安装 PostgreSQL 15
sudo apt-get install -y postgresql-15 postgresql-contrib-15

# 安装 pgvector
sudo apt-get install -y postgresql-15-pgvector

# 安装中文分词依赖
sudo apt-get install -y postgresql-server-dev-15 gcc make git

# 编译安装 scws + pg_zhparser
cd /tmp
git clone https://github.com/amutu/zhparser.git
cd zhparser
make
sudo make install

# 创建数据库和用户
sudo -u postgres psql -c "CREATE DATABASE udid_db;"
sudo -u postgres psql -c "CREATE USER udid_user WITH PASSWORD 'your_secure_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE udid_db TO udid_user;"
```

### 2.3 安装迁移工具

```bash
# 安装 pgloader
# macOS
brew install pgloader

# Ubuntu
sudo apt-get install pgloader

# 或使用 Docker
docker pull dimitri/pgloader:latest
```

### 2.4 Python 依赖更新

```bash
# 添加到 requirements.txt
# 新增：
psycopg2-binary>=2.9.0

# 安装
pip install psycopg2-binary
```

---

## 三、数据库 Schema 设计

### 3.1 创建表结构

```sql
-- 连接数据库
psql -U udid_user -d udid_db

-- 启用扩展
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- 模糊搜索
CREATE EXTENSION IF NOT EXISTS zhparser;      -- 中文分词

-- 创建中文全文检索配置
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;

-- ============================================
-- 1. products 表（主数据表）
-- ============================================
CREATE TABLE products (
    di_code VARCHAR(100) PRIMARY KEY,
    product_name TEXT,
    commercial_name TEXT,
    model TEXT,
    manufacturer TEXT,
    description TEXT,
    publish_date DATE,
    source VARCHAR(50) DEFAULT 'RSS',
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    category_code VARCHAR(100),
    social_code VARCHAR(100),
    cert_no VARCHAR(200),
    status VARCHAR(50),
    product_type VARCHAR(100),
    phone VARCHAR(100),
    email VARCHAR(200),
    scope TEXT,
    safety_info TEXT,

    -- 全文检索向量（中文）
    search_vector tsvector
);

-- 创建全文检索索引
CREATE INDEX idx_products_search ON products USING GIN (search_vector);

-- 创建常用查询索引
CREATE INDEX idx_products_name ON products USING GIN (product_name gin_trgm_ops);
CREATE INDEX idx_products_manufacturer ON products USING GIN (manufacturer gin_trgm_ops);
CREATE INDEX idx_products_category ON products(category_code);
CREATE INDEX idx_products_publish_date ON products(publish_date);
CREATE INDEX idx_products_last_updated ON products(last_updated);

-- 创建全文检索更新触发器
CREATE OR REPLACE FUNCTION products_search_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('chinese', COALESCE(NEW.product_name, '')), 'A') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.manufacturer, '')), 'B') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.model, '')), 'C') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.description, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION products_search_update();

-- ============================================
-- 2. embeddings 表（向量元数据表）
-- ============================================
CREATE TABLE embeddings (
    di_code VARCHAR(100) PRIMARY KEY REFERENCES products(di_code) ON DELETE CASCADE,
    text_hash VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_embeddings_hash ON embeddings(text_hash);

-- ============================================
-- 3. users 表（用户认证）
-- ============================================
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) DEFAULT 'admin',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_users_username ON users(username);

-- ============================================
-- 4. system_config 表（系统配置）
-- ============================================
CREATE TABLE system_config (
    key VARCHAR(255) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- 5. sync_log 表（同步日志）
-- ============================================
CREATE TABLE sync_log (
    id SERIAL PRIMARY KEY,
    sync_date DATE,
    file_name VARCHAR(500),
    records_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_sync_log_date ON sync_log(sync_date);

-- ============================================
-- 6. sync_run 表（同步运行记录）
-- ============================================
CREATE TABLE sync_run (
    id SERIAL PRIMARY KEY,
    sync_date DATE,
    file_name VARCHAR(500),
    status VARCHAR(50),
    error_message TEXT,
    records_count INTEGER DEFAULT 0,
    invalid_records INTEGER DEFAULT 0,
    audit_records INTEGER DEFAULT 0,
    file_checksum VARCHAR(64),
    file_size BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_sync_run_date ON sync_run(sync_date);
CREATE INDEX idx_sync_run_status ON sync_run(status);

-- ============================================
-- 7. search_logs 表（搜索日志）
-- ============================================
CREATE TABLE search_logs (
    id SERIAL PRIMARY KEY,
    query TEXT,
    query_type VARCHAR(50),
    results_count INTEGER,
    response_time_ms INTEGER,
    client_ip INET,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_search_logs_created ON search_logs(created_at);

-- 插入默认管理员用户（密码：admin123，请在生产环境修改）
INSERT INTO users (username, password_hash, role) VALUES
('admin', 'scrypt:32768:8:1$...$...', 'admin');
```

---

## 四、数据迁移步骤

### 4.1 备份现有数据

```bash
#!/bin/bash
# backup.sh

BACKUP_DIR="/backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p $BACKUP_DIR

# 备份 SQLite
cp /path/to/udid_hybrid_lake.db $BACKUP_DIR/

# 备份 FAISS 索引
cp -r /path/to/data/faiss_index $BACKUP_DIR/

# 备份配置文件
cp config.json $BACKUP_DIR/

# 压缩
 tar -czvf $BACKUP_DIR.tar.gz $BACKUP_DIR

echo "备份完成: $BACKUP_DIR.tar.gz"
```

### 4.2 使用 pgloader 迁移数据

#### 方式一：直接使用 pgloader

```bash
# 创建 pgloader 配置文件
# migration.load
```

创建 `migration.load` 文件：

```lisp
LOAD DATABASE
    FROM sqlite:///Users/zhaozengqing/github/AI/test/高新医疗/udid_hybrid_lake.db
    INTO postgresql://udid_user:your_secure_password@localhost:5432/udid_db

WITH include drop, create tables, create indexes, reset sequences,
     workers = 8, concurrency = 2

SET work_mem to '200MB', maintenance_work_mem to '512MB'

CAST
    -- 日期时间转换
    column products.publish_date to date drop not null using zero-dates-to-null,
    column products.last_updated to timestamptz using sqlite-timestamp-to-timestamp,

    -- FTS 虚拟表不迁移（PostgreSQL 使用 tsvector）
    type products_fts to drop,

    -- embeddings 表只迁移元数据，不迁移 BLOB
    column embeddings.embedding to drop

BEFORE LOAD DO
    $$ TRUNCATE TABLE products, embeddings, users, system_config, sync_log, sync_run CASCADE; $$

AFTER LOAD DO
    $$ ANALYZE products; $$,
    $$ ANALYZE embeddings; $$
;
```

执行迁移：
```bash
pgloader migration.load
```

#### 方式二：使用 Docker

```bash
docker run --rm \
  -v $(pwd):/data \
  dimitri/pgloader:latest \
  pgloader /data/migration.load
```

### 4.3 验证数据完整性

```bash
#!/bin/bash
# verify_migration.sh

echo "=== 数据迁移验证 ==="

# 连接 PostgreSQL 检查记录数
psql -U udid_user -d udid_db -c "SELECT COUNT(*) as products_count FROM products;"

# 对比 SQLite 记录数
sqlite3 udid_hybrid_lake.db "SELECT COUNT(*) FROM products;"

# 检查 embeddings 记录数
psql -U udid_user -d udid_db -c "SELECT COUNT(*) as embeddings_count FROM embeddings;"
sqlite3 udid_hybrid_lake.db "SELECT COUNT(*) FROM embeddings;"

# 抽样检查
psql -U udid_user -d udid_db -c "SELECT di_code, product_name, manufacturer FROM products LIMIT 5;"

echo "=== 验证完成 ==="
```

---

## 五、代码改造

### 5.1 创建数据库连接模块

创建 `db_postgres.py`：

```python
"""
PostgreSQL 数据库连接模块
替代原有的 SQLite 连接
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool

# 配置日志
logger = logging.getLogger(__name__)

# 数据库连接配置
DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', '5432')),
    'database': os.getenv('POSTGRES_DB', 'udid_db'),
    'user': os.getenv('POSTGRES_USER', 'udid_user'),
    'password': os.getenv('POSTGRES_PASSWORD', 'your_secure_password'),
}

# 连接池（全局）
connection_pool: Optional[ThreadedConnectionPool] = None


def init_connection_pool(min_conn: int = 2, max_conn: int = 10):
    """初始化数据库连接池"""
    global connection_pool
    try:
        connection_pool = ThreadedConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            **DB_CONFIG
        )
        logger.info(f"PostgreSQL 连接池初始化成功 ({min_conn}-{max_conn})")
    except Exception as e:
        logger.error(f"连接池初始化失败: {e}")
        raise


@contextmanager
def get_connection():
    """获取数据库连接（上下文管理器）"""
    global connection_pool
    if connection_pool is None:
        init_connection_pool()

    conn = None
    try:
        conn = connection_pool.getconn()
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"数据库操作失败: {e}")
        raise
    finally:
        if conn:
            connection_pool.putconn(conn)


@contextmanager
def get_cursor(cursor_factory=None):
    """获取数据库游标"""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
        finally:
            cursor.close()


# ============================================
# 查询函数（兼容原有接口）
# ============================================

def get_product_by_di_code(di_code: str) -> Optional[Dict]:
    """通过 DI 编码获取产品信息"""
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM products WHERE di_code = %s
        """, (di_code,))
        return cur.fetchone()


def search_products(
    keyword: str,
    limit: int = 20,
    offset: int = 0
) -> Tuple[List[Dict], int]:
    """
    全文检索产品
    使用 PostgreSQL 中文全文检索
    """
    with get_cursor(RealDictCursor) as cur:
        # 计算总数量
        cur.execute("""
            SELECT COUNT(*) FROM products
            WHERE search_vector @@ plainto_tsquery('chinese', %s)
               OR product_name ILIKE %s
               OR manufacturer ILIKE %s
        """, (keyword, f'%{keyword}%', f'%{keyword}%'))
        total = cur.fetchone()['count']

        # 执行搜索（按相关性排序）
        cur.execute("""
            SELECT
                *,
                ts_rank(search_vector, plainto_tsquery('chinese', %s)) as rank
            FROM products
            WHERE search_vector @@ plainto_tsquery('chinese', %s)
               OR product_name ILIKE %s
               OR manufacturer ILIKE %s
            ORDER BY
                ts_rank(search_vector, plainto_tsquery('chinese', %s)) DESC,
                last_updated DESC
            LIMIT %s OFFSET %s
        """, (keyword, keyword, f'%{keyword}%', f'%{keyword}%', keyword, limit, offset))

        results = cur.fetchall()
        return results, total


def search_products_fuzzy(
    keyword: str,
    limit: int = 20
) -> List[Dict]:
    """
    模糊搜索（使用 pg_trgm）
    适用于拼写错误容忍搜索
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT
                *,
                similarity(product_name, %s) as sml
            FROM products
            WHERE
                product_name %% %s
                OR manufacturer %% %s
            ORDER BY
                GREATEST(
                    similarity(product_name, %s),
                    similarity(manufacturer, %s)
                ) DESC
            LIMIT %s
        """, (keyword, keyword, keyword, keyword, keyword, limit))
        return cur.fetchall()


def get_products_by_manufacturer(
    manufacturer: str,
    limit: int = 50
) -> List[Dict]:
    """按生产企业搜索"""
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM products
            WHERE manufacturer ILIKE %s
            ORDER BY last_updated DESC
            LIMIT %s
        """, (f'%{manufacturer}%', limit))
        return cur.fetchall()


def get_embedding_meta(di_code: str) -> Optional[Dict]:
    """获取向量元数据（不包含向量数据）"""
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT di_code, text_hash, created_at
            FROM embeddings
            WHERE di_code = %s
        """, (di_code,))
        return cur.fetchone()


def save_search_log(
    query: str,
    query_type: str,
    results_count: int,
    response_time_ms: int,
    client_ip: str = None
):
    """记录搜索日志"""
    try:
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO search_logs
                (query, query_type, results_count, response_time_ms, client_ip)
                VALUES (%s, %s, %s, %s, %s)
            """, (query, query_type, results_count, response_time_ms, client_ip))
    except Exception as e:
        logger.error(f"记录搜索日志失败: {e}")


def get_db_stats() -> Dict[str, Any]:
    """获取数据库统计信息"""
    with get_cursor(RealDictCursor) as cur:
        stats = {}

        # 产品总数
        cur.execute("SELECT COUNT(*) as count FROM products")
        stats['total_products'] = cur.fetchone()['count']

        # 向量记录数
        cur.execute("SELECT COUNT(*) as count FROM embeddings")
        stats['total_embeddings'] = cur.fetchone()['count']

        # 今日更新数
        cur.execute("""
            SELECT COUNT(*) as count FROM products
            WHERE DATE(last_updated) = CURRENT_DATE
        """)
        stats['today_updated'] = cur.fetchone()['count']

        # 数据源统计
        cur.execute("""
            SELECT source, COUNT(*) as count
            FROM products
            GROUP BY source
        """)
        stats['source_distribution'] = {r['source']: r['count'] for r in cur.fetchall()}

        # 数据库大小
        cur.execute("""
            SELECT pg_size_pretty(pg_database_size(%s)) as size
        """, (DB_CONFIG['database'],))
        stats['db_size'] = cur.fetchone()['size']

        return stats


# ============================================
# 批量操作
# ============================================

def batch_insert_products(products: List[Dict], batch_size: int = 1000):
    """批量插入产品数据"""
    if not products:
        return

    with get_cursor() as cur:
        for i in range(0, len(products), batch_size):
            batch = products[i:i+batch_size]
            execute_values(
                cur,
                """
                INSERT INTO products
                (di_code, product_name, commercial_name, model, manufacturer,
                 description, publish_date, source, category_code, social_code,
                 cert_no, status, product_type, phone, email, scope, safety_info)
                VALUES %s
                ON CONFLICT (di_code) DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    last_updated = NOW()
                """,
                [(
                    p.get('di_code'),
                    p.get('product_name'),
                    p.get('commercial_name'),
                    p.get('model'),
                    p.get('manufacturer'),
                    p.get('description'),
                    p.get('publish_date'),
                    p.get('source', 'RSS'),
                    p.get('category_code'),
                    p.get('social_code'),
                    p.get('cert_no'),
                    p.get('status'),
                    p.get('product_type'),
                    p.get('phone'),
                    p.get('email'),
                    p.get('scope'),
                    p.get('safety_info')
                ) for p in batch]
            )
            logger.info(f"已插入批次 {i//batch_size + 1}/{(len(products)-1)//batch_size + 1}")


def batch_insert_embeddings(embeddings_meta: List[Dict], batch_size: int = 5000):
    """批量插入向量元数据"""
    if not embeddings_meta:
        return

    with get_cursor() as cur:
        for i in range(0, len(embeddings_meta), batch_size):
            batch = embeddings_meta[i:i+batch_size]
            execute_values(
                cur,
                """
                INSERT INTO embeddings (di_code, text_hash, created_at)
                VALUES %s
                ON CONFLICT (di_code) DO UPDATE SET
                    text_hash = EXCLUDED.text_hash,
                    created_at = EXCLUDED.created_at
                """,
                [(
                    e['di_code'],
                    e['text_hash'],
                    e.get('created_at', datetime.now())
                ) for e in batch]
            )
```

### 5.2 修改 udid_server.py 入口

```python
# 在 app 初始化时添加
from db_postgres import init_connection_pool

# 在 create_app() 或应用启动时调用
init_connection_pool(min_conn=2, max_conn=10)
```

### 5.3 环境变量配置

创建 `.env` 文件：

```bash
# PostgreSQL 配置
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=udid_db
POSTGRES_USER=udid_user
POSTGRES_PASSWORD=your_secure_password

# FAISS 配置（保持不变）
FAISS_INDEX_PATH=/path/to/data/faiss_index/index.faiss
FAISS_ID_MAP_PATH=/path/to/data/faiss_index/id_map.pkl
```

---

## 六、FAISS 集成保持不变

### 6.1 确认 FAISS 文件位置

```bash
# FAISS 索引文件位置（相对路径保持不变）
data/faiss_index/
├── index.faiss      # 346MB，向量索引
└── id_map.pkl       # 57MB，ID映射表
```

### 6.2 修改 embedding_service.py 的数据库连接

只需修改数据库查询部分，FAISS 加载和检索逻辑完全不变：

```python
# 原有导入
# from udid_hybrid_system import get_connection  # SQLite

# 新导入
from db_postgres import get_connection  # PostgreSQL

# FAISS 相关代码完全不需要修改
# - 加载索引：保持不变
# - 向量检索：保持不变
# - 相似度计算：保持不变
```

---

## 七、迁移验证清单

### 7.1 数据完整性检查

```bash
#!/bin/bash
# check_migration.sh

echo "====== 迁移验证 ======"

# 1. 记录数对比
echo "1. 产品记录数对比："
echo "  SQLite: $(sqlite3 udid_hybrid_lake.db 'SELECT COUNT(*) FROM products')"
echo "  PostgreSQL: $(psql -U udid_user -d udid_db -t -c 'SELECT COUNT(*) FROM products')"

echo "2. 向量记录数对比："
echo "  SQLite: $(sqlite3 udid_hybrid_lake.db 'SELECT COUNT(*) FROM embeddings')"
echo "  PostgreSQL: $(psql -U udid_user -d udid_db -t -c 'SELECT COUNT(*) FROM embeddings')"

echo "3. 随机抽样检查："
SAMPLE_DI=$(sqlite3 udid_hybrid_lake.db 'SELECT di_code FROM products ORDER BY RANDOM() LIMIT 1')
echo "  抽样 DI: $SAMPLE_DI"

# 对比字段值
sqlite3 udid_hybrid_lake.db "SELECT product_name, manufacturer FROM products WHERE di_code = '$SAMPLE_DI'"
psql -U udid_user -d udid_db -c "SELECT product_name, manufacturer FROM products WHERE di_code = '$SAMPLE_DI'"

echo "4. 搜索功能测试："
curl -s "http://localhost:5000/api/search?keyword=心脏&limit=5" | jq '.results | length'

echo "5. AI匹配测试："
curl -s -X POST "http://localhost:5000/api/ai-match" \
  -H "Content-Type: application/json" \
  -d '{"query":"心脏起搏器","limit":3}' | jq '.results | length'

echo "====== 验证完成 ======"
```

### 7.2 性能基准测试

```bash
#!/bin/bash
# benchmark.sh

API_BASE="http://localhost:5000"

echo "====== 性能基准测试 ======"

# 1. 搜索接口
for keyword in "心脏" "支架" "导管"; do
    echo "搜索 '$keyword':"
    time curl -s "$API_BASE/api/search?keyword=$keyword&limit=20" > /dev/null
done

# 2. AI 匹配接口
echo "AI 匹配："
time curl -s -X POST "$API_BASE/api/ai-match" \
  -H "Content-Type: application/json" \
  -d '{"query":"心脏起搏器","limit":5}' > /dev/null

echo "====== 测试完成 ======"
```

---

## 八、回滚方案

如果迁移失败，执行回滚：

```bash
#!/bin/bash
# rollback.sh

echo "====== 执行回滚 ======"

# 1. 停止服务
pkill -f udid_server.py

# 2. 恢复配置文件
cp config.json.backup config.json

# 3. 恢复代码（git 回滚）
git checkout -- udid_hybrid_system.py

# 4. 重启原服务
python udid_server.py &

echo "====== 回滚完成 ======"
```

---

## 九、迁移时间线

| 阶段 | 任务 | 预计时间 | 影响 |
|------|------|----------|------|
| T-1天 | 备份、准备环境 | 1小时 | 无影响 |
| T-30分 | 停止服务、最终备份 | 10分钟 | 停机开始 |
| T-20分 | 数据迁移 | 15-20分钟 | 停机中 |
| T-5分 | 验证数据 | 5分钟 | 停机中 |
| T-0 | 启动新服务 | 2分钟 | 恢复服务 |
| T+1小时 | 监控、验证 | 1小时 | 观察期 |

---

## 十、注意事项

1. **向量索引不重建**：FAISS 文件直接复制使用，节省 API 调用费用
2. **全文检索差异**：PostgreSQL 中文分词需要额外配置，初期可能需要微调
3. **连接池配置**：根据并发量调整 `min_conn` 和 `max_conn`
4. **监控日志**：迁移后重点关注搜索响应时间和错误率

---

**文档版本**: 1.0
**创建日期**: 2026-02-05
**适用版本**: v1.0 → v2.0
