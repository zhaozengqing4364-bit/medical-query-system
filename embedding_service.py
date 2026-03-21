"""
UDID 向量检索服务
================

使用 OpenAI Embedding API 实现语义检索，提升召回质量。
支持 OpenAI 兼容接口（中转站）。

工作流程：
1. 预计算：为数据库中的产品生成向量，存入 embeddings 表
2. 查询：将用户需求转为向量，计算余弦相似度，召回最相关产品

版本: 1.0.0
"""

import os
import sqlite3
import time
import numpy as np
from typing import List, Dict, Optional, Tuple
import requests

from config_utils import load_env_file_once, merge_config_sources
from retry_utils import retry_with_backoff
from db_backend import connect as db_connect, is_postgres_backend

# 配置
BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
EMBEDDING_CONFIG_PATH = os.path.join(BASE_DIR, 'embedding_config.json')
DB_PATH = os.path.join(BASE_DIR, 'udid_hybrid_lake.db')

load_env_file_once(BASE_DIR, log_prefix='[Embedding]')

# ==========================================
# 配置管理
# ==========================================

def load_config() -> Dict:
    """加载 API 配置（数据库优先，环境变量兜底）"""
    env_mappings = {
        'EMBEDDING_API_URL': 'embedding_api_url',
        'EMBEDDING_API_KEY': 'embedding_api_key',
        'EMBEDDING_MODEL': 'embedding_model',
        'AI_API_BASE_URL': 'api_base_url',
        'AI_API_KEY': 'api_key',
    }
    return merge_config_sources(
        config_paths=[CONFIG_PATH, EMBEDDING_CONFIG_PATH],
        db_path=DB_PATH,
        env_mapping=env_mappings,
        log_prefix='[Embedding]',
        env_overrides_db=False,
    )

def get_embedding_config() -> Dict:
    """获取 Embedding 配置（从 config.json 读取）"""
    config = load_config()
    return {
        'model': config.get('embedding_model', 'text-embedding-v3'),
        'dim': config.get('embedding_dim', 1024),
        'batch_size': config.get('embedding_batch_size', 10)
    }

# ==========================================
# 数据库初始化
# ==========================================

def init_embedding_table(conn: sqlite3.Connection):
    """创建向量存储表"""
    cursor = conn.cursor()
    
    # 创建 embeddings 表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_embedding_hash ON embeddings(text_hash)')
    
    conn.commit()
    print("[Embedding] 向量表初始化完成")

# ==========================================
# Embedding API 调用
# ==========================================

@retry_with_backoff(max_retries=3, base_delay=1.0)
def get_embeddings(texts: List[str], config: Dict = None) -> Optional[List[List[float]]]:
    """
    调用 OpenAI Embedding API 获取文本向量
    
    Args:
        texts: 文本列表
        config: API 配置
    
    Returns:
        向量列表，每个向量是 float 列表
    """
    if not texts:
        return []
    
    if config is None:
        config = load_config()
    
    # 优先使用 embedding 专用配置，否则使用主配置；load_config 已处理多源优先级
    api_base = config.get('embedding_api_url') or config.get('api_base_url', '') or os.getenv('EMBEDDING_API_URL', '')
    api_base = api_base.rstrip('/')
    api_key = (
        config.get('embedding_api_key')
        or config.get('api_key', '')
        or os.getenv('EMBEDDING_API_KEY')
        or os.getenv('OPENAI_API_KEY')
        or ''
    )
    
    if not api_base or not api_key:
        print("[Embedding] API 配置不完整")
        return None
    
    # 使用 embeddings 端点
    url = f"{api_base}/embeddings"
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    # 获取配置的 embedding 模型
    model = config.get('embedding_model', 'text-embedding-v3') or os.getenv('EMBEDDING_MODEL') or 'text-embedding-v3'
    
    # 检测是否是阿里云 DashScope（它只支持单条输入）
    is_dashscope = 'dashscope' in api_base.lower()
    
    if is_dashscope:
        # 阿里云 DashScope: 逐条处理
        embeddings = []
        for text in texts:
            # 清理文本，移除可能导致问题的字符
            clean_text = text.strip()
            if not clean_text:
                clean_text = "空"
            
            payload = {
                'model': model,
                'input': clean_text  # DashScope 要求 input 是字符串
            }
            
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                data = response.json()
                
                if 'data' in data and len(data['data']) > 0:
                    embeddings.append(data['data'][0]['embedding'])
                else:
                    print(f"[Embedding] 响应格式异常: {data}")
                    return None
                    
            except requests.RequestException as e:
                print(f"[Embedding] API 请求失败: {e}")
                # 打印更多错误信息
                if hasattr(e, 'response') and e.response is not None:
                    print(f"[Embedding] 响应内容: {e.response.text[:500]}")
                raise
        
        return embeddings
    else:
        # OpenAI 标准格式: 批量处理
        payload = {
            'model': model,
            'input': texts
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            data = response.json()

            items = data.get('data')
            if not isinstance(items, list) or not items:
                raise ValueError("Embedding 响应缺少 data 列表")

            # 按 index 排序确保顺序正确；若 index 缺失则按原始顺序兜底
            try:
                items = sorted(items, key=lambda x: int(x.get('index', 0)))
            except Exception:
                pass

            embeddings = []
            for i, item in enumerate(items):
                embedding = item.get('embedding') if isinstance(item, dict) else None
                if not isinstance(embedding, list) or not embedding:
                    raise ValueError(f"Embedding 响应第 {i} 项缺少有效 embedding")
                embeddings.append(embedding)
            return embeddings
            
        except requests.RequestException as e:
            print(f"[Embedding] API 请求失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    print(f"[Embedding] 响应状态码: {e.response.status_code}")
                    print(f"[Embedding] 响应内容: {(e.response.text or '')[:500]}")
                except Exception:
                    pass
            raise
        except (ValueError, TypeError, KeyError, IndexError) as e:
            print(f"[Embedding] 响应结构异常: {e}")
            raise

def get_single_embedding(text: str, config: Dict = None) -> Optional[List[float]]:
    """获取单个文本的向量"""
    try:
        result = get_embeddings([text], config)
        return result[0] if result else None
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError):
        return None

# ==========================================
# 向量存储与检索
# ==========================================

def vector_to_blob(vector: List[float]) -> bytes:
    """将向量转为二进制存储"""
    return np.array(vector, dtype=np.float32).tobytes()

def blob_to_vector(blob: Optional[bytes]) -> Optional[np.ndarray]:
    """将二进制转回向量"""
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)

def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """计算余弦相似度"""
    dot = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)

def build_product_text(product: Dict) -> str:
    """构建产品的文本表示（用于生成向量）"""
    parts = []
    
    # 产品名称（最重要）
    if product.get('product_name'):
        parts.append(product['product_name'])
    
    # 规格型号
    if product.get('model'):
        parts.append(f"规格型号：{product['model']}")
    
    # 产品描述
    if product.get('description'):
        parts.append(product['description'][:500])
    
    # 适用范围
    if product.get('scope'):
        parts.append(f"适用范围：{product['scope'][:200]}")
    
    return ' '.join(parts)

def get_text_hash(text: str) -> str:
    """计算文本哈希，用于检测变化"""
    import hashlib
    normalized = (text or '').strip()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else ''


def _like_op() -> str:
    return "ILIKE" if is_postgres_backend() else "LIKE"


