"""
PostgreSQL 数据库连接模块
用于替代原有的 SQLite 连接

使用方法:
    from db_postgres import get_db_stats, search_products

    # 获取统计信息
    stats = get_db_stats()

    # 搜索产品
    results, total = search_products("心脏起搏器", limit=20)
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

_db_password = os.getenv('POSTGRES_PASSWORD', '').strip()
if not _db_password or _db_password == 'your_secure_password':
    raise RuntimeError("必须设置有效的 POSTGRES_PASSWORD，禁止使用默认占位密码。")

# 数据库连接配置（从环境变量读取）
DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', '5432')),
    'database': os.getenv('POSTGRES_DB', 'udid_db'),
    'user': os.getenv('POSTGRES_USER', 'udid_user'),
    'password': _db_password,
}

# 连接池（全局单例）
_connection_pool: Optional[ThreadedConnectionPool] = None


def init_connection_pool(min_conn: int = 2, max_conn: int = 10):
    """
    初始化数据库连接池

    Args:
        min_conn: 最小连接数
        max_conn: 最大连接数
    """
    global _connection_pool
    if _connection_pool is not None:
        return

    try:
        _connection_pool = ThreadedConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            **DB_CONFIG
        )
        logger.info(f"PostgreSQL 连接池初始化成功 ({min_conn}-{max_conn})")
    except Exception as e:
        logger.error(f"连接池初始化失败: {e}")
        raise


def close_connection_pool():
    """关闭连接池"""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("PostgreSQL 连接池已关闭")


@contextmanager
def get_connection():
    """
    获取数据库连接（上下文管理器）

    使用示例:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products")
    """
    global _connection_pool
    if _connection_pool is None:
        init_connection_pool()

    conn = None
    try:
        conn = _connection_pool.getconn()
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"数据库操作失败: {e}")
        raise
    finally:
        if conn:
            _connection_pool.putconn(conn)


@contextmanager
def get_cursor(cursor_factory=None):
    """
    获取数据库游标

    使用示例:
        with get_cursor(RealDictCursor) as cur:
            cur.execute("SELECT * FROM products")
            results = cur.fetchall()
    """
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
        finally:
            cursor.close()


# ============================================
# 产品查询函数
# ============================================

def get_product_by_di_code(di_code: str) -> Optional[Dict]:
    """
    通过 DI 编码获取产品信息

    Args:
        di_code: DI 编码

    Returns:
        产品信息字典，不存在返回 None
    """
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
    全文检索产品（使用 PostgreSQL 中文全文检索）

    Args:
        keyword: 搜索关键词
        limit: 返回数量限制
        offset: 分页偏移

    Returns:
        (结果列表, 总数量)
    """
    with get_cursor(RealDictCursor) as cur:
        # 计算总数量
        cur.execute("""
            SELECT COUNT(*) as count FROM products
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


def search_products_simple(
    keyword: str,
    limit: int = 20
) -> List[Dict]:
    """
    简单关键词搜索（用于模糊匹配）

    Args:
        keyword: 搜索关键词
        limit: 返回数量限制

    Returns:
        产品列表
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM products
            WHERE product_name ILIKE %s
               OR manufacturer ILIKE %s
               OR model ILIKE %s
            ORDER BY last_updated DESC
            LIMIT %s
        """, (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', limit))
        return cur.fetchall()


def search_products_fuzzy(
    keyword: str,
    limit: int = 20
) -> List[Dict]:
    """
    模糊搜索（使用 pg_trgm 相似度）

    适用于容忍拼写错误的场景

    Args:
        keyword: 搜索关键词
        limit: 返回数量限制

    Returns:
        按相似度排序的产品列表
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT
                *,
                GREATEST(
                    similarity(product_name, %s),
                    similarity(manufacturer, %s)
                ) as sml
            FROM products
            WHERE
                product_name %% %s
                OR manufacturer %% %s
            ORDER BY sml DESC
            LIMIT %s
        """, (keyword, keyword, keyword, keyword, limit))
        return cur.fetchall()


def get_products_by_manufacturer(
    manufacturer: str,
    limit: int = 50
) -> List[Dict]:
    """
    按生产企业搜索

    Args:
        manufacturer: 生产企业名称（支持模糊匹配）
        limit: 返回数量限制

    Returns:
        产品列表
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM products
            WHERE manufacturer ILIKE %s
            ORDER BY last_updated DESC
            LIMIT %s
        """, (f'%{manufacturer}%', limit))
        return cur.fetchall()


def get_products_by_category(
    category_code: str,
    limit: int = 50
) -> List[Dict]:
    """
    按分类编码搜索

    Args:
        category_code: 分类编码
        limit: 返回数量限制

    Returns:
        产品列表
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM products
            WHERE category_code = %s
            ORDER BY last_updated DESC
            LIMIT %s
        """, (category_code, limit))
        return cur.fetchall()


# ============================================
# 向量相关函数
# ============================================

def get_embedding_meta(di_code: str) -> Optional[Dict]:
    """
    获取向量元数据（不包含向量数据本身）

    向量数据存储在 FAISS 索引文件中，不在数据库中

    Args:
        di_code: DI 编码

    Returns:
        向量元数据字典
    """
    with get_cursor(RealDictCursor) as cur:
        cur.execute("""
            SELECT di_code, text_hash, created_at
            FROM embeddings
            WHERE di_code = %s
        """, (di_code,))
        return cur.fetchone()


def check_embedding_exists(di_code: str, text_hash: str) -> bool:
    """
    检查向量是否存在且未过期

    Args:
        di_code: DI 编码
        text_hash: 文本哈希（用于检测内容变化）

    Returns:
        是否存在且哈希匹配
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT 1 FROM embeddings
            WHERE di_code = %s AND text_hash = %s
        """, (di_code, text_hash))
        return cur.fetchone() is not None


def save_embedding_meta(di_code: str, text_hash: str):
    """
    保存向量元数据

    Args:
        di_code: DI 编码
        text_hash: 文本哈希
    """
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO embeddings (di_code, text_hash, created_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (di_code) DO UPDATE SET
                text_hash = EXCLUDED.text_hash,
                created_at = EXCLUDED.created_at
        """, (di_code, text_hash))


