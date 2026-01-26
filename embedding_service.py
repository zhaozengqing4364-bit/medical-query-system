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
import json
import sqlite3
import numpy as np
from typing import List, Dict, Optional, Tuple
import requests

# 配置
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
EMBEDDING_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'embedding_config.json')
DB_PATH = os.path.join(os.path.dirname(__file__), 'udid_hybrid_lake.db')

# ==========================================
# 配置管理
# ==========================================

def load_config() -> Dict:
    """加载 API 配置"""
    config: Dict = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
        if os.path.exists(EMBEDDING_CONFIG_PATH):
            with open(EMBEDDING_CONFIG_PATH, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
    except Exception as e:
        print(f"[Embedding] 加载配置失败: {e}")
    return config

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
    
    # 优先使用 embedding 专用配置，否则使用主配置
    api_base = os.getenv('EMBEDDING_API_URL') or config.get('embedding_api_url') or config.get('api_base_url', '')
    api_base = api_base.rstrip('/')
    api_key = os.getenv('EMBEDDING_API_KEY') or config.get('embedding_api_key') or os.getenv('OPENAI_API_KEY') or config.get('api_key', '')
    
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
    model = os.getenv('EMBEDDING_MODEL') or config.get('embedding_model', 'text-embedding-v3')
    
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
                return None
        
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
            
            # 按 index 排序确保顺序正确
            embeddings = sorted(data['data'], key=lambda x: x['index'])
            return [item['embedding'] for item in embeddings]
            
        except requests.RequestException as e:
            print(f"[Embedding] API 请求失败: {e}")
            return None

def get_single_embedding(text: str, config: Dict = None) -> Optional[List[float]]:
    """获取单个文本的向量"""
    result = get_embeddings([text], config)
    return result[0] if result else None

# ==========================================
# 向量存储与检索
# ==========================================

def vector_to_blob(vector: List[float]) -> bytes:
    """将向量转为二进制存储"""
    return np.array(vector, dtype=np.float32).tobytes()

def blob_to_vector(blob: bytes) -> np.ndarray:
    """将二进制转回向量"""
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
        conn = sqlite3.connect(DB_PATH)
    
    # 初始化表
    init_embedding_table(conn)
    
    cursor = conn.cursor()
    
    # 统计总数
    cursor.execute('SELECT COUNT(*) FROM products')
    total_products = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM embeddings')
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
            SELECT p.di_code, p.product_name, p.model, p.description, p.scope, e.text_hash
            FROM products p
            INNER JOIN embeddings e ON p.di_code = e.di_code
            WHERE p.last_updated > e.created_at
        ''')
        candidate_products = cursor.fetchall()
        print(f"[Embedding] 可能变更产品（最近更新）: {len(candidate_products)} 个")
        
        # 检测内容变更的产品（比较 text_hash）
        changed_products = []
        for row in candidate_products:
            di_code, product_name, model, description, scope, old_hash = row
            product = {
                'di_code': di_code,
                'product_name': product_name,
                'model': model,
                'description': description,
                'scope': scope
            }
            text = build_product_text(product)
            new_hash = get_text_hash(text)
            if new_hash != old_hash:
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
    
    return {
        'success': True,
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
        conn = sqlite3.connect(DB_PATH)
    
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
    where_conditions = []
    params = [fts_query]
    
    if filters:
        if filters.get('category_code'):
            where_conditions.append("p.category_code LIKE ?")
            params.append(f"{filters['category_code']}%")
        if filters.get('manufacturer'):
            where_conditions.append("p.manufacturer LIKE ?")
            params.append(f"%{filters['manufacturer']}%")
        if filters.get('keyword'):
            where_conditions.append(
                "(p.product_name LIKE ? OR p.description LIKE ?)"
            )
            params.extend([f"%{filters['keyword']}%"] * 2)
    
    where_sql = " AND ".join(where_conditions) if where_conditions else "1=1"
    
    # FTS 召回 Top 1000（增加召回量，提高覆盖率）
    RECALL_SIZE = 1000
    
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
    
    candidates = []
    try:
        cursor.execute(recall_sql, params)
        candidates = cursor.fetchall()
    except Exception as e:
        print(f"[Embedding] FTS 召回失败: {e}")
    
    recall_time = time.time() - start_time
    fts_count = len(candidates)
    print(f"[Embedding] FTS 粗召回 {fts_count} 条，关键词: {keywords[:5]}...，耗时 {recall_time*1000:.0f}ms")
    
    # 如果 FTS 召回太少，尝试用 LIKE 补充
    if len(candidates) < 100:
        print(f"[Embedding] FTS 召回不足，尝试 LIKE 补充...")
        like_conditions = []
        like_params = []
        
        for kw in keywords[:5]:
            like_conditions.append("(p.product_name LIKE ? OR p.description LIKE ?)")
            like_params.extend([f"%{kw}%", f"%{kw}%"])
        
        if like_conditions:
            like_where = " OR ".join(like_conditions)
            if where_conditions:
                like_where = f"({like_where}) AND {where_sql.replace(fts_query, '1=1')}"
            
            like_sql = f'''
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, 
                       p.manufacturer, p.description, p.publish_date, p.source, 
                       p.last_updated, p.category_code, p.scope
                FROM products p
                WHERE {like_where}
                LIMIT {RECALL_SIZE - len(candidates)}
            '''
            
            try:
                # 移除 FTS 参数
                like_params_final = like_params + [p for p in params[1:] if p != fts_query]
                cursor.execute(like_sql, like_params_final)
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

    if not candidates:
        print("[Embedding] FTS 无结果，尝试全字段 LIKE 回退...")
        fallback_conditions = []
        fallback_params = []

        if filters:
            if filters.get('category_code'):
                fallback_conditions.append("p.category_code LIKE ?")
                fallback_params.append(f"{filters['category_code']}%")
            if filters.get('manufacturer'):
                fallback_conditions.append("p.manufacturer LIKE ?")
                fallback_params.append(f"%{filters['manufacturer']}%")
            if filters.get('keyword'):
                fallback_conditions.append(
                    "(p.product_name LIKE ? OR p.description LIKE ?)"
                )
                fallback_params.extend([f"%{filters['keyword']}%"] * 2)

        fallback_conditions.append(
            "(p.product_name LIKE ? OR p.description LIKE ? OR p.model LIKE ? OR p.manufacturer LIKE ?)"
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
            embedding_map[row[0]] = blob_to_vector(row[1])
    
    # 计算相似度并重排
    results = []
    columns = ['di_code', 'product_name', 'commercial_name', 'model', 
               'manufacturer', 'description', 'publish_date', 'source', 
               'last_updated', 'category_code', 'scope']
    
    for row in candidates:
        di_code = row[0]
        item = dict(zip(columns, row))
        
        if di_code in embedding_map:
            product_vector = embedding_map[di_code]
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
                  dedupe_by_manufacturer: bool = True,
                  product_name: str = None,
                  force_vector_recall: bool = False,
                  return_keywords: bool = True) -> List[Dict]:
    """
    两阶段语义检索：
    1. 产品名称过滤（硬性条件）
    2. 参数需求向量排序（相似度）
    
    智能策略：
    - 长文本需求（>20字符）：优先使用纯向量召回（FAISS）
    - 短关键词：使用 FTS 召回 + 向量重排
    
    Args:
        query: 参数需求描述（规格、适用范围等）
        conn: 数据库连接
        top_k: 返回数量（去重后）
        filters: 其他筛选条件
        dedupe_by_manufacturer: 是否按厂家去重
        product_name: 产品名称（用于第一阶段过滤）
        force_vector_recall: 强制使用纯向量召回
        return_keywords: 是否返回高亮关键词
    """
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
    
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
        if not candidate_di_codes:
            # 2a. 正向：DB名称 包含 用户输入
            cursor.execute(
                'SELECT di_code, product_name FROM products WHERE product_name LIKE ?',
                (f'%{product_name}%',)
            )
            forward_matches = cursor.fetchall()
            
            # 2b. 反向：用户输入 包含 DB名称（DB名称长度>=4，避免匹配过短的词）
            cursor.execute(
                'SELECT di_code, product_name FROM products WHERE LENGTH(product_name) >= 4 AND ? LIKE \'%\' || product_name || \'%\'',
                (product_name,)
            )
            reverse_matches = cursor.fetchall()
            
            all_matches = list(set(forward_matches + reverse_matches))
            
            if all_matches:
                candidate_di_codes = set()
                for di_code, db_name in all_matches:
                    candidate_di_codes.add(di_code)
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
                print(f"[Search] 策略2-双向包含匹配: {len(candidate_di_codes)} 条")
        
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
            
            # 3b. 降级：OR 逻辑，部分匹配（仅当全匹配结果太少时）
            if len(candidate_di_codes or set()) < 10:
                or_conditions = ' OR '.join(['product_name LIKE ?' for _ in user_name_keywords])
                or_params = [f'%{kw}%' for kw in user_name_keywords]
                
                cursor.execute(
                    f'SELECT di_code, product_name FROM products WHERE {or_conditions}',
                    or_params
                )
                partial_matches = cursor.fetchall()
                
                if partial_matches:
                    if candidate_di_codes is None:
                        candidate_di_codes = set()
                    for di_code, db_name in partial_matches:
                        if di_code not in candidate_di_codes:
                            candidate_di_codes.add(di_code)
                            # 计算关键词覆盖率作为匹配分数
                            try:
                                db_keywords = set(w for w in jieba.cut(db_name) if len(w) >= 2 and w not in stop_words)
                                matched = user_name_keywords & db_keywords
                                coverage = len(matched) / len(user_name_keywords) if user_name_keywords else 0
                                name_match_scores[di_code] = coverage * 0.7  # 部分匹配降权
                            except:
                                name_match_scores[di_code] = 0.3
                    print(f"[Search] 策略3b-部分匹配补充后: {len(candidate_di_codes)} 条")
        
        if not candidate_di_codes:
            print(f"[Search] 产品名称无匹配，使用全量检索（将惩罚名称不符的结果）")
    
    # ========================================
    # 第二阶段：参数需求向量检索
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
    recall_k = 500  # 向量召回，重点是参数需求匹配度
    vector_results = []
    
    # 智能策略：长文本优先使用纯向量召回
    use_pure_vector = force_vector_recall or is_long_description(query)
    recall_method = "vector" if use_pure_vector else "hybrid"
    
    if use_pure_vector:
        print(f"[Search] 使用纯向量召回策略（长文本描述）")
    
    try:
        from embedding_faiss import faiss_search, get_faiss_index
        
        faiss_idx = get_faiss_index()
        if faiss_idx.index is not None:
            # 只用参数需求做向量检索（不包含产品名称）
            # 关键修复：不传递 keyword 过滤，产品名称过滤已在第一阶段完成
            # 避免向量召回结果被错误的关键词过滤（如：向量召回"引流管"但被"负压支架"过滤为0）
            faiss_filters = {
                'category_code': filters.get('category_code') if filters else None,
                'manufacturer': filters.get('manufacturer') if filters else None
            }
            faiss_filters = {k: v for k, v in faiss_filters.items() if v}  # 移除空值
            vector_results = faiss_search(query, conn, recall_k, faiss_filters if faiss_filters else None)
            print(f"[Search] FAISS 召回 {len(vector_results)} 条")
    except Exception as e:
        print(f"[Search] FAISS 失败: {e}")
    
    if not vector_results:
        vector_results = vector_search(query, conn, recall_k, filters)
        recall_method = "fts_vector"

    # 如果有产品名称匹配，确保这些候选进入向量排序池（避免被需求描述的 FTS 过滤掉）
    if candidate_di_codes:
        try:
            max_name_candidates = 500  # 避免 SQL 参数过多
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
    # 合并：产品名称过滤 + 向量排序（改进版）
    # ========================================
    
    if candidate_di_codes:
        # 只保留产品名称匹配的
        filtered_results = [r for r in vector_results if r['di_code'] in candidate_di_codes]
        print(f"[Search] 产品名称过滤后 {len(filtered_results)} 条")
        
        # 如果过滤后太少，放宽条件但优先展示匹配的
        if len(filtered_results) < 10:
            if not filtered_results and name_candidates:
                print(f"[Search] 名称过滤为空，回退到名称候选池")
                filtered_results = name_candidates
                for r in filtered_results:
                    di_code = r['di_code']
                    name_score = name_match_scores.get(di_code, 0.4)
                    vec_score = r.get('similarity', 0)
                    # 名称优先：名称匹配权重 70%，向量相似度权重 30%
                    r['_combined_score'] = name_score * 0.7 + vec_score * 0.3
                    r['_name_match'] = name_score >= 0.3
                filtered_results.sort(key=lambda x: -x.get('_combined_score', 0))
            else:
                print(f"[Search] 过滤后结果太少，混合排序（名称匹配优先）")
                # 为每个结果计算综合分数
                for r in vector_results:
                    di_code = r['di_code']
                    name_score = name_match_scores.get(di_code, 0)  # 名称匹配分数 0-1
                    vec_score = r.get('similarity', 0)  # 向量相似度 0-1
                    # 综合分数：名称匹配权重 60%，向量相似度权重 40%
                    r['_combined_score'] = name_score * 0.6 + vec_score * 0.4
                    r['_name_match'] = name_score > 0
                # 按综合分数排序
                vector_results.sort(key=lambda x: -x.get('_combined_score', 0))
                filtered_results = vector_results
        else:
            # 过滤结果足够多，也按综合分数排序
            for r in filtered_results:
                di_code = r['di_code']
                name_score = name_match_scores.get(di_code, 0.5)  # 默认0.5（已通过名称过滤）
                vec_score = r.get('similarity', 0)
                r['_combined_score'] = name_score * 0.5 + vec_score * 0.5
                r['_name_match'] = True  # 关键修复：已通过名称过滤，标记为名称匹配
            filtered_results.sort(key=lambda x: -x.get('_combined_score', 0))
        
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
                    r['_name_match'] = name_sim >= 0.3  # 相似度>=30%视为名称匹配
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
    for item in vector_results:
        raw_sim = item.get('similarity', 0)
        combined_score = item.get('_combined_score', raw_sim)
        is_name_match = item.get('_name_match', False)
        
        # 符合率计算（改进版）：
        # - 产品名称匹配的，基础分 70%，再加向量相似度加成
        # - 产品名称不匹配的，只用向量相似度
        if is_name_match:
            # 名称匹配：70 + 向量相似度 * 30（最高100）
            # 如果 similarity 为 0，使用 combined_score 作为补偿
            sim_boost = max(raw_sim, combined_score) * 30
            match_score = min(100, max(70, int(70 + sim_boost)))
        else:
            # 名称不匹配：向量相似度 * 60（最高60）
            match_score = min(60, max(0, int(raw_sim * 60)))
        
        item['_match_score'] = match_score
        scored_results.append(item)
    
    # 按 matchScore 降序排序（确保高分在前）
    scored_results.sort(key=lambda x: -x.get('_match_score', 0))
    
    # 调试输出：前5条结果的评分详情
    if scored_results:
        print(f"[Search] 评分详情（前5条）:")
        for idx, r in enumerate(scored_results[:5]):
            print(f"  {idx+1}. {r.get('product_name', '')[:20]}... | "
                  f"名称匹配={r.get('_name_match', False)} | "
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
        
        results.append({
            **item,
            'rank': i + 1,
            'matchScore': match_score,
            'final_score': combined_score,
            'highlightKeywords': query_keywords  # 用于前端高亮
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
    
    conn = sqlite3.connect(DB_PATH)
    
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
        
        cursor.execute('SELECT COUNT(*) FROM embeddings')
        total_embeddings = cursor.fetchone()[0]
        
        print(f"产品总数: {total_products}")
        print(f"已生成向量: {total_embeddings}")
        print(f"覆盖率: {total_embeddings / total_products * 100:.1f}%")
    
    else:
        parser.print_help()
    
    conn.close()