def _build_keyword_or_clause(alias: str, keywords: List[str], columns: List[str], like_op: str) -> Tuple[str, List[str]]:
    clauses: List[str] = []
    params: List[str] = []
    for raw_kw in keywords:
        kw = (raw_kw or '').strip()
        if not kw:
            continue
        pattern = f"%{kw}%"
        field_clauses = []
        for col in columns:
            field_clauses.append(f"{alias}.{col} {like_op} ?")
            params.append(pattern)
        clauses.append(f"({' OR '.join(field_clauses)})")
    return (" OR ".join(clauses) if clauses else "1=1", params)

# ==========================================
# 批量生成向量
# ==========================================

def build_embeddings(conn: sqlite3.Connection = None, force: bool = False) -> Dict:
    """
    为数据库中的产品批量生成向量（增量模式）
    
    Args:
        conn: 数据库连接
        force: 是否强制重新生成所有向量
    
    Returns:
        {'success': bool, 'processed': int, 'skipped': int, 'failed': int}
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    # 初始化表
    init_embedding_table(conn)
    
    cursor = conn.cursor()
    
    # 统计总数
    cursor.execute('SELECT COUNT(*) FROM products')
    total_products = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
    existing_embeddings = cursor.fetchone()[0]
    
    print(f"[Embedding] 产品总数: {total_products}, 已有向量: {existing_embeddings}")
    
    if force:
        # 强制模式：处理所有产品
        cursor.execute('''
            SELECT di_code, product_name, model, description, scope 
            FROM products
        ''')
        products = cursor.fetchall()
        print(f"[Embedding] 强制模式：需要处理全部 {len(products)} 个产品")
        existing = {}
    else:
        # 增量模式：只获取新增产品（不在 embeddings 表中的）
        cursor.execute('''
            SELECT p.di_code, p.product_name, p.model, p.description, p.scope 
            FROM products p
            LEFT JOIN embeddings e ON p.di_code = e.di_code
            WHERE e.di_code IS NULL
        ''')
        new_products = cursor.fetchall()
        print(f"[Embedding] 新增产品: {len(new_products)} 个")
        
        # 获取可能变更的产品（last_updated 比向量创建时间更新的）
        # 这样只检查最近同步更新的产品，而不是遍历全部
        cursor.execute('''
            SELECT p.di_code, p.product_name, p.model, p.description, p.scope, e.text_hash, e.embedding
            FROM products p
            INNER JOIN embeddings e ON p.di_code = e.di_code
            WHERE p.last_updated > e.created_at
               OR e.embedding IS NULL
        ''')
        candidate_products = cursor.fetchall()
        print(f"[Embedding] 可能变更产品（最近更新）: {len(candidate_products)} 个")
        
        # 检测内容变更的产品（比较 text_hash）
        changed_products = []
        for row in candidate_products:
            di_code, product_name, model, description, scope, old_hash, old_embedding = row
            product = {
                'di_code': di_code,
                'product_name': product_name,
                'model': model,
                'description': description,
                'scope': scope
            }
            text = build_product_text(product)
            new_hash = get_text_hash(text)
            if new_hash != old_hash or old_embedding is None:
                changed_products.append((di_code, product_name, model, description, scope))
        
        print(f"[Embedding] 确认内容变更: {len(changed_products)} 个")
        
        products = list(new_products) + changed_products
        existing = {}
    
    # 筛选需要处理的产品
    to_process = []
    skipped = total_products - len(products) if not force else 0
    
    for row in products:
        di_code = row[0]
        product = {
            'di_code': di_code,
            'product_name': row[1],
            'model': row[2],
            'description': row[3],
            'scope': row[4]
        }
        
        text = build_product_text(product)
        text_hash = get_text_hash(text)
        
        to_process.append({
            'di_code': di_code,
            'text': text,
            'text_hash': text_hash
        })
    
    print(f"[Embedding] 跳过 {skipped} 个（已有向量且未变化）")
    print(f"[Embedding] 需要处理 {len(to_process)} 个")
    
    if not to_process:
        return {'success': True, 'processed': 0, 'skipped': skipped, 'failed': 0}
    
    # 逐条处理（阿里云 DashScope 不支持批量）
    config = load_config()
    processed = 0
    failed = 0
    total = len(to_process)
    
    for i, item in enumerate(to_process):
        # 每 50 条显示一次进度
        if i % 50 == 0:
            print(f"[Embedding] 进度: {i}/{total} ({i*100//total}%)")
        
        embedding = get_single_embedding(item['text'], config)
        
        if embedding is None:
            failed += 1
            continue
        
        try:
            blob = vector_to_blob(embedding)
            cursor.execute('''
                INSERT OR REPLACE INTO embeddings (di_code, embedding, text_hash)
                VALUES (?, ?, ?)
            ''', (item['di_code'], blob, item['text_hash']))
            processed += 1
            
            # 每 100 条提交一次
            if processed % 100 == 0:
                conn.commit()
                
        except Exception as e:
            print(f"[Embedding] 存储失败 {item['di_code']}: {e}")
            failed += 1
    
    conn.commit()
    print(f"[Embedding] 完成: 处理 {processed}, 跳过 {skipped}, 失败 {failed}")
    
    success = failed == 0
    return {
        'success': success,
        'partial': processed > 0 and failed > 0,
        'processed': processed,
        'skipped': skipped,
        'failed': failed
    }

# ==========================================
# 向量检索
# ==========================================

def vector_search(query: str, conn: sqlite3.Connection = None, 
                  top_k: int = 50, filters: Dict = None) -> List[Dict]:
    """
    向量相似度检索（两阶段：FTS粗召回 + 向量重排）
    
    Args:
        query: 用户查询文本
        conn: 数据库连接
        top_k: 返回数量
        filters: 筛选条件
    
    Returns:
        产品列表，带有 similarity 字段
    """
    import time
    start_time = time.time()
    
    if conn is None:
        conn = db_connect(DB_PATH)
    
    cursor = conn.cursor()
    
    # ========================================
    # Stage 1: FTS5 粗召回 (快速，~50ms)
    # ========================================
    
    # 提取关键词（多提取一些，增加召回率）
    keywords = []
    try:
        import jieba.analyse
        keywords = jieba.analyse.extract_tags(query, topK=10)
        
        # 补充：对查询进行分词，确保核心词被包含
        import jieba
        words = list(jieba.cut(query))
        for w in words:
            if len(w) >= 2 and w not in keywords:
                keywords.append(w)
    except:
        # 简单分词
        keywords = [w for w in query.split() if len(w) >= 2]
    
    if not keywords:
        keywords = [query[:20]]  # 至少用查询的前20字符
    
    # 构建 FTS 查询（OR 关系，宽松召回）
    fts_query = " OR ".join([f'"{k}"' for k in keywords[:15]])  # 最多15个关键词
    
    # 构建筛选条件
    like_op = _like_op()
    where_conditions = []
    where_params = []

    if filters:
        if filters.get('category_code'):
            where_conditions.append(f"p.category_code {like_op} ?")
            where_params.append(f"{filters['category_code']}%")
        if filters.get('manufacturer'):
            where_conditions.append(f"p.manufacturer {like_op} ?")
            where_params.append(f"%{filters['manufacturer']}%")
        if filters.get('keyword'):
            where_conditions.append(
                f"(p.product_name {like_op} ? OR p.description {like_op} ?)"
            )
            where_params.extend([f"%{filters['keyword']}%"] * 2)

    where_sql = " AND ".join(where_conditions) if where_conditions else "1=1"

    # 召回 Top 1000（增加召回量，提高覆盖率）
    RECALL_SIZE = 1000
    candidates = []

    if is_postgres_backend():
        keyword_sql, keyword_params = _build_keyword_or_clause(
            alias='p',
            keywords=keywords[:15],
            columns=['product_name', 'commercial_name', 'model', 'manufacturer', 'description', 'scope', 'cert_no'],
            like_op=like_op
        )
        recall_sql = f'''
            SELECT p.di_code, p.product_name, p.commercial_name, p.model,
                   p.manufacturer, p.description, p.publish_date, p.source,
                   p.last_updated, p.category_code, p.scope
            FROM products p
            WHERE ({keyword_sql}) AND {where_sql}
            ORDER BY p.last_updated DESC
            LIMIT {RECALL_SIZE}
        '''
        try:
            cursor.execute(recall_sql, keyword_params + where_params)
            candidates = cursor.fetchall()
        except Exception as e:
            print(f"[Embedding] PostgreSQL 召回失败: {e}")
    else:
        params = [fts_query] + where_params
        recall_sql = f'''
            SELECT p.di_code, p.product_name, p.commercial_name, p.model,
                   p.manufacturer, p.description, p.publish_date, p.source,
                   p.last_updated, p.category_code, p.scope
            FROM products p
            INNER JOIN products_fts f ON p.rowid = f.rowid
            WHERE f.products_fts MATCH ? AND {where_sql}
            ORDER BY f.rank
            LIMIT {RECALL_SIZE}
        '''

        try:
            cursor.execute(recall_sql, params)
            candidates = cursor.fetchall()
        except Exception as e:
            print(f"[Embedding] FTS 召回失败: {e}")

        # 如果 FTS 召回太少，尝试用 LIKE 补充
        if len(candidates) < 100:
            print(f"[Embedding] FTS 召回不足，尝试 LIKE 补充...")
            like_conditions = []
            like_params = []

            for kw in keywords[:5]:
                like_conditions.append(f"(p.product_name {like_op} ? OR p.description {like_op} ?)")
                like_params.extend([f"%{kw}%", f"%{kw}%"])

            if like_conditions:
                like_where = " OR ".join(like_conditions)
                if where_conditions:
                    like_where = f"({like_where}) AND {where_sql}"

                like_sql = f'''
                    SELECT p.di_code, p.product_name, p.commercial_name, p.model,
                           p.manufacturer, p.description, p.publish_date, p.source,
                           p.last_updated, p.category_code, p.scope
                    FROM products p
                    WHERE {like_where}
                    LIMIT {RECALL_SIZE - len(candidates)}
                '''

                try:
                    cursor.execute(like_sql, like_params + where_params)
                    extra = cursor.fetchall()

                    # 去重合并
                    existing_codes = {c[0] for c in candidates}
                    for row in extra:
                        if row[0] not in existing_codes:
                            candidates.append(row)
                            existing_codes.add(row[0])

                    print(f"[Embedding] LIKE 补充后共 {len(candidates)} 条")
                except Exception as e:
                    print(f"[Embedding] LIKE 补充失败: {e}")

    recall_time = time.time() - start_time
    print(f"[Embedding] 粗召回 {len(candidates)} 条，关键词: {keywords[:5]}...，耗时 {recall_time*1000:.0f}ms")

    if not candidates:
        print("[Embedding] 粗召回无结果，尝试全字段 LIKE 回退...")
        fallback_conditions = []
        fallback_params = []

        if filters:
            if filters.get('category_code'):
                fallback_conditions.append(f"p.category_code {like_op} ?")
                fallback_params.append(f"{filters['category_code']}%")
            if filters.get('manufacturer'):
                fallback_conditions.append(f"p.manufacturer {like_op} ?")
                fallback_params.append(f"%{filters['manufacturer']}%")
            if filters.get('keyword'):
                fallback_conditions.append(
                    f"(p.product_name {like_op} ? OR p.description {like_op} ?)"
                )
                fallback_params.extend([f"%{filters['keyword']}%"] * 2)

        fallback_conditions.append(
            f"(p.product_name {like_op} ? OR p.description {like_op} ? OR p.model {like_op} ? OR p.manufacturer {like_op} ?)"
        )
        fallback_params.extend([f"%{query}%"] * 4)

        fallback_where = " AND ".join(fallback_conditions) if fallback_conditions else "1=1"
        fallback_sql = f'''
            SELECT p.di_code, p.product_name, p.commercial_name, p.model,
                   p.manufacturer, p.description, p.publish_date, p.source,
                   p.last_updated, p.category_code, p.scope
            FROM products p
            WHERE {fallback_where}
            LIMIT {RECALL_SIZE}
        '''

        try:
            cursor.execute(fallback_sql, fallback_params)
            candidates = cursor.fetchall()
            print(f"[Embedding] LIKE 回退召回 {len(candidates)} 条")
        except Exception as e:
            print(f"[Embedding] LIKE 回退失败: {e}")
    
    if not candidates:
        return []
    
    # ========================================
    # Stage 2: 向量重排 (精确，~200ms)
    # ========================================
    
    # 获取查询向量
    config = load_config()
    query_embedding = get_single_embedding(query, config)
    
    if query_embedding is None:
        print("[Embedding] 无法获取查询向量，返回 FTS 结果")
        # 降级：直接返回 FTS 结果
        columns = ['di_code', 'product_name', 'commercial_name', 'model', 
                   'manufacturer', 'description', 'publish_date', 'source', 
                   'last_updated', 'category_code', 'scope']
        return [dict(zip(columns, row)) for row in candidates[:top_k]]
    
    query_vector = np.array(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_vector)
    
    if query_norm > 0:
        query_vector = query_vector / query_norm
    
    # 获取候选产品的向量
    di_codes = [row[0] for row in candidates]
    
    # 分批查询向量（避免 SQL 参数过多）
    embedding_map = {}
    BATCH = 500
    for i in range(0, len(di_codes), BATCH):
        batch_codes = di_codes[i:i+BATCH]
        placeholders = ','.join(['?' for _ in batch_codes])
        
        cursor.execute(f'''
            SELECT di_code, embedding FROM embeddings 
            WHERE di_code IN ({placeholders})
        ''', batch_codes)
        
        for row in cursor.fetchall():
            vector = blob_to_vector(row[1])
            if vector is not None:
                embedding_map[row[0]] = vector
    
    # 计算相似度并重排
    results = []
    dimension_mismatch_count = 0
    columns = ['di_code', 'product_name', 'commercial_name', 'model', 
               'manufacturer', 'description', 'publish_date', 'source', 
               'last_updated', 'category_code', 'scope']
    
    for row in candidates:
        di_code = row[0]
        item = dict(zip(columns, row))
        
        if di_code in embedding_map:
            product_vector = embedding_map[di_code]
            if product_vector.shape[0] != query_vector.shape[0]:
                dimension_mismatch_count += 1
                if dimension_mismatch_count <= 5:
                    print(
                        f"[Embedding] 维度不一致，跳过 {di_code}: "
                        f"query={query_vector.shape[0]}, product={product_vector.shape[0]}"
                    )
                item['similarity'] = 0.0
                item['_has_embedding'] = False
                results.append(item)
                continue
            product_norm = np.linalg.norm(product_vector)
            
            if product_norm > 0:
                similarity = np.dot(query_vector, product_vector) / product_norm
                item['similarity'] = float(similarity)
            else:
                item['similarity'] = 0.0
            item['_has_embedding'] = True
        else:
            # 没有向量的产品不参与相似度提升
            item['similarity'] = 0.0
            item['_has_embedding'] = False
        
        results.append(item)
    
    embedding_coverage = len(embedding_map) / len(candidates) if candidates else 0
    print(f"[Embedding] 向量覆盖率 {embedding_coverage*100:.1f}% ({len(embedding_map)}/{len(candidates)})")
    if dimension_mismatch_count > 0:
        print(f"[Embedding] 发现 {dimension_mismatch_count} 条向量维度异常记录，已自动跳过")

    # 按向量覆盖优先 + 相似度排序
    results.sort(key=lambda x: (x.get('_has_embedding', False), x.get('similarity', 0)), reverse=True)

    for item in results:
        item.pop('_has_embedding', None)
    
    total_time = time.time() - start_time
    print(f"[Embedding] 向量重排完成，总耗时 {total_time*1000:.0f}ms，返回 {min(top_k, len(results))} 条")
    
    return results[:top_k]

# ==========================================
# 混合检索（向量 + 关键词）
# ==========================================

def extract_keywords(text: str, top_k: int = 10) -> List[str]:
    """
    提取文本中的关键词（用于前端高亮）
    
    Args:
        text: 输入文本
        top_k: 返回关键词数量
    
    Returns:
        关键词列表
    """
    if not text:
        return []
    
    try:
        import jieba.analyse
        keywords = jieba.analyse.extract_tags(text, topK=top_k)
        return keywords
    except:
        # 简单分词回退
        words = [w for w in text.split() if len(w) >= 2]
        return words[:top_k]


def is_long_description(query: str, threshold: int = 20) -> bool:
    """
    判断查询是否为长文本描述（应使用纯向量召回）
    
    长文本特征：
    - 长度 > threshold
    - 包含中文描述性词汇
    """
    if not query:
        return False
    
    # 长度判断
    if len(query) > threshold:
        return True
    
    # 包含描述性词汇
    descriptive_words = ['适用于', '用于', '可用于', '规格', '型号', '注射', '植入', '填充']
    for word in descriptive_words:
        if word in query:
            return True
    
    return False


def hybrid_search(query: str, conn: sqlite3.Connection = None,
                  top_k: int = 50, filters: Dict = None,
                  dedupe_by_manufacturer: bool = False,
                  product_name: str = None,
                  specs: str = None,
                  force_vector_recall: bool = False,
                  return_keywords: bool = True,
                  min_score: int = 0) -> List[Dict]:
    """
    三阶段语义检索：
    1. 产品名称过滤（硬性条件）
    2. 规格型号过滤（关键词匹配）
    3. 参数需求向量排序（相似度）

    智能策略：
    - 长文本需求（>20字符）：优先使用纯向量召回（FAISS）
    - 短关键词：使用 FTS 召回 + 向量重排

    Args:
        query: 参数需求描述（功能、用途等）
        conn: 数据库连接
        top_k: 返回数量（去重后），最大支持1000
        filters: 其他筛选条件
        dedupe_by_manufacturer: 是否按厂家去重
        product_name: 产品名称（用于第一阶段过滤）
        specs: 规格型号（用于第二阶段过滤）
        force_vector_recall: 强制使用纯向量召回
        return_keywords: 是否返回高亮关键词
        min_score: 最低匹配分数（0-100），0表示不限制
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    # 提取查询关键词（用于前端高亮）
    query_keywords = extract_keywords(query, top_k=8) if return_keywords and query else []
    
    cursor = conn.cursor()
    
    # ========================================
    # 第一阶段：产品名称过滤（三层策略）
    # ========================================
    candidate_di_codes = None
    name_match_scores = {}  # 记录产品名称匹配分数（通用算法，不写死任何产品类型）
    user_name_keywords = set()  # 用户输入的关键词集合
    name_candidates = []  # 产品名称候选池
    BROAD_NAME_POOL_THRESHOLD = 500
    
    if product_name and product_name.strip():
        product_name = product_name.strip()
        
        # 预处理：提取用户输入的关键词（用于后续相似度计算）
        stop_words = {'一次性', '使用', '用', '型', '式', '的', '及', '与', '医用', '无菌'}
        try:
            import jieba
            user_name_keywords = set(w for w in jieba.cut(product_name) if len(w) >= 2 and w not in stop_words)
            print(f"[Search] 用户产品名关键词: {user_name_keywords}")
        except ImportError:
            user_name_keywords = {product_name}
        
        # ---- 策略 1：精确匹配 ----
        cursor.execute(
            'SELECT di_code FROM products WHERE product_name = ?',
            (product_name,)
        )
        exact_matches = [row[0] for row in cursor.fetchall()]
        
        if exact_matches:
            candidate_di_codes = set(exact_matches)
            for di in exact_matches:
                name_match_scores[di] = 1.0  # 精确匹配满分
            print(f"[Search] 策略1-精确匹配: {len(candidate_di_codes)} 条")
        
        # ---- 策略 2：双向包含匹配（核心改进）----
        # 解决：用户输入"一次性使用气管切开插管"，数据库是"气管切开插管"（子集）
        # 注意：即使策略1有结果，也继续执行策略2来补充更多候选（不同厂家）
        need_more_candidates = (candidate_di_codes is None or len(candidate_di_codes) < 100)
        if need_more_candidates:
            # 2a. 正向：DB名称 包含 用户输入
            cursor.execute(
                'SELECT di_code, product_name FROM products WHERE product_name LIKE ?',
                (f'%{product_name}%',)
            )
            forward_matches = cursor.fetchall()
            
            # 2b. 反向：用户输入 包含 DB名称（DB名称长度>=4，避免匹配过短的词）
            if is_postgres_backend():
                # PostgreSQL 下避免在 SQL 字面量中使用 '%'，防止 pyformat 占位符解析冲突
                reverse_sql = (
                    "SELECT di_code, product_name FROM products "
                    "WHERE LENGTH(product_name) >= 4 "
                    "AND POSITION(product_name IN ?) > 0"
                )
            else:
                # SQLite 使用 instr(全文, 子串) 判断“用户输入包含数据库名称”
                reverse_sql = (
                    "SELECT di_code, product_name FROM products "
                    "WHERE LENGTH(product_name) >= 4 "
                    "AND instr(?, product_name) > 0"
                )
            cursor.execute(reverse_sql, (product_name,))
            reverse_matches = cursor.fetchall()
            
            all_matches = list(set(forward_matches + reverse_matches))
            
            if all_matches:
                # 初始化或合并到现有候选池
                if candidate_di_codes is None:
                    candidate_di_codes = set()
                added_count = 0
                for di_code, db_name in all_matches:
                    if di_code not in candidate_di_codes:
                        candidate_di_codes.add(di_code)
                        added_count += 1
                        # 计算名称相似度（Jaccard）
                        try:
                            db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                            if user_name_keywords and db_keywords:
                                intersection = user_name_keywords & db_keywords
                                union = user_name_keywords | db_keywords
                                jaccard = len(intersection) / len(union) if union else 0
                                name_match_scores[di_code] = jaccard
                            else:
                                name_match_scores[di_code] = 0.5
                        except:
                            name_match_scores[di_code] = 0.5
                print(f"[Search] 策略2-双向包含匹配补充: +{added_count} 条，总计 {len(candidate_di_codes)} 条")
        
        # ---- 策略 3：分词关键词匹配（优先全匹配，再降级部分匹配）----
        if not candidate_di_codes and user_name_keywords:
            # 3a. 优先：AND 逻辑，要求所有关键词都匹配
            and_conditions = ' AND '.join(['product_name LIKE ?' for _ in user_name_keywords])
            and_params = [f'%{kw}%' for kw in user_name_keywords]

            cursor.execute(
                f'SELECT di_code, product_name FROM products WHERE {and_conditions}',
                and_params
            )
            full_matches = cursor.fetchall()

            if full_matches:
                candidate_di_codes = set()
                for di_code, db_name in full_matches:
                    candidate_di_codes.add(di_code)
                    name_match_scores[di_code] = 1.0  # 全匹配高分
                print(f"[Search] 策略3a-全关键词匹配: {len(candidate_di_codes)} 条")

            # 3b. 降级：智能部分匹配（避免过于宽泛的关键词淹没结果）
            if len(candidate_di_codes or set()) < 10:
                # 关键改进：如果参数需求中有描述性信息，尝试从中提取关键词用于交叉验证
                query_keywords = set()
                if query and len(query) > 5:
                    try:
                        import jieba.analyse
                        query_keywords = set(jieba.analyse.extract_tags(query, topK=5))
                        # 过滤掉通用词
                        query_keywords = {w for w in query_keywords if w not in stop_words and len(w) >= 2}
                        print(f"[Search] 从参数需求提取辅助关键词: {query_keywords}")
                    except:
                        pass

                # 对关键词按重要性排序（长度越长、越具体的词优先级越高）
                sorted_keywords = sorted(user_name_keywords, key=lambda x: (-len(x), x))

                # 限制每个关键词的匹配数量，避免过于宽泛的词淹没结果
                max_per_keyword = 500
                partial_matches_all = []

                for kw in sorted_keywords:
                    cursor.execute(
                        'SELECT di_code, product_name FROM products WHERE product_name LIKE ? LIMIT ?',
                        (f'%{kw}%', max_per_keyword)
                    )
                    matches = cursor.fetchall()
                    partial_matches_all.extend(matches)

                    # 如果匹配太多，说明这个词太宽泛，记录日志
                    if len(matches) >= max_per_keyword:
                        print(f"[Search] 关键词 '{kw}' 匹配过多(>{max_per_keyword})，已截断，建议用户输入更具体的产品名")

                # 去重
                seen_di = set()
                partial_matches = []
                for di_code, db_name in partial_matches_all:
                    if di_code not in seen_di:
                        seen_di.add(di_code)
                        partial_matches.append((di_code, db_name))

                if partial_matches:
                    if candidate_di_codes is None:
                        candidate_di_codes = set()

                    # 如果有参数需求关键词，优先保留匹配这些词的产品
                    if query_keywords:
                        prioritized = []
                        for di_code, db_name in partial_matches:
                            db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                            cross_match = query_keywords & db_keywords
                            if cross_match:
                                prioritized.append((di_code, db_name, len(cross_match)))
                            else:
                                prioritized.append((di_code, db_name, 0))
                        # 按交叉匹配数量降序排列
                        prioritized.sort(key=lambda x: -x[2])
                        partial_matches = [(p[0], p[1]) for p in prioritized]
                        print(f"[Search] 已按参数需求关键词交叉排序，优先产品数: {sum(1 for p in prioritized if p[2] > 0)}")

                    for di_code, db_name in partial_matches:
                        if di_code not in candidate_di_codes:
                            candidate_di_codes.add(di_code)
                            # 计算关键词覆盖率作为匹配分数
                            try:
                                db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                                matched = user_name_keywords & db_keywords
                                coverage = len(matched) / len(user_name_keywords) if user_name_keywords else 0
                                # 额外加分：如果匹配参数需求关键词
                                if query_keywords:
                                    cross_matched = query_keywords & db_keywords
                                    coverage += len(cross_matched) * 0.1  # 交叉匹配额外加分
                                name_match_scores[di_code] = min(1.0, coverage * 0.7 + 0.1)  # 部分匹配降权，但保底0.1
                            except:
                                name_match_scores[di_code] = 0.3
                    print(f"[Search] 策略3b-智能部分匹配补充后: {len(candidate_di_codes)} 条")
        
        if not candidate_di_codes:
            print(f"[Search] 产品名称无匹配，使用全量检索（将惩罚名称不符的结果）")
    
    # ========================================
    # 第二阶段：规格型号过滤（关键词匹配）
    # ========================================
    specs_match_scores = {}  # 记录规格匹配分数
    
    if specs and specs.strip():
        specs = specs.strip()
        
        # 提取规格关键词
        try:
            import jieba
            spec_stop_words = {'型', '式', '的', '及', '与', '用', '带', '含'}
            spec_keywords = [w for w in jieba.cut(specs) if len(w) >= 2 and w not in spec_stop_words]
            # 补充：保留数字+单位组合（如 2000ml）
            import re
            number_units = re.findall(r'\d+(?:ml|ML|mL|L|l|mm|cm|m|g|kg|Fr|fr|FR)?', specs)
            spec_keywords.extend([u for u in number_units if u not in spec_keywords])
            print(f"[Search] 规格关键词: {spec_keywords}")
        except ImportError:
            spec_keywords = [specs]
        
        if spec_keywords and candidate_di_codes:
            # 关键修复：规格作为排序因素，而不是硬性过滤
            # 问题：规格写法多样（40g/40克/40G/40ml），硬性过滤会漏掉很多产品
            # 解决：给规格匹配的产品加分，但不过滤不匹配的产品

            placeholders = ','.join(['?' for _ in candidate_di_codes])
            spec_conditions = ' OR '.join(['model LIKE ?' for _ in spec_keywords])

            cursor.execute(f'''
                SELECT di_code, model FROM products
                WHERE di_code IN ({placeholders}) AND ({spec_conditions})
            ''', list(candidate_di_codes) + [f'%{kw}%' for kw in spec_keywords])

            spec_matches = cursor.fetchall()

            if spec_matches:
                # 计算每个产品的规格匹配分数
                for di_code, model in spec_matches:
                    matched_count = sum(1 for kw in spec_keywords if kw in (model or ''))
                    specs_match_scores[di_code] = matched_count / len(spec_keywords) if spec_keywords else 0

                spec_matched_count = len(spec_matches)
                print(f"[Search] 规格匹配: {spec_matched_count}/{len(candidate_di_codes)} 条")
                # 注意：不再过滤候选集，只记录匹配分数用于后续排序
            else:
                print(f"[Search] 规格无匹配，仍保留全部 {len(candidate_di_codes)} 条候选")
        elif spec_keywords and not candidate_di_codes:
            # 没有产品名称候选，直接用规格在全库搜索
            spec_conditions = ' OR '.join(['model LIKE ?' for _ in spec_keywords])
            cursor.execute(f'''
                SELECT di_code, model FROM products WHERE {spec_conditions} LIMIT 5000
            ''', [f'%{kw}%' for kw in spec_keywords])
            
            spec_matches = cursor.fetchall()
            if spec_matches:
                candidate_di_codes = set(row[0] for row in spec_matches)
                for di_code, model in spec_matches:
                    matched_count = sum(1 for kw in spec_keywords if kw in (model or ''))
                    specs_match_scores[di_code] = matched_count / len(spec_keywords) if spec_keywords else 0
                print(f"[Search] 仅规格搜索召回: {len(candidate_di_codes)} 条")
    
    # ========================================
    # 第三阶段：参数需求向量检索
    # ========================================
    
    # 如果没有参数需求，直接返回产品名称匹配的结果
    if not query or not query.strip():
        if candidate_di_codes:
            placeholders = ','.join(['?' for _ in candidate_di_codes])
            cursor.execute(f'''
                SELECT di_code, product_name, commercial_name, model, manufacturer,
                       description, publish_date, source, last_updated, category_code, scope
                FROM products WHERE di_code IN ({placeholders})
                ORDER BY last_updated DESC LIMIT ?
            ''', list(candidate_di_codes) + [top_k * 5])
            
            rows = cursor.fetchall()
            columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                       'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
            
            results = [dict(zip(columns, row)) for row in rows]
            
            # 厂家去重
            if dedupe_by_manufacturer:
                seen_mfr = set()
                deduped = []
                for item in results:
                    mfr = (item.get('manufacturer') or '').strip()
                    if not mfr or mfr not in seen_mfr:
                        deduped.append(item)
                        if mfr:
                            seen_mfr.add(mfr)
                        if len(deduped) >= top_k:
                            break
                results = deduped
            
            # 添加默认分数
            for i, item in enumerate(results[:top_k]):
                item['matchScore'] = 80  # 产品名称匹配，但无参数需求评分
                item['rank'] = i + 1
                item['final_score'] = 0.5
            
            return results[:top_k]
        else:
            return []
    
    # 有参数需求，进行向量检索
    # 关键：如果产品名称匹配数量很多，需要增大召回数量
    name_match_count = len(candidate_di_codes) if candidate_di_codes else 0
    if name_match_count > 1000:
        recall_k = 3000  # 大量匹配时，召回更多
    elif name_match_count > 500:
        recall_k = 2000
    else:
        recall_k = 1000  # 默认召回1000条
    vector_results = []
    print(f"[Search] 产品名称匹配 {name_match_count} 条，设置向量召回 recall_k={recall_k}")
    
    # 智能策略：长文本优先使用纯向量召回
    use_pure_vector = force_vector_recall or is_long_description(query)
    recall_method = "vector" if use_pure_vector else "hybrid"
    
    if use_pure_vector:
        print(f"[Search] 使用纯向量召回策略（长文本描述）")
    
    try:
        from embedding_faiss import faiss_search, get_faiss_index
        
        faiss_idx = get_faiss_index()
        if faiss_idx.index is not None:
            # 仅在名称候选池不宽泛时合并产品名，避免“支架/导管”等宽词污染语义查询
            should_merge_name = bool(product_name)
            if should_merge_name and candidate_di_codes and len(candidate_di_codes) > BROAD_NAME_POOL_THRESHOLD:
                should_merge_name = False
                print(
                    f"[Search] 产品名称候选池过宽({len(candidate_di_codes)}条)，"
                    "向量检索仅使用参数需求文本"
                )
            combined_query = f"{product_name} {query}".strip() if should_merge_name else query
            if should_merge_name:
                print(f"[Search] 合并产品名称进行向量检索: '{combined_query[:50]}...'")

            # 关键修复：不传递 keyword 过滤，产品名称过滤已在第一阶段完成
            # 避免向量召回结果被错误的关键词过滤（如：向量召回"引流管"但被"负压支架"过滤为0）
            faiss_filters = {
                'category_code': filters.get('category_code') if filters else None,
                'manufacturer': filters.get('manufacturer') if filters else None
            }
            faiss_filters = {k: v for k, v in faiss_filters.items() if v}  # 移除空值
            vector_results = faiss_search(combined_query, conn, recall_k, faiss_filters if faiss_filters else None)
            print(f"[Search] FAISS 召回 {len(vector_results)} 条")
    except Exception as e:
        print(f"[Search] FAISS 失败: {e}")

    if not vector_results:
        # FTS+向量重排回退路径沿用同一查询策略
        should_merge_name = bool(product_name)
        if should_merge_name and candidate_di_codes and len(candidate_di_codes) > BROAD_NAME_POOL_THRESHOLD:
            should_merge_name = False
        combined_query = f"{product_name} {query}".strip() if should_merge_name else query
        vector_results = vector_search(combined_query, conn, recall_k, filters)
        recall_method = "fts_vector"

    # 如果有产品名称匹配，确保这些候选进入向量排序池（避免被需求描述的 FTS 过滤掉）
    if candidate_di_codes:
        try:
            max_name_candidates = min(len(candidate_di_codes), 2000)  # 增大到2000条
            name_code_list = list(candidate_di_codes)[:max_name_candidates]
            placeholders = ','.join(['?' for _ in name_code_list])
            cursor.execute(f'''
                SELECT di_code, product_name, commercial_name, model, manufacturer,
                       description, publish_date, source, last_updated, category_code, scope
                FROM products WHERE di_code IN ({placeholders})
            ''', name_code_list)
            rows = cursor.fetchall()
            columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                       'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
            name_candidates = [dict(zip(columns, row)) for row in rows]

            if name_candidates:
                similarity_map = {r.get('di_code'): r.get('similarity', 0) for r in vector_results}
                merged_map = {r.get('di_code'): r for r in vector_results}

                for item in name_candidates:
                    di_code = item.get('di_code')
                    if di_code in similarity_map:
                        item['similarity'] = similarity_map[di_code]
                    if di_code not in merged_map:
                        merged_map[di_code] = item
                    else:
                        # 若向量结果缺相似度，用名称候选补齐
                        if merged_map[di_code].get('similarity', 0) == 0 and item.get('similarity', 0) > 0:
                            merged_map[di_code]['similarity'] = item.get('similarity', 0)

                vector_results = list(merged_map.values())
                print(f"[Search] 合并产品名称候选 {len(name_candidates)} 条 -> 总候选 {len(vector_results)} 条")
        except Exception as e:
            print(f"[Search] 合并产品名称候选失败: {e}")
    
    # ========================================
    # 合并：RRF (Reciprocal Rank Fusion) 多路融合排序
    # ========================================
    # RRF 公式: score(d) = Σ 1/(k + rank_i)
    # k=60 是经验常数，避免高排名权重过大
    
    RRF_K = 60
    
    if candidate_di_codes:
        # 关键修复：合并向量召回和产品名称候选，而不是过滤
        # 问题：向量召回的1000条中可能只有少数匹配产品名称
        # 解决：将产品名称匹配的候选加入结果池

        # 判断产品名称候选池是否过于宽泛（召回过多）
        name_pool_size = len(candidate_di_codes)
        is_name_pool_too_broad = name_pool_size > BROAD_NAME_POOL_THRESHOLD

        if is_name_pool_too_broad:
            print(f"[Search] 产品名称候选池过大({name_pool_size}条)，优先信任向量召回结果")

        # 1. 先收集向量召回中匹配产品名称的
        vector_matched = [r for r in vector_results if r['di_code'] in candidate_di_codes]
        print(f"[Search] 向量召回中匹配产品名称: {len(vector_matched)} 条")

        # 2. 如果产品名称候选池过大，保留所有向量召回结果（即使不在候选池中）
        # 因为此时产品名称匹配太宽泛，向量召回更可靠
        if is_name_pool_too_broad and len(vector_matched) < len(vector_results):
            # 添加向量召回中有但产品名称候选池中没有的结果
            vector_codes_in_matched = {r['di_code'] for r in vector_matched}
            extra_vector_results = [r for r in vector_results if r['di_code'] not in vector_codes_in_matched]
            if extra_vector_results:
                # 给这些产品标记为"向量召回额外结果"，后续排序时给予适当权重
                for r in extra_vector_results:
                    r['_vector_only'] = True
                vector_matched.extend(extra_vector_results)
                print(f"[Search] 向量召回补充额外结果: {len(extra_vector_results)} 条")

        # 3. 如果向量召回覆盖不足（且候选池不过大），补充产品名称候选
        vector_di_codes = {r['di_code'] for r in vector_results}
        missing_codes = candidate_di_codes - vector_di_codes

        if missing_codes and not is_name_pool_too_broad:
            # 批量查询缺失的产品信息（限制数量避免SQL过长）
            missing_list = list(missing_codes)[:min(len(missing_codes), 1000)]
            placeholders = ','.join(['?' for _ in missing_list])
            try:
                cursor.execute(f'''
                    SELECT di_code, product_name, commercial_name, model, manufacturer,
                           description, publish_date, source, last_updated, category_code, scope
                    FROM products WHERE di_code IN ({placeholders})
                ''', missing_list)
                rows = cursor.fetchall()
                columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                           'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
                missing_products = [dict(zip(columns, row)) for row in rows]

                # 给补充的产品一个默认相似度（稍低于向量召回的平均值）
                avg_sim = sum(r.get('similarity', 0.5) for r in vector_results) / len(vector_results) if vector_results else 0.5
                for p in missing_products:
                    p['similarity'] = avg_sim * 0.9  # 稍降权

                print(f"[Search] 补充产品名称候选: {len(missing_products)} 条")
                vector_matched.extend(missing_products)
            except Exception as e:
                print(f"[Search] 补充产品名称候选失败: {e}")

        filtered_results = vector_matched
        print(f"[Search] 合并后候选池: {len(filtered_results)} 条")

        # 3. 如果仍然太少，回退到名称候选池
        if not filtered_results and name_candidates:
            print(f"[Search] 名称为空，回退到名称候选池")
            filtered_results = name_candidates
        
        if filtered_results:
            # ========================================
            # RRF 多路融合排序
            # ========================================
            
            # 路1: 名称精确度排序（name_score 降序）
            name_sorted = sorted(filtered_results, key=lambda x: -name_match_scores.get(x['di_code'], 0))
            name_rank = {r['di_code']: i+1 for i, r in enumerate(name_sorted)}
            
            # 路2: 向量相似度排序（similarity 降序）
            vec_sorted = sorted(filtered_results, key=lambda x: -x.get('similarity', 0))
            vec_rank = {r['di_code']: i+1 for i, r in enumerate(vec_sorted)}
            
            # 路3: 精确匹配加成（name_score=1.0 的排第一）
            exact_match_codes = {di for di, score in name_match_scores.items() if score >= 0.99}
            
            # 计算 RRF 分数
            for r in filtered_results:
                di_code = r['di_code']
                name_score = name_match_scores.get(di_code, 0.5)
                is_vector_only = r.get('_vector_only', False)

                # RRF 融合
                rrf_name = 1.0 / (RRF_K + name_rank.get(di_code, 999))
                rrf_vec = 1.0 / (RRF_K + vec_rank.get(di_code, 999))

                # 精确匹配额外加成（确保"止血海绵"排在"鼻腔止血海绵"前面）
                exact_bonus = 0.05 if di_code in exact_match_codes else 0

                # 关键改进：当产品名称候选池过于宽泛时，给向量召回额外结果更高的向量权重
                if is_vector_only and is_name_pool_too_broad:
                    # 向量召回额外结果在宽泛匹配时获得更高的向量权重
                    r['_rrf_score'] = rrf_name * 0.3 + rrf_vec * 1.2 + exact_bonus
                else:
                    r['_rrf_score'] = rrf_name + rrf_vec + exact_bonus

                # 对多关键词输入提高名称匹配门槛，降低“宽词误命中”噪音
                if len(user_name_keywords) >= 2:
                    name_match_threshold = 0.55 if is_name_pool_too_broad else 0.4
                else:
                    name_match_threshold = 0.3

                r['_combined_score'] = r['_rrf_score']
                r['_name_match'] = name_score >= name_match_threshold
                r['_name_score'] = name_score

            # 按 RRF 分数排序
            filtered_results.sort(key=lambda x: (-x.get('_rrf_score', 0), -x.get('similarity', 0)))
            print(f"[Search] RRF 排序完成，前3名: {[r.get('product_name', '')[:10] for r in filtered_results[:3]]}")
        
        vector_results = filtered_results
    else:
        # 没有产品名称过滤成功 → 全量检索模式
        # 关键：对每个结果计算与用户输入的名称相似度，惩罚名称不符的产品
        if product_name and user_name_keywords:
            print(f"[Search] 全量检索模式，计算名称相似度进行惩罚")
            try:
                import jieba
                for r in vector_results:
                    db_name = r.get('product_name', '')
                    db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                    
                    if user_name_keywords and db_keywords:
                        intersection = user_name_keywords & db_keywords
                        union = user_name_keywords | db_keywords
                        name_sim = len(intersection) / len(union) if union else 0
                    else:
                        name_sim = 0
                    
                    vec_score = r.get('similarity', 0)
                    # 名称相似度权重 70%，向量相似度权重 30%（严格惩罚名称不符）
                    r['_combined_score'] = name_sim * 0.7 + vec_score * 0.3
                    if len(user_name_keywords) >= 2:
                        name_match_threshold = 0.4
                    else:
                        name_match_threshold = 0.3
                    r['_name_match'] = name_sim >= name_match_threshold
                    r['_name_sim'] = name_sim
                
                # 按综合分数重新排序
                vector_results.sort(key=lambda x: -x.get('_combined_score', 0))
            except ImportError:
                for r in vector_results:
                    r['_name_match'] = False
                    r['_combined_score'] = r.get('similarity', 0)
        else:
            # 没有提供产品名称，正常使用向量相似度
            for r in vector_results:
                r['_name_match'] = False
                r['_combined_score'] = r.get('similarity', 0)
    
    # 构建结果（先打分，再去重）
    scored_results = []
    for idx, item in enumerate(vector_results):
        raw_sim = item.get('similarity', 0)
        rrf_score = item.get('_rrf_score', 0)  # RRF 融合分数
        name_score = item.get('_name_score', 0)  # 名称精确度分数
        is_name_match = item.get('_name_match', False)
        di_code = item.get('di_code', '')

        # 规格匹配分数（如果有）
        spec_score = specs_match_scores.get(di_code, 0)

        # 符合率计算（RRF优化版）：
        # - 精确匹配(name_score=1.0)：基础分 90%，再加向量相似度加成
        # - 部分匹配(name_score<1.0)：基础分 70%，再加向量相似度加成
        # - 名称不匹配：基础分 70，向量相似度权重增加（给向量相似度高的产品更多机会）

        # 检查是否是宽泛匹配场景（名称候选池过大）
        is_broad_match = len(candidate_di_codes) > BROAD_NAME_POOL_THRESHOLD if candidate_di_codes else False
        if is_broad_match and idx == 0:  # 只在第一条记录时打印一次
            print(f"[Search] 启用宽泛匹配评分模式（候选池{len(candidate_di_codes)}条），向量相似度权重提升")

        if is_name_match:
            # 关键修复：宽泛匹配场景下大幅降低名称匹配权重，让向量相似度主导排序
            if is_broad_match:
                # 宽泛匹配场景（如"支架"匹配了7万+产品）：
                # 名称匹配只给基础优势，主要靠向量相似度拉开差距
                if name_score >= 0.99:
                    # 精确匹配但候选池过大：72 + 向量相似度 * 23（最高95）
                    base_score = 72
                    sim_boost = raw_sim * 23
                else:
                    # 部分匹配且候选池过大：70 + 向量相似度 * 25（最高95）
                    base_score = 70
                    sim_boost = raw_sim * 25
            else:
                # 正常场景：保持原有评分逻辑
                if name_score >= 0.99:
                    # 精确匹配：90 + 向量相似度 * 10（最高100）
                    base_score = 90
                    sim_boost = raw_sim * 10
                else:
                    # 部分匹配：70 + 向量相似度 * 18（最高88）
                    base_score = 70
                    sim_boost = raw_sim * 18
            match_score = min(100, max(base_score, int(base_score + sim_boost)))
        else:
            # 关键修复：名称不匹配时基础分从60提高到70，向量相似度权重增加
            # 在宽泛匹配场景下，给向量召回的产品更多机会
            if is_broad_match:
                # 宽泛匹配场景：向量相似度权重更高（与名称匹配产品同一起跑线）
                match_score = min(95, max(70, int(70 + raw_sim * 25)))
            else:
                # 正常场景：基础分70，向量相似度加成
                match_score = min(85, max(70, int(70 + raw_sim * 15)))

        # 规格匹配加分（最高+5分）
        if spec_score > 0:
            spec_boost = int(spec_score * 5)
            match_score = min(100, match_score + spec_boost)

        # 关键改进：交叉匹配加分（产品名称关键词与参数需求关键词都匹配时加分）
        # 这有助于在宽泛匹配场景下，提升同时匹配名称和功能描述的产品
        cross_match_bonus = 0
        if is_broad_match and user_name_keywords and query_keywords:
            try:
                db_name = item.get('product_name', '')
                db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                # 检查是否同时匹配名称关键词和查询关键词
                name_match_count = len(user_name_keywords & db_keywords)
                query_match_count = len(set(query_keywords) & db_keywords)
                if name_match_count > 0 and query_match_count > 0:
                    # 交叉匹配加分：同时匹配名称和查询关键词的产品获得额外加分
                    cross_match_bonus = min(5, name_match_count + query_match_count)
                    match_score = min(100, match_score + cross_match_bonus)
            except:
                pass

        item['_match_score'] = match_score
        item['_spec_score'] = spec_score
        item['_cross_bonus'] = cross_match_bonus
        scored_results.append(item)
    
    # 按 matchScore 降序排序，精确匹配优先（name_score 作为次要排序键）
    scored_results.sort(key=lambda x: (-x.get('_match_score', 0), -x.get('_name_score', 0)))
    
    # 调试输出：前5条结果的评分详情
    if scored_results:
        broad_indicator = " [宽泛匹配模式]" if is_broad_match else ""
        print(f"[Search] 评分详情（前5条）{broad_indicator}:")
        for idx, r in enumerate(scored_results[:5]):
            cross_info = f"交叉+{r.get('_cross_bonus', 0)} | " if r.get('_cross_bonus', 0) > 0 else ""
            name_indicator = "✓" if r.get('_name_match', False) else "✗"
            print(f"  {idx+1}. {r.get('product_name', '')[:20]}... | "
                  f"名称{name_indicator} | "
                  f"规格={r.get('_spec_score', 0):.1f} | "
                  f"{cross_info}"
                  f"sim={r.get('similarity', 0):.2f} | "
                  f"score={r.get('_match_score', 0)}%")
    
    # 厂家去重（在打分之后进行）
    if dedupe_by_manufacturer:
        seen_mfr = set()
        deduped = []
        for item in scored_results:
            mfr = (item.get('manufacturer') or '').strip()
            if not mfr or mfr not in seen_mfr:
                deduped.append(item)
                if mfr:
                    seen_mfr.add(mfr)
                if len(deduped) >= top_k:
                    break
        scored_results = deduped
        print(f"[Search] 厂家去重后 {len(scored_results)} 条")
    
    # 按最低分数过滤（如果设置了 min_score）
    if min_score > 0:
        before_filter = len(scored_results)
        scored_results = [item for item in scored_results if item.get('_match_score', 0) >= min_score]
        print(f"[Search] 最低分数过滤: {before_filter} -> {len(scored_results)} 条 (min_score={min_score})")

    # 构建最终结果
    results = []
    for i, item in enumerate(scored_results[:top_k]):
        match_score = item.get('_match_score', 0)
        combined_score = item.get('_combined_score', 0)

        # 清理临时字段
        item.pop('_name_match', None)
        item.pop('_name_sim', None)
        item.pop('_combined_score', None)
        item.pop('_match_score', None)
        item.pop('_spec_score', None)
        item.pop('_cross_bonus', None)
        item.pop('_name_score', None)
        item.pop('_rrf_score', None)
        item.pop('_vector_only', None)

        results.append({
            **item,
            'rank': i + 1,
            'matchScore': match_score,
            'final_score': combined_score,
            'highlightKeywords': list(query_keywords) if query_keywords else []  # 用于前端高亮，转换为list确保JSON可序列化
        })

    return results