def batch_save_embedding_meta(items: List[Tuple[str, str]], batch_size: int = 1000):
    """
    批量保存向量元数据

    Args:
        items: (di_code, text_hash) 元组列表
        batch_size: 批次大小
    """
    if not items:
        return

    with get_cursor() as cur:
        for i in range(0, len(items), batch_size):
            batch = items[i:i+batch_size]
            execute_values(
                cur,
                """
                INSERT INTO embeddings (di_code, text_hash, created_at)
                VALUES %s
                ON CONFLICT (di_code) DO UPDATE SET
                    text_hash = EXCLUDED.text_hash,
                    created_at = EXCLUDED.created_at
                """,
                [(code, hash, datetime.now()) for code, hash in batch]
            )


# ============================================
# 统计和日志函数
# ============================================

def get_db_stats() -> Dict[str, Any]:
    """
    获取数据库统计信息

    Returns:
        统计信息字典
    """
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
            SELECT pg_size_pretty(pg_database_size(current_database())) as size
        """)
        stats['db_size'] = cur.fetchone()['size']

        return stats


def save_search_log(
    query: str,
    query_type: str,
    results_count: int,
    response_time_ms: int,
    client_ip: str = None
):
    """
    记录搜索日志

    Args:
        query: 查询词
        query_type: 查询类型
        results_count: 返回结果数
        response_time_ms: 响应时间（毫秒）
        client_ip: 客户端IP
    """
    try:
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO search_logs
                (query, query_type, results_count, response_time_ms, client_ip)
                VALUES (%s, %s, %s, %s, %s)
            """, (query, query_type, results_count, response_time_ms, client_ip))
    except Exception as e:
        logger.error(f"记录搜索日志失败: {e}")


# ============================================
# 批量操作
# ============================================

def batch_insert_products(products: List[Dict], batch_size: int = 1000):
    """
    批量插入产品数据

    Args:
        products: 产品字典列表
        batch_size: 批次大小
    """
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
                 cert_no, status, product_type, phone, email, scope, safety_info, last_updated)
                VALUES %s
                ON CONFLICT (di_code) DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    commercial_name = EXCLUDED.commercial_name,
                    model = EXCLUDED.model,
                    manufacturer = EXCLUDED.manufacturer,
                    description = EXCLUDED.description,
                    publish_date = EXCLUDED.publish_date,
                    category_code = EXCLUDED.category_code,
                    social_code = EXCLUDED.social_code,
                    cert_no = EXCLUDED.cert_no,
                    status = EXCLUDED.status,
                    product_type = EXCLUDED.product_type,
                    phone = EXCLUDED.phone,
                    email = EXCLUDED.email,
                    scope = EXCLUDED.scope,
                    safety_info = EXCLUDED.safety_info,
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
                    p.get('safety_info'),
                    p.get('last_updated', datetime.now())
                ) for p in batch]
            )
            logger.info(f"已插入批次 {i//batch_size + 1}/{(len(products)-1)//batch_size + 1}")


# ============================================
# 兼容层：模拟原有 SQLite 接口
# ============================================

class DatabaseConnection:
    """
    兼容层：模拟原有 SQLite 连接对象

    用于最小化代码改动，逐步迁移
    """

    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = _connection_pool.getconn() if _connection_pool else None
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
            _connection_pool.putconn(self.conn)


def get_connection_compat():
    """
    兼容函数：返回类 SQLite 连接对象

    使用示例（兼容旧代码）:
        conn = get_connection_compat()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE di_code = %s", (code,))
    """
    if _connection_pool is None:
        init_connection_pool()
    return DatabaseConnection()


# 初始化（模块加载时自动执行）
try:
    init_connection_pool()
except Exception as e:
    logger.warning(f"自动初始化连接池失败: {e}")