# ==========================================
# 命令行工具
# ==========================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='UDID 向量检索服务')
    parser.add_argument('--build', action='store_true', help='构建向量索引')
    parser.add_argument('--force', action='store_true', help='强制重建所有向量')
    parser.add_argument('--search', type=str, help='测试搜索')
    parser.add_argument('--stats', action='store_true', help='显示统计信息')
    
    args = parser.parse_args()
    
    conn = db_connect(DB_PATH)
    
    if args.build:
        print("开始构建向量索引...")
        result = build_embeddings(conn, force=args.force)
        print(f"结果: {result}")
    
    elif args.search:
        print(f"搜索: {args.search}")
        results = hybrid_search(args.search, conn, top_k=10)
        print(f"\n找到 {len(results)} 个结果:\n")
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r['final_score']:.3f}] {r['product_name']}")
            print(f"   厂家: {r['manufacturer']}")
            print(f"   规格: {r['model']}")
            print()
    
    elif args.stats:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM products')
        total_products = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
        total_embeddings = cursor.fetchone()[0]
        
        print(f"产品总数: {total_products}")
        print(f"已生成向量: {total_embeddings}")
        print(f"覆盖率: {total_embeddings / total_products * 100:.1f}%")
    
    else:
        parser.print_help()
    
    conn.close()
