"""
UDID 向量批量处理服务（阿里云 Batch API）
========================================

使用阿里云 DashScope Batch API 批量生成向量，适合首次构建大量数据。

流程：
1. 生成 JSONL 文件（每行一个产品）
2. 上传文件到阿里云
3. 创建 Batch 任务
4. 等待完成，下载结果
5. 解析结果，存入数据库

版本: 1.0.0
"""

import os
import json
import sqlite3
import time
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests
import numpy as np
from urllib.parse import urlsplit

from config_utils import load_env_file_once, merge_config_sources
from retry_utils import retry_with_backoff
from db_backend import connect as db_connect, is_postgres_backend

# 配置
BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DB_PATH = os.path.join(BASE_DIR, 'udid_hybrid_lake.db')
BATCH_DIR = os.path.join(BASE_DIR, 'data', 'embedding_batch')
PIPELINE_STATE_DB = os.path.join(BATCH_DIR, 'pipeline_state.db')
LEGACY_PIPELINE_STATE_FILE = os.path.join(BATCH_DIR, 'pipeline_state.json')

# 确保目录存在
os.makedirs(BATCH_DIR, exist_ok=True)

load_env_file_once(BASE_DIR, log_prefix='[Batch]')

def _url_origin(api_base: str) -> str:
    api_base = (api_base or '').strip()
    if not api_base:
        return ''
    parts = urlsplit(api_base)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return api_base.rstrip('/')


def load_config() -> Dict:
    """加载配置（优先级：数据库 > 环境变量 > 配置文件）"""
    env_mappings = {
        'EMBEDDING_API_URL': 'embedding_api_url',
        'EMBEDDING_API_KEY': 'embedding_api_key',
        'EMBEDDING_MODEL': 'embedding_model',
    }
    return merge_config_sources(
        config_paths=[CONFIG_PATH],
        db_path=DB_PATH,
        env_mapping=env_mappings,
        log_prefix='[Batch]',
        env_overrides_db=False,
    )

def get_api_config(config: Dict = None) -> tuple:
    """获取 API 配置（数据库优先，环境变量兜底）"""
    if config is None:
        config = load_config()

    api_base = config.get('embedding_api_url') or config.get('api_base_url', '') or os.getenv('EMBEDDING_API_URL', '')
    api_base = api_base.rstrip('/')
    api_key = config.get('embedding_api_key') or config.get('api_key', '') or os.getenv('EMBEDDING_API_KEY', '')
    model = config.get('embedding_model', 'text-embedding-v3') or os.getenv('EMBEDDING_MODEL') or 'text-embedding-v3'

    return api_base, api_key, model

def build_product_text(product: Dict) -> str:
    """构建产品的文本表示"""
    parts = []
    if product.get('product_name'):
        parts.append(product['product_name'])
    if product.get('model'):
        parts.append(f"规格型号：{product['model']}")
    if product.get('description'):
        parts.append(product['description'][:500])
    if product.get('scope'):
        parts.append(f"适用范围：{product['scope'][:200]}")
    return ' '.join(parts)

def compute_text_hash(text: str) -> str:
    """计算文本哈希，用于判断向量是否需要更新"""
    normalized = (text or '').strip()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else ''

def get_product_text_hash(conn: sqlite3.Connection, di_code: str) -> str:
    """根据产品信息生成文本哈希"""
    cursor = conn.cursor()

    # 确保 embeddings 表存在
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        SELECT di_code, product_name, model, description, scope
        FROM products
        WHERE di_code = ?
    ''', (di_code,))
    row = cursor.fetchone()
    if not row:
        return ''
    product = {
        'di_code': row[0],
        'product_name': row[1],
        'model': row[2],
        'description': row[3],
        'scope': row[4]
    }
    text = build_product_text(product)
    return compute_text_hash(text)

# ==========================================
# Step 1: 生成 JSONL 文件
# ==========================================

def generate_jsonl(conn: sqlite3.Connection = None, output_path: str = None, 
                   batch_size: int = 50000, batch_index: int = 0,
                   incremental: bool = True) -> Dict:
    """
    生成 JSONL 文件（支持分批，增量模式）
    
    Args:
        batch_size: 每批产品数量（默认 5 万条，约 15-20MB）
        batch_index: 批次索引（0 开始）
        incremental: 增量模式，只处理新增/变更产品
    
    Returns:
        {'success': bool, 'file_path': str, 'count': int, 'total_batches': int}
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    config = load_config()
    _, _, model = get_api_config(config)
    
    cursor = conn.cursor()

    # 确保 embeddings 表存在
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 统计总数
    cursor.execute('SELECT COUNT(*) FROM products')
    total_products = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
    existing_count = cursor.fetchone()[0]
    
    print(f"[Batch] 产品总数: {total_products}, 已有向量: {existing_count}")
    print(f"[Batch] ========== 步骤 1/5: 分析待处理数据 ==========")
    
    if incremental:
        # 增量模式：新增 + 空向量补齐
        print(f"[Batch] 正在查询新增/缺失向量产品...")
        cursor.execute('''
            SELECT p.di_code, p.product_name, p.model, p.description, p.scope
            FROM products p
            LEFT JOIN embeddings e ON p.di_code = e.di_code
            WHERE e.di_code IS NULL OR e.embedding IS NULL
            ORDER BY p.last_updated ASC
        ''')
        new_products = cursor.fetchall()
        print(f"[Batch] ✓ 新增/缺失向量产品: {len(new_products)} 个")
        
        # 变更产品检测（全量扫描变更，避免固定窗口导致漏处理）
        changed_products = []
        print(f"[Batch] 正在检测变更产品...")
        batch_fetch_size = 100000

        cursor.execute('''
            SELECT p.di_code, p.product_name, p.model, p.description, p.scope, e.text_hash
            FROM products p
            INNER JOIN embeddings e ON p.di_code = e.di_code
            WHERE e.embedding IS NOT NULL
              AND p.last_updated > e.created_at
            ORDER BY p.last_updated ASC
        ''')

        checked = 0
        while True:
            candidate_products = cursor.fetchmany(batch_fetch_size)
            if not candidate_products:
                break
            checked += len(candidate_products)
            for row in candidate_products:
                di_code, product_name, model_val, description, scope, old_hash = row
                product = {
                    'di_code': di_code,
                    'product_name': product_name,
                    'model': model_val,
                    'description': description,
                    'scope': scope
                }
                text = build_product_text(product)
                new_hash = compute_text_hash(text)
                if new_hash != old_hash:
                    changed_products.append((di_code, product_name, model_val, description, scope))
            print(f"[Batch] 已校验变更候选 {checked} 条，确认变更 {len(changed_products)} 条")

        print(f"[Batch] ✓ 确认内容变更: {len(changed_products)} 个")
        
        # 合并需要处理的产品
        all_products = list(new_products) + changed_products
        need_process = len(all_products)
    else:
        # 全量模式
        cursor.execute('''
            SELECT di_code, product_name, model, description, scope 
            FROM products
        ''')
        all_products = cursor.fetchall()
        need_process = len(all_products)
    
    # 计算总批次数
    total_batches = max(1, (need_process + batch_size - 1) // batch_size)
    
    print(f"[Batch] ========== 步骤 2/5: 生成数据文件 ==========")
    print(f"[Batch] 需要处理: {need_process} 个产品")
    
    # 分页获取当前批次
    start_idx = batch_index * batch_size
    end_idx = min(start_idx + batch_size, need_process)
    batch_products = all_products[start_idx:end_idx]
    
    if output_path is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(BATCH_DIR, f'embedding_input_{timestamp}_batch{batch_index}.jsonl')
    
    count = 0
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, row in enumerate(batch_products):
            di_code = row[0]
            
            product = {
                'di_code': di_code,
                'product_name': row[1],
                'model': row[2],
                'description': row[3],
                'scope': row[4]
            }
            
            text = build_product_text(product)
            if not text.strip():
                continue

            text_hash = compute_text_hash(text)
            
            # 构建 JSONL 行
            line = {
                "custom_id": f"{di_code}::{text_hash}" if text_hash else di_code,
                "method": "POST",
                "url": "/v1/embeddings",
                "body": {
                    "model": model,
                    "input": text
                }
            }
            
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
            count += 1
            
            # 每 10000 条显示进度
            if count % 10000 == 0:
                print(f"[Batch] 已生成 {count} / {len(batch_products)} 条...")
    
    file_size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[Batch] ✓ 生成 JSONL 文件完成: {output_path}")
    print(f"[Batch] ✓ 文件大小: {file_size_mb:.1f} MB, 产品数: {count}")
    
    return {
        'success': True,
        'file_path': output_path,
        'count': count,
        'batch_index': batch_index,
        'total_batches': total_batches,
        'total_need_process': need_process
    }

# ==========================================
# Step 2: 上传文件
# ==========================================

@retry_with_backoff(max_retries=3, base_delay=2.0)
def upload_file(file_path: str, config: Dict = None) -> Optional[str]:
    """
    上传文件到阿里云
    
    Returns:
        file_id 或 None
    """
    print(f"[Batch] ========== 步骤 3/5: 上传数据文件 ==========")
    
    api_base, api_key, _ = get_api_config(config)
    
    if not api_base or not api_key:
        print("[Batch] ✗ API 配置不完整，请检查 config.json")
        return None
    
    url = f"{api_base}/files"
    file_size_mb = os.path.getsize(file_path) / 1024 / 1024
    
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    
    with open(file_path, 'rb') as f:
        files = {
            'file': (os.path.basename(file_path), f, 'application/jsonl'),
            'purpose': (None, 'batch')
        }
        
        try:
            print(f"[Batch] 正在上传文件 ({file_size_mb:.1f} MB)...")
            print(f"[Batch] 目标地址: {url}")
            response = requests.post(url, headers=headers, files=files, timeout=300)
            response.raise_for_status()
            
            data = response.json()
            file_id = data.get('id')
            print(f"[Batch] ✓ 上传成功! file_id: {file_id}")
            return file_id
            
        except requests.RequestException as e:
            print(f"[Batch] ✗ 上传失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"[Batch] 响应内容: {e.response.text[:500]}")
            return None

# ==========================================
# Step 3: 创建 Batch 任务
# ==========================================

@retry_with_backoff(max_retries=3, base_delay=2.0)
def create_batch(file_id: str, config: Dict = None) -> Optional[str]:
    """
    创建 Batch 任务
    
    Returns:
        batch_id 或 None
    """
    print(f"[Batch] ========== 步骤 4/5: 创建批处理任务 ==========")
    
    api_base, api_key, _ = get_api_config(config)
    
    url = f"{api_base}/batches"
    
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        "input_file_id": file_id,
        "endpoint": "/v1/embeddings",
        "completion_window": "24h"
    }
    
    try:
        print(f"[Batch] 正在创建任务...")
        print(f"[Batch] file_id: {file_id}")
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        
        data = response.json()
        batch_id = data.get('id')
        status = data.get('status', 'unknown')
        print(f"[Batch] ✓ 任务创建成功!")
        print(f"[Batch]   batch_id: {batch_id}")
        print(f"[Batch]   状态: {status}")
        print(f"[Batch] ========== 步骤 5/5: 等待后台处理 ==========")
        print(f"[Batch] 任务已提交到云端，正在后台处理...")
        print(f"[Batch] 预计处理时间: 10-30 分钟（取决于数据量）")
        print(f"[Batch] 可以在管理后台查看进度，完成后点击'导入结果'")
        return batch_id
        
    except requests.RequestException as e:
        print(f"[Batch] ✗ 创建任务失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[Batch] 响应内容: {e.response.text[:500]}")
        return None

# ==========================================
# Step 4: 查询任务状态
# ==========================================

def get_pipeline_state() -> Dict:
    """从SQLite读取流水线状态"""
    return load_pipeline_state()

def set_pipeline_state(**kwargs):
    """保存流水线状态到SQLite"""
    state = load_pipeline_state()
    state.update(kwargs)
    save_pipeline_state(state)

# ==========================================
# 批处理任务记录（用于智能检测）
# ==========================================

BATCH_TASK_DB = os.path.join(os.path.dirname(__file__), 'batch_tasks.db')

def _init_batch_task_db():
    """初始化批处理任务记录表"""
    conn = db_connect(BATCH_TASK_DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS batch_tasks (
            batch_id TEXT PRIMARY KEY,
            count INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT,
            imported_at TEXT,
            imported_count INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_batch_task(batch_id: str, count: int):
    """保存批处理任务记录"""
    _init_batch_task_db()
    conn = db_connect(BATCH_TASK_DB)
    conn.execute('''
        INSERT OR REPLACE INTO batch_tasks (batch_id, count, status, created_at)
        VALUES (?, ?, 'pending', ?)
    ''', (batch_id, count, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print(f"[Batch] 已记录任务: {batch_id}, 数量: {count}")

def get_pending_batch_tasks(max_age_hours: Optional[int] = None) -> List[Dict]:
    """
    获取待导入的批处理任务（未导入且在指定时间内创建的）
    
    Returns:
        [{'batch_id': str, 'count': int, 'created_at': str, 'status': str}, ...]
    """
    _init_batch_task_db()
    conn = db_connect(BATCH_TASK_DB)
    cursor = conn.cursor()
    
    if max_age_hours is None:
        cursor.execute('''
            SELECT batch_id, count, status, created_at
            FROM batch_tasks
            WHERE imported_at IS NULL
            ORDER BY created_at DESC
        ''')
    else:
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        cursor.execute('''
            SELECT batch_id, count, status, created_at
            FROM batch_tasks
            WHERE imported_at IS NULL
              AND created_at > ?
            ORDER BY created_at DESC
        ''', (cutoff,))
    
    tasks = []
    for row in cursor.fetchall():
        tasks.append({
            'batch_id': row[0],
            'count': row[1],
            'status': row[2],
            'created_at': row[3]
        })
    
    conn.close()
    return tasks

def mark_batch_imported(batch_id: str, imported_count: int):
    """标记批处理任务已导入"""
    _init_batch_task_db()
    conn = db_connect(BATCH_TASK_DB)
    conn.execute('''
        UPDATE batch_tasks 
        SET status = 'imported', imported_at = ?, imported_count = ?
        WHERE batch_id = ?
    ''', (datetime.now().isoformat(), imported_count, batch_id))
    conn.commit()
    conn.close()
    print(f"[Batch] 已标记任务完成: {batch_id}, 导入数量: {imported_count}")

def check_and_import_completed_tasks(conn: sqlite3.Connection = None) -> Dict:
    """
    检查并导入所有已完成的批处理任务
    
    Returns:
        {'found': int, 'imported': int, 'tasks': [...]}
    """
    print(f"[Batch] ========== 检查待导入任务 ==========")
    
    pending_tasks = get_pending_batch_tasks(max_age_hours=None)
    print(f"[Batch] 找到 {len(pending_tasks)} 个待处理任务")
    
    if not pending_tasks:
        return {'found': 0, 'imported': 0, 'tasks': []}
    
    results = []
    total_imported = 0
    
    for task in pending_tasks:
        batch_id = task['batch_id']
        print(f"[Batch] 检查任务: {batch_id} (创建于 {task['created_at']})")
        
        # 检查状态
        status = check_batch_status(batch_id)
        task['api_status'] = status.get('status', 'unknown')
        
        if status.get('status') == 'completed':
            print(f"[Batch] ✓ 任务已完成，开始导入...")
            
            # 下载并导入
            result_file = download_results(batch_id)
            if result_file:
                import_result = import_results(result_file, conn)
                imported = import_result.get('imported', 0)
                total_imported += imported
                
                task['imported'] = imported
                task['failed'] = import_result.get('failed', 0)
                task['success'] = bool(import_result.get('success'))
                if task['success']:
                    mark_batch_imported(batch_id, imported)
                else:
                    task['error'] = import_result.get('error') or '导入结果失败或无有效记录'
            else:
                task['success'] = False
                task['error'] = '下载结果失败'
        elif status.get('status') in ('validating', 'in_progress'):
            print(f"[Batch] ⏳ 任务进行中: {status.get('status')}")
            task['success'] = None  # 进行中
        else:
            print(f"[Batch] ✗ 任务状态异常: {status.get('status')}")
            task['success'] = False
            task['error'] = f"状态: {status.get('status')}"
        
        results.append(task)
    
    return {
        'found': len(pending_tasks),
        'imported': total_imported,
        'tasks': results
    }

def get_batch_status(batch_id: str, config: Dict = None) -> Dict:
    """
    查询 Batch 任务状态（OpenAI 兼容 API）
    
    Returns:
        {'status': str, 'output_file_id': str, ...}
    """
    api_base, api_key, _ = get_api_config(config)
    
    url = f"{api_base}/batches/{batch_id}"
    
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
        
    except requests.RequestException as e:
        print(f"[Batch] 查询状态失败: {e}")
        return {}

@retry_with_backoff(max_retries=5, base_delay=5.0)
def check_batch_status(batch_id: str, config: Dict = None) -> Dict:
    """
    检查批处理任务状态（兼容多种 API）
    
    Returns:
        统一格式: {'status': str, 'completed': int, 'total': int, 'output_file_id': str, ...}
    """
    api_base, api_key, _ = get_api_config(config)
    
    if not api_base or not api_key:
        return {'status': 'error', 'error': 'API 配置不完整'}
    
    # 判断是 DashScope 原生 API 还是 OpenAI 兼容 API
    # compatible-mode 使用 OpenAI 兼容格式
    if 'dashscope' in api_base and 'compatible-mode' not in api_base:
        # DashScope 异步任务 API
        dashscope_origin = _url_origin(api_base)
        url = f"{dashscope_origin}/api/v1/tasks/{batch_id}"
        headers = {'Authorization': f'Bearer {api_key}'}
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            output = data.get('output', {})
            task_status = output.get('task_status', 'UNKNOWN')
            
            # 映射 DashScope 状态到统一格式
            status_map = {
                'PENDING': 'validating',
                'RUNNING': 'in_progress', 
                'SUCCEEDED': 'completed',
                'FAILED': 'failed',
                'CANCELED': 'cancelled',
                'UNKNOWN': 'unknown'
            }
            
            result = {
                'status': status_map.get(task_status, 'unknown'),
                'raw_status': task_status,
                'output_url': output.get('url'),  # DashScope 返回的是 URL
                'submit_time': output.get('submit_time'),
                'end_time': output.get('end_time'),
                'usage': data.get('usage', {})
            }
            
            if task_status == 'SUCCEEDED':
                result['completed'] = result['usage'].get('total_tokens', 0)
                result['total'] = result['completed']
            
            print(f"[Batch] DashScope 状态: {task_status}")
            return result
            
        except requests.RequestException as e:
            print(f"[Batch] 查询 DashScope 状态失败: {e}")
            return {'status': 'error', 'error': str(e)}
    else:
        # OpenAI 兼容 Batch API
        url = f"{api_base}/batches/{batch_id}"
        headers = {'Authorization': f'Bearer {api_key}'}
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            counts = data.get('request_counts', {})
            result = {
                'status': data.get('status', 'unknown'),
                'completed': counts.get('completed', 0),
                'total': counts.get('total', 0),
                'failed': counts.get('failed', 0),
                'output_file_id': data.get('output_file_id'),
                'error_file_id': data.get('error_file_id')
            }
            
            print(f"[Batch] OpenAI 状态: {result['status']}, {result['completed']}/{result['total']}")
            return result
            
        except requests.RequestException as e:
            print(f"[Batch] 查询状态失败: {e}")
            return {'status': 'error', 'error': str(e)}

def wait_for_completion(batch_id: str, config: Dict = None, 
                        check_interval: int = 30, max_wait: int = 3600) -> Dict:
    """
    等待任务完成
    
    Args:
        batch_id: 任务 ID
        check_interval: 检查间隔（秒）
        max_wait: 最大等待时间（秒）
    
    Returns:
        最终状态
    """
    start_time = time.time()
    
    while True:
        status = check_batch_status(batch_id, config)
        current_status = status.get('status', 'unknown')
        
        print(f"[Batch] 状态: {current_status}, 已等待: {int(time.time() - start_time)}s")
        
        if current_status == 'completed':
            return status
        elif current_status in ['failed', 'expired', 'cancelled', 'error']:
            print(f"[Batch] 任务失败: {status}")
            return status
        
        if time.time() - start_time > max_wait:
            print(f"[Batch] 等待超时")
            return status
        
        time.sleep(check_interval)

# ==========================================
# Step 5: 下载结果
# ==========================================

@retry_with_backoff(max_retries=3, base_delay=2.0)
def download_results(batch_id: str, config: Dict = None) -> Optional[str]:
    """
    根据 batch_id 下载批处理结果
    
    Returns:
        本地文件路径
    """
    print(f"[Batch] ========== 下载批处理结果 ==========")
    print(f"[Batch] batch_id: {batch_id}")
    
    # 先查询状态获取 output_file_id
    status = check_batch_status(batch_id, config)
    
    if status.get('status') == 'error':
        print(f"[Batch] ✗ 查询状态失败: {status.get('error')}")
        return None
    
    if status.get('status') != 'completed':
        print(f"[Batch] ✗ 任务未完成，当前状态: {status.get('status')}")
        return None
    
    output_url = status.get('output_url')
    if output_url:
        print(f"[Batch] 检测到 output_url，直接下载: {output_url}")
        return download_result_url(output_url, batch_id)

    output_file_id = status.get('output_file_id')
    if not output_file_id:
        print(f"[Batch] ✗ 未找到结果文件 ID 或 output_url")
        return None
    
    print(f"[Batch] output_file_id: {output_file_id}")
    return download_result(output_file_id, config)


def download_result_url(url: str, batch_id: str) -> Optional[str]:
    """通过直链下载批处理结果（DashScope 原生模式）"""
    try:
        local_path = os.path.join(BATCH_DIR, f"result_{batch_id}.jsonl")
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        with open(local_path, 'wb') as f:
            f.write(response.content)
        print(f"[Batch] ✓ 已下载结果文件: {local_path}")
        return local_path
    except requests.RequestException as e:
        print(f"[Batch] 下载 output_url 失败: {e}")
        return None

def download_result(file_id: str, config: Dict = None) -> Optional[str]:
    """
    下载结果文件（通过 file_id）
    
    Returns:
        本地文件路径
    """
    api_base, api_key, _ = get_api_config(config)
    
    url = f"{api_base}/files/{file_id}/content"
    
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    
    try:
        print(f"[Batch] 正在下载结果文件...")
        print(f"[Batch] URL: {url}")
        response = requests.get(url, headers=headers, timeout=300)
        response.raise_for_status()
        
        # 保存到本地
        output_path = os.path.join(BATCH_DIR, f'embedding_output_{file_id}.jsonl')
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        file_size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"[Batch] ✓ 下载完成: {output_path}")
        print(f"[Batch] ✓ 文件大小: {file_size_mb:.1f} MB")
        return output_path
        
    except requests.RequestException as e:
        print(f"[Batch] ✗ 下载失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[Batch] 响应: {e.response.text[:500]}")
        return None

# ==========================================
# Step 6: 解析结果并存入数据库
# ==========================================

def vector_to_blob(vector: List[float]) -> bytes:
    """将向量转为二进制"""
    return np.array(vector, dtype=np.float32).tobytes()

def import_results(result_file: str, conn: sqlite3.Connection = None) -> Dict:
    """
    解析结果文件并存入数据库
    
    Returns:
        {'success': bool, 'imported': int, 'failed': int}
    """
    print(f"[Batch] ========== 导入结果到数据库 ==========")
    print(f"[Batch] 结果文件: {result_file}")
    
    if conn is None:
        conn = db_connect(DB_PATH)
    
    cursor = conn.cursor()
    
    # 确保表存在
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    imported = 0
    failed = 0
    total_lines = 0
    imported_di_codes = []
    
    # 先统计总行数
    with open(result_file, 'r', encoding='utf-8') as f:
        total_lines = sum(1 for _ in f)
    print(f"[Batch] 结果文件共 {total_lines} 行")
    
    with open(result_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            try:
                data = json.loads(line.strip())
                
                custom_id = data.get('custom_id')  # 可能包含文本哈希
                response = data.get('response', {})
                body = response.get('body', {})
                
                # 检查是否成功
                if response.get('status_code') != 200:
                    if failed < 5:  # 只打印前5个错误
                        print(f"[Batch] 产品 {custom_id} 处理失败: {response}")
                    failed += 1
                    continue
                
                # 提取向量
                embeddings_data = body.get('data', [])
                if not embeddings_data:
                    failed += 1
                    continue
                
                embedding = embeddings_data[0].get('embedding')
                if not embedding:
                    failed += 1
                    continue
                
                # 解析 di_code 与 text_hash
                di_code = custom_id
                text_hash = ''
                if isinstance(custom_id, str) and '::' in custom_id:
                    di_code, text_hash = custom_id.split('::', 1)

                if not text_hash:
                    text_hash = get_product_text_hash(conn, di_code)

                # 存入数据库
                blob = vector_to_blob(embedding)
                cursor.execute('''
                    INSERT OR REPLACE INTO embeddings (di_code, embedding, text_hash)
                    VALUES (?, ?, ?)
                ''', (di_code, blob, text_hash))
                
                imported += 1
                imported_di_codes.append(di_code)
                
                if imported % 5000 == 0:
                    progress = (line_num / total_lines * 100) if total_lines > 0 else 0
                    print(f"[Batch] 进度: {progress:.1f}% ({imported} 已导入, {failed} 失败)")
                    conn.commit()
                
            except Exception as e:
                if failed < 5:
                    print(f"[Batch] 解析行 {line_num} 失败: {e}")
                failed += 1
    
    conn.commit()
    print(f"[Batch] ========== 导入完成 ==========")
    print(f"[Batch] ✓ 成功导入: {imported} 条")
    print(f"[Batch] ✗ 失败: {failed} 条")
    
    if imported > 0 and failed == 0:
        success = True
        error_msg = None
    elif imported > 0 and failed > 0:
        success = False
        error_msg = 'partial import'
    elif imported == 0 and failed > 0:
        success = False
        error_msg = 'all records failed'
    else:
        success = False
        error_msg = 'empty result file'
    return {
        'success': success,
        'partial': imported > 0 and failed > 0,
        'imported': imported,
        'failed': failed,
        'imported_di_codes': imported_di_codes,
        'error': error_msg
    }

# ==========================================
# 流水线状态管理（SQLite）
# ==========================================

def _get_pipeline_conn() -> sqlite3.Connection:
    conn = db_connect(PIPELINE_STATE_DB, timeout=30, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn

def _init_pipeline_state_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pipeline_batches (
            batch_index TEXT PRIMARY KEY,
            file_path TEXT,
            file_id TEXT,
            batch_id TEXT,
            status TEXT,
            output_file_id TEXT,
            output_url TEXT,
            result_path TEXT,
            count INTEGER,
            imported_count INTEGER,
            error TEXT,
            updated_at TEXT
        )
    ''')
    try:
        cursor.execute("ALTER TABLE pipeline_batches ADD COLUMN output_url TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()

def load_pipeline_state() -> Dict:
    """加载流水线状态"""
    conn = _get_pipeline_conn()
    _init_pipeline_state_db(conn)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM pipeline_batches')
    has_batches = cursor.fetchone()[0] > 0
    cursor.execute('SELECT key, value FROM pipeline_meta')
    meta = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute('SELECT * FROM pipeline_batches')
    columns = [desc[0] for desc in cursor.description]
    batches = {}
    for row in cursor.fetchall():
        item = dict(zip(columns, row))
        batch_index = str(item.pop('batch_index'))
        if item.get('error'):
            try:
                item['error'] = json.loads(item['error'])
            except Exception:
                pass
        batches[batch_index] = item

    conn.close()
    state = {
        'batches': batches,
        'total_batches': int(meta.get('total_batches') or 0),
        'batch_size': int(meta.get('batch_size') or 50000),
        'created_at': meta.get('created_at'),
        'updated_at': meta.get('updated_at')
    }

    if not has_batches and os.path.exists(LEGACY_PIPELINE_STATE_FILE):
        try:
            with open(LEGACY_PIPELINE_STATE_FILE, 'r', encoding='utf-8') as f:
                legacy_state = json.load(f)
            save_pipeline_state(legacy_state)
            state = legacy_state
        except Exception as e:
            print(f"[Pipeline] 迁移旧状态失败: {e}")

    return reconcile_pipeline_state(state)

def save_pipeline_state(state: Dict):
    """保存流水线状态"""
    conn = _get_pipeline_conn()
    _init_pipeline_state_db(conn)
    cursor = conn.cursor()
    state['updated_at'] = datetime.now().isoformat()

    meta = {
        'total_batches': state.get('total_batches', 0),
        'batch_size': state.get('batch_size', 50000),
        'created_at': state.get('created_at'),
        'updated_at': state.get('updated_at')
    }
    for key, value in meta.items():
        cursor.execute(
            'INSERT OR REPLACE INTO pipeline_meta (key, value) VALUES (?, ?)',
            (key, '' if value is None else str(value))
        )

    for batch_index, batch in state.get('batches', {}).items():
        cursor.execute('''
            INSERT OR REPLACE INTO pipeline_batches (
                batch_index, file_path, file_id, batch_id, status, output_file_id,
                output_url, result_path, count, imported_count, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            str(batch_index),
            batch.get('file_path'),
            batch.get('file_id'),
            batch.get('batch_id'),
            batch.get('status'),
            batch.get('output_file_id'),
            batch.get('output_url'),
            batch.get('result_path'),
            batch.get('count'),
            batch.get('imported_count'),
            json.dumps(batch.get('error'), ensure_ascii=False) if batch.get('error') else None,
            state['updated_at']
        ))

    conn.commit()
    conn.close()


def reconcile_pipeline_state(state: Dict) -> Dict:
    """
    修复流水线状态文件中的悬空 result_path，并保持 imported 状态干净。
    """
    changed = False
    for _, batch in state.get('batches', {}).items():
        status = batch.get('status')
        result_path = batch.get('result_path')
        if status == 'imported' and result_path:
            batch['result_path'] = None
            changed = True
            continue
        if result_path and not os.path.exists(result_path):
            if status in ('downloaded', 'completed'):
                batch['result_path'] = None
                changed = True
    if changed:
        save_pipeline_state(state)
    return state

# ==========================================
# 流水线操作
# ==========================================

def pipeline_generate_all(conn: sqlite3.Connection = None, batch_size: int = 50000) -> Dict:
    """
    生成所有批次的 JSONL 文件
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    state = load_pipeline_state()
    state['batch_size'] = batch_size
    state['created_at'] = datetime.now().isoformat()
    
    # 计算总批次数
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('SELECT COUNT(*) FROM products')
    total_products = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
    existing_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL AND (text_hash IS NULL OR text_hash = '')")
    stale_count = cursor.fetchone()[0]

    remaining_est = max(0, total_products - existing_count) + stale_count
    total_batches = (total_products + batch_size - 1) // batch_size
    state['total_batches'] = total_batches
    
    print(f"[Pipeline] 产品总数: {total_products}, 已有向量: {existing_count}")
    print(f"[Pipeline] 待处理(估算): {remaining_est}, 总批次: {total_batches}")
    
    generated = 0
    for i in range(total_batches):
        if str(i) in state['batches'] and state['batches'][str(i)].get('file_path'):
            print(f"[Pipeline] 批次 {i} 已生成，跳过")
            continue
        
        result = generate_jsonl(conn, batch_size=batch_size, batch_index=i)
        if result['success'] and result['count'] > 0:
            state['batches'][str(i)] = {
                'file_path': result['file_path'],
                'file_id': None,
                'batch_id': None,
                'status': 'generated',
                'output_file_id': None,
                'output_url': None,
                'count': result['count']
            }
            generated += 1
            save_pipeline_state(state)
        
        if result['count'] == 0:
            continue
    
    print(f"[Pipeline] 生成完成，共 {generated} 个批次")
    return {'success': True, 'generated': generated, 'total_batches': total_batches}

def pipeline_upload_all(config: Dict = None, qps_limit: float = 3.0) -> Dict:
    """
    上传所有待上传的批次（控制 QPS）
    
    Args:
        qps_limit: 每秒最大请求数（阿里云限制 3 QPS）
    """
    if config is None:
        config = load_config()
    
    state = load_pipeline_state()
    uploaded = 0
    interval = 1.0 / qps_limit  # 请求间隔
    
    # 按批次索引排序
    batch_indices = sorted(state['batches'].keys(), key=lambda x: int(x))
    
    for batch_idx in batch_indices:
        batch_info = state['batches'][batch_idx]
        
        if batch_info.get('file_id'):
            print(f"[Pipeline] 批次 {batch_idx} 已上传，跳过")
            continue
        
        if not batch_info.get('file_path'):
            continue
        
        file_id = upload_file(batch_info['file_path'], config)
        if file_id:
            state['batches'][batch_idx]['file_id'] = file_id
            state['batches'][batch_idx]['status'] = 'uploaded'
            uploaded += 1
            save_pipeline_state(state)
            print(f"[Pipeline] 批次 {batch_idx} 上传成功: {file_id}")
        else:
            print(f"[Pipeline] 批次 {batch_idx} 上传失败")
        
        # QPS 限制
        time.sleep(interval)
    
    print(f"[Pipeline] 上传完成，共 {uploaded} 个批次")
    return {'success': True, 'uploaded': uploaded}

def pipeline_create_all(config: Dict = None, qps_limit: float = 3.0) -> Dict:
    """
    为所有已上传的批次创建任务（控制 QPS）
    
    Args:
        qps_limit: 每秒最大请求数（阿里云限制 3 QPS）
    """
    if config is None:
        config = load_config()
    
    state = load_pipeline_state()
    created = 0
    interval = 1.0 / qps_limit  # 请求间隔
    
    # 按批次索引排序
    batch_indices = sorted(state['batches'].keys(), key=lambda x: int(x))
    
    for batch_idx in batch_indices:
        batch_info = state['batches'][batch_idx]
        
        if batch_info.get('batch_id'):
            print(f"[Pipeline] 批次 {batch_idx} 任务已创建，跳过")
            continue
        
        if not batch_info.get('file_id'):
            continue
        
        batch_id = create_batch(batch_info['file_id'], config)
        if batch_id:
            state['batches'][batch_idx]['batch_id'] = batch_id
            state['batches'][batch_idx]['status'] = 'processing'
            created += 1
            save_pipeline_state(state)
            print(f"[Pipeline] 批次 {batch_idx} 任务创建成功: {batch_id}")
        else:
            print(f"[Pipeline] 批次 {batch_idx} 任务创建失败")
        
        # QPS 限制
        time.sleep(interval)
    
    print(f"[Pipeline] 任务创建完成，共 {created} 个")
    return {'success': True, 'created': created}

def pipeline_check_status(config: Dict = None) -> Dict:
    """
    检查所有任务状态
    """
    if config is None:
        config = load_config()
    
    state = load_pipeline_state()
    
    stats = {
        'total': len(state['batches']),
        'generated': 0,
        'uploaded': 0,
        'processing': 0,
        'completed': 0,
        'failed': 0,
        'imported': 0
    }
    
    for batch_idx, batch_info in state['batches'].items():
        batch_id = batch_info.get('batch_id')
        current_status = batch_info.get('status', 'unknown')
        
        # 已导入的跳过
        if current_status == 'imported':
            stats['imported'] += 1
            continue
        
        # 没有 batch_id 的统计原状态
        if not batch_id:
            if current_status == 'generated':
                stats['generated'] += 1
            elif current_status == 'uploaded':
                stats['uploaded'] += 1
            continue
        
        # 查询远程状态
        remote_status = check_batch_status(batch_id, config)
        new_status = remote_status.get('status', 'unknown')
        
        # 更新本地状态
        if new_status == 'completed':
            state['batches'][batch_idx]['status'] = 'completed'
            state['batches'][batch_idx]['output_file_id'] = remote_status.get('output_file_id')
            state['batches'][batch_idx]['output_url'] = remote_status.get('output_url')
            stats['completed'] += 1
        elif new_status in ['failed', 'expired', 'cancelled', 'error']:
            state['batches'][batch_idx]['status'] = 'failed'
            state['batches'][batch_idx]['error'] = remote_status.get('errors') or remote_status.get('error')
            stats['failed'] += 1
        else:
            state['batches'][batch_idx]['status'] = new_status
            stats['processing'] += 1
        
        # 显示进度
        request_counts = remote_status.get('request_counts', {})
        completed = request_counts.get('completed', 0)
        total = request_counts.get('total', 0)
        print(f"[Pipeline] 批次 {batch_idx}: {new_status} ({completed}/{total})")
    
    save_pipeline_state(state)
    
    print(f"\n[Pipeline] 状态汇总:")
    print(f"  - 总批次: {stats['total']}")
    print(f"  - 已生成: {stats['generated']}")
    print(f"  - 已上传: {stats['uploaded']}")
    print(f"  - 处理中: {stats['processing']}")
    print(f"  - 已完成: {stats['completed']}")
    print(f"  - 已导入: {stats['imported']}")
    print(f"  - 失败: {stats['failed']}")
    
    return {'success': True, 'stats': stats}

def pipeline_download_and_import_one(conn: sqlite3.Connection = None, config: Dict = None) -> Dict:
    """
    下载并导入一个批次（节省磁盘空间）
    
    Returns:
        {'success': bool, 'batch_idx': str, 'imported': int}
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    if config is None:
        config = load_config()
    
    state = load_pipeline_state()
    
    # 找到第一个已完成但未导入的批次
    batch_indices = sorted(state['batches'].keys(), key=lambda x: int(x))
    
    for batch_idx in batch_indices:
        batch_info = state['batches'][batch_idx]
        
        if batch_info.get('status') != 'completed':
            continue
        
        output_file_id = batch_info.get('output_file_id')
        output_url = batch_info.get('output_url')
        if not output_file_id and not output_url:
            continue
        
        # 下载
        print(f"[Pipeline] 下载批次 {batch_idx}...")
        if output_url:
            result_path = download_result_url(output_url, batch_info.get('batch_id') or batch_idx)
        else:
            result_path = download_result(output_file_id, config)
        if not result_path:
            return {'success': False, 'error': f'下载批次 {batch_idx} 失败'}
        
        state['batches'][batch_idx]['result_path'] = result_path
        save_pipeline_state(state)
        
        # 导入
        print(f"[Pipeline] 导入批次 {batch_idx}...")
        result = import_results(result_path, conn)

        imported_count = int(result.get('imported', 0))
        is_success = bool(result.get('success')) and imported_count > 0
        if is_success:
            state['batches'][batch_idx]['status'] = 'imported'
            state['batches'][batch_idx]['imported_count'] = imported_count
            state['batches'][batch_idx]['result_path'] = None
            save_pipeline_state(state)

            # 清理文件
            if os.path.exists(result_path):
                os.remove(result_path)
                print(f"[Pipeline] 已删除 output 文件")

            input_path = batch_info.get('file_path')
            if input_path and os.path.exists(input_path):
                os.remove(input_path)
                print(f"[Pipeline] 已删除 input 文件")

            return {
                'success': True,
                'batch_idx': batch_idx,
                'imported': imported_count
            }

        # 导入失败或零成功记录，保留结果文件以便排查/重试
        state['batches'][batch_idx]['status'] = 'failed'
        state['batches'][batch_idx]['error'] = {
            'message': '导入失败或无有效记录',
            'imported': imported_count,
            'failed': int(result.get('failed', 0))
        }
        save_pipeline_state(state)
        return {
            'success': False,
            'batch_idx': batch_idx,
            'imported': imported_count,
            'failed': int(result.get('failed', 0)),
            'error': '导入失败或无有效记录'
        }
    
    return {'success': True, 'batch_idx': None, 'imported': 0, 'message': '没有待处理的批次'}

def pipeline_download_all(config: Dict = None) -> Dict:
    """
    下载所有已完成的结果
    """
    if config is None:
        config = load_config()
    
    state = load_pipeline_state()
    downloaded = 0
    
    for batch_idx, batch_info in state['batches'].items():
        if batch_info.get('status') != 'completed':
            continue
        
        if batch_info.get('result_path'):
            print(f"[Pipeline] 批次 {batch_idx} 已下载，跳过")
            continue
        
        output_file_id = batch_info.get('output_file_id')
        output_url = batch_info.get('output_url')
        if not output_file_id and not output_url:
            continue
        
        if output_url:
            result_path = download_result_url(output_url, batch_info.get('batch_id') or batch_idx)
        else:
            result_path = download_result(output_file_id, config)
        if result_path:
            state['batches'][batch_idx]['result_path'] = result_path
            downloaded += 1
            save_pipeline_state(state)
            print(f"[Pipeline] 批次 {batch_idx} 下载成功: {result_path}")
    
    print(f"[Pipeline] 下载完成，共 {downloaded} 个批次")
    return {'success': True, 'downloaded': downloaded}

def pipeline_import_all(conn: sqlite3.Connection = None, cleanup: bool = True) -> Dict:
    """
    导入所有已下载的结果到数据库
    
    Args:
        cleanup: 导入成功后是否删除源文件（节省磁盘空间）
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    state = load_pipeline_state()
    total_imported = 0
    total_failed = 0
    
    # 按批次索引排序
    batch_indices = sorted(state['batches'].keys(), key=lambda x: int(x))
    
    for batch_idx in batch_indices:
        batch_info = state['batches'][batch_idx]
        
        if batch_info.get('status') == 'imported':
            continue
        
        result_path = batch_info.get('result_path')
        if not result_path or not os.path.exists(result_path):
            continue
        
        print(f"[Pipeline] 导入批次 {batch_idx}: {result_path}")
        result = import_results(result_path, conn)
        
        imported_count = int(result.get('imported', 0))
        failed_count = int(result.get('failed', 0))
        total_imported += imported_count
        total_failed += failed_count

        is_success = bool(result.get('success')) and imported_count > 0
        if is_success:
            state['batches'][batch_idx]['status'] = 'imported'
            state['batches'][batch_idx]['imported_count'] = imported_count
            state['batches'][batch_idx]['result_path'] = None
        else:
            state['batches'][batch_idx]['status'] = 'failed'
            state['batches'][batch_idx]['error'] = {
                'message': '导入失败或无有效记录',
                'imported': imported_count,
                'failed': failed_count
            }
        save_pipeline_state(state)
        
        # 清理文件节省空间（仅成功导入时清理）
        if cleanup and is_success:
            # 删除 output 文件
            if os.path.exists(result_path):
                os.remove(result_path)
                print(f"[Pipeline] 已删除 output 文件: {result_path}")
            
            # 删除对应的 input 文件
            input_path = batch_info.get('file_path')
            if input_path and os.path.exists(input_path):
                os.remove(input_path)
                print(f"[Pipeline] 已删除 input 文件: {input_path}")
    
    print(f"[Pipeline] 导入完成，成功: {total_imported}, 失败: {total_failed}")
    return {'success': True, 'imported': total_imported, 'failed': total_failed}

def pipeline_run_full(conn: sqlite3.Connection = None, batch_size: int = 50000,
                      check_interval: int = 60) -> Dict:
    """
    运行完整流水线（自动循环直到全部完成）
    """
    if conn is None:
        conn = db_connect(DB_PATH)
    
    config = load_config()
    
    print("\n" + "="*60)
    print("开始批量 Embedding 流水线")
    print("="*60)
    
    # Step 1: 生成所有 JSONL
    print("\n>>> Step 1: 生成 JSONL 文件")
    pipeline_generate_all(conn, batch_size)
    
    # Step 2: 上传所有文件
    print("\n>>> Step 2: 上传文件")
    pipeline_upload_all(config)
    
    # Step 3: 创建所有任务
    print("\n>>> Step 3: 创建 Batch 任务")
    pipeline_create_all(config)
    
    # Step 4: 循环检查状态、下载、导入
    print("\n>>> Step 4: 等待完成并处理结果")
    while True:
        status_result = pipeline_check_status(config)
        stats = status_result['stats']
        
        # 下载已完成的
        if stats['completed'] > 0:
            pipeline_download_all(config)
            pipeline_import_all(conn)
        
        # 检查是否全部完成
        pending = stats['processing'] + stats['completed']
        if pending == 0:
            break
        
        print(f"\n[Pipeline] 等待 {check_interval} 秒后再次检查...")
        time.sleep(check_interval)
    
    # 最终统计
    state = load_pipeline_state()
    total_imported = sum(b.get('imported_count', 0) for b in state['batches'].values())
    
    print("\n" + "="*60)
    print(f"流水线完成！共导入 {total_imported} 条向量")
    print("="*60)
    
    return {'success': True, 'total_imported': total_imported}

# ==========================================
# 一键执行（保留旧接口）
# ==========================================

def run_batch_embedding(conn: sqlite3.Connection = None) -> Dict:
    """一键执行批量向量化（使用流水线）"""
    return pipeline_run_full(conn)

# ==========================================
# 命令行入口
# ==========================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='UDID 向量批量处理')
    
    # 单步操作
    parser.add_argument('--generate', action='store_true', help='生成 JSONL 文件（单批）')
    parser.add_argument('--upload', type=str, help='上传指定的 JSONL 文件')
    parser.add_argument('--create', type=str, help='使用 file_id 创建任务')
    parser.add_argument('--status', type=str, help='查询 batch_id 状态')
    parser.add_argument('--download', type=str, help='下载 file_id 的结果')
    parser.add_argument('--import-file', type=str, help='导入结果文件到数据库')
    
    # 流水线操作
    parser.add_argument('--pipeline-generate', action='store_true', help='[流水线] 生成所有批次 JSONL')
    parser.add_argument('--pipeline-upload', action='store_true', help='[流水线] 上传所有待上传批次')
    parser.add_argument('--pipeline-create', action='store_true', help='[流水线] 创建所有待创建任务')
    parser.add_argument('--pipeline-status', action='store_true', help='[流水线] 检查所有任务状态')
    parser.add_argument('--pipeline-download', action='store_true', help='[流水线] 下载所有已完成结果')
    parser.add_argument('--pipeline-import', action='store_true', help='[流水线] 导入所有已下载结果')
    parser.add_argument('--pipeline-run', action='store_true', help='[流水线] 运行完整流水线（自动循环）')
    parser.add_argument('--pipeline-reset', action='store_true', help='[流水线] 重置流水线状态')
    
    # 参数
    parser.add_argument('--batch-size', type=int, default=50000, help='每批产品数量（默认 50000）')
    parser.add_argument('--batch-index', type=int, default=0, help='批次索引（用于 --generate）')
    
    # 旧接口
    parser.add_argument('--run', action='store_true', help='一键执行全流程（使用流水线）')
    
    args = parser.parse_args()
    
    conn = db_connect(DB_PATH)
    config = load_config()
    
    # 单步操作
    if args.generate:
        result = generate_jsonl(conn, batch_size=args.batch_size, batch_index=args.batch_index)
        print(f"结果: {result}")
    
    elif args.upload:
        file_id = upload_file(args.upload, config)
        print(f"file_id: {file_id}")
    
    elif args.create:
        batch_id = create_batch(args.create, config)
        print(f"batch_id: {batch_id}")
    
    elif args.status:
        status = check_batch_status(args.status, config)
        print(f"状态: {json.dumps(status, indent=2, ensure_ascii=False)}")
    
    elif args.download:
        path = download_result(args.download, config)
        print(f"下载到: {path}")
    
    elif args.import_file:
        result = import_results(args.import_file, conn)
        print(f"结果: {result}")
    
    # 流水线操作
    elif args.pipeline_generate:
        result = pipeline_generate_all(conn, args.batch_size)
        print(f"结果: {result}")
    
    elif args.pipeline_upload:
        result = pipeline_upload_all(config)
        print(f"结果: {result}")
    
    elif args.pipeline_create:
        result = pipeline_create_all(config)
        print(f"结果: {result}")
    
    elif args.pipeline_status:
        result = pipeline_check_status(config)
        print(f"结果: {result}")
    
    elif args.pipeline_download:
        result = pipeline_download_all(config)
        print(f"结果: {result}")
    
    elif args.pipeline_import:
        result = pipeline_import_all(conn)
        print(f"结果: {result}")
    
    elif args.pipeline_run:
        result = pipeline_run_full(conn, args.batch_size)
        print(f"结果: {result}")
    
    elif args.pipeline_reset:
        removed_files = []
        for state_file in (PIPELINE_STATE_DB, LEGACY_PIPELINE_STATE_FILE):
            if os.path.exists(state_file):
                os.remove(state_file)
                removed_files.append(state_file)
        if removed_files:
            print(f"流水线状态已重置: {removed_files}")
        else:
            print("没有需要重置的流水线状态文件")
    
    elif args.run:
        result = run_batch_embedding(conn)
        print(f"\n最终结果: {result}")
    
    else:
        parser.print_help()

    conn.close()


# ==========================================
# 向量更新队列处理（模块间通信）
# ==========================================

def process_embedding_queue(conn: sqlite3.Connection = None, batch_size: int = 1000) -> Dict:
    """
    处理向量更新队列

    从 embedding_update_queue 表中读取待处理记录，
    批量生成向量并更新到 embeddings 表。

    Args:
        conn: 数据库连接，None 则自动创建
        batch_size: 每批处理数量

    Returns:
        {'success': bool, 'processed': int, 'failed': int}
    """
    if conn is None:
        conn = db_connect(DB_PATH)

    cursor = conn.cursor()
    claim_limit = min(max(int(batch_size), 1), 900)

    # 确保 embeddings 表存在
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            di_code TEXT PRIMARY KEY,
            embedding BLOB,
            text_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 兜底恢复：将长期处于 processing 的记录回退为 pending，避免异常退出导致队列卡死。
    # 使用 claimed_at 判断是否超时，避免“老记录刚认领就被回收”。
    if is_postgres_backend():
        cursor.execute('''
            UPDATE embedding_update_queue
            SET status = 'pending',
                claimed_at = NULL,
                error_message = COALESCE(error_message, 'Recovered from stale processing')
            WHERE status = 'processing'
              AND COALESCE(claimed_at, created_at) < (CURRENT_TIMESTAMP - INTERVAL '2 hours')
        ''')
    else:
        cursor.execute('''
            UPDATE embedding_update_queue
            SET status = 'pending',
                claimed_at = NULL,
                error_message = COALESCE(error_message, 'Recovered from stale processing')
            WHERE status = 'processing'
              AND COALESCE(claimed_at, created_at) < datetime('now', '-2 hours')
        ''')
    conn.commit()

    # 原子认领队列：先在写事务中把 pending 标记为 processing，避免并发重复消费。
    pending_items = []
    try:
        if is_postgres_backend():
            conn.execute('BEGIN')
        else:
            conn.execute('BEGIN IMMEDIATE')
        cursor.execute('''
            SELECT id, di_code, action
            FROM embedding_update_queue
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT ?
        ''', (claim_limit,))
        candidates = cursor.fetchall()
        if candidates:
            candidate_ids = [row[0] for row in candidates]
            placeholders = ','.join('?' for _ in candidate_ids)
            cursor.execute(
                f'''
                UPDATE embedding_update_queue
                SET status = 'processing',
                    claimed_at = CURRENT_TIMESTAMP,
                    processed_at = NULL,
                    error_message = NULL
                WHERE status = 'pending' AND id IN ({placeholders})
                ''',
                candidate_ids
            )
            cursor.execute(
                f'''
                SELECT id, di_code, action
                FROM embedding_update_queue
                WHERE status = 'processing' AND id IN ({placeholders})
                ORDER BY created_at
                ''',
                candidate_ids
            )
            pending_items = cursor.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if not pending_items:
        return {'success': True, 'processed': 0, 'failed': 0}

    print(f"[Queue] 已原子认领 {len(pending_items)} 个向量更新任务")
    claimed_ids = [row[0] for row in pending_items]

    processed = 0
    failed = 0

    api_base, api_key, model = get_api_config()

    for item_id, di_code, action in pending_items:
        try:
            # 获取产品信息
            cursor.execute('''
                SELECT di_code, product_name, model, description, scope
                FROM products
                WHERE di_code = ?
            ''', (di_code,))

            row = cursor.fetchone()
            if not row:
                # 产品不存在，标记为失败
                cursor.execute('''
                    UPDATE embedding_update_queue
                    SET status = 'failed', processed_at = CURRENT_TIMESTAMP,
                        error_message = 'Product not found'
                    WHERE id = ?
                ''', (item_id,))
                failed += 1
                continue

            product = {
                'di_code': row[0],
                'product_name': row[1],
                'model': row[2],
                'description': row[3],
                'scope': row[4]
            }

            text = build_product_text(product)
            text_hash = compute_text_hash(text)

            if not text.strip():
                # 空文本，跳过
                cursor.execute('''
                    UPDATE embedding_update_queue
                    SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (item_id,))
                processed += 1
                continue

            # 检查是否需要更新（对比哈希）
            cursor.execute('SELECT text_hash, embedding FROM embeddings WHERE di_code = ?', (di_code,))
            existing = cursor.fetchone()

            if existing and existing[0] == text_hash and existing[1] is not None:
                # 内容未变更，直接标记完成
                cursor.execute('''
                    UPDATE embedding_update_queue
                    SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (item_id,))
                processed += 1
                continue

            # 调用 API 生成向量（简化版：同步调用）
            if api_base and api_key:
                try:
                    url = f"{api_base}/embeddings"
                    headers = {
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json'
                    }
                    payload = {
                        "model": model,
                        "input": text
                    }

                    response = requests.post(url, headers=headers, json=payload, timeout=60)
                    response.raise_for_status()

                    data = response.json()
                    embedding = data.get('data', [{}])[0].get('embedding')

                    if embedding:
                        # 保存向量
                        blob = vector_to_blob(embedding)
                        cursor.execute('''
                            INSERT OR REPLACE INTO embeddings (di_code, embedding, text_hash)
                            VALUES (?, ?, ?)
                        ''', (di_code, blob, text_hash))

                        # 标记队列完成
                        cursor.execute('''
                            UPDATE embedding_update_queue
                            SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        ''', (item_id,))

                        processed += 1

                        if processed % 100 == 0:
                            conn.commit()
                            print(f"[Queue] 已处理 {processed}/{len(pending_items)}")
                    else:
                        raise Exception("Empty embedding response")

                except requests.exceptions.HTTPError as e:
                    # 检测余额不足错误
                    is_insufficient_balance = False
                    error_msg = str(e)

                    if hasattr(e, 'response') and e.response is not None:
                        status_code = e.response.status_code
                        try:
                            error_data = e.response.json()
                            error_code = error_data.get('code', '')
                            error_info = error_data.get('error', {}).get('message', '') if isinstance(error_data.get('error'), dict) else str(error_data)

                            # 阿里云余额不足错误码: 402, InsufficientBalance, InvalidApiKey
                            if (status_code == 402 or
                                'InsufficientBalance' in error_code or
                                '余额不足' in str(error_data) or
                                'insufficient balance' in error_info.lower() or
                                'InvalidApiKey' in error_code):
                                is_insufficient_balance = True
                                error_msg = f"余额不足或API密钥无效: {error_code}"
                        except:
                            # 解析失败，检查状态码和文本
                            if status_code == 402:
                                is_insufficient_balance = True
                                error_msg = "余额不足 (HTTP 402)"

                    if is_insufficient_balance:
                        print(f"[Queue] ⚠️ 检测到余额不足，暂停向量化处理: {di_code}")
                        for start in range(0, len(claimed_ids), 900):
                            chunk_ids = claimed_ids[start:start + 900]
                            placeholders = ','.join('?' for _ in chunk_ids)
                            cursor.execute(
                                f'''
                                UPDATE embedding_update_queue
                                SET status = 'pending',
                                    claimed_at = NULL,
                                    processed_at = NULL,
                                    error_message = ?
                                WHERE status = 'processing' AND id IN ({placeholders})
                                ''',
                                [error_msg] + chunk_ids
                            )
                        # 跳出循环，不再处理后续记录
                        conn.commit()
                        print(f"[Queue] ⚠️ 余额不足，已暂停处理。剩余 {len(pending_items) - processed - 1} 条记录将保留在队列中")
                        return {
                            'success': False,
                            'processed': processed,
                            'failed': failed,
                            'error': 'INSUFFICIENT_BALANCE',
                            'error_message': error_msg
                        }
                    else:
                        # 其他错误，正常标记为失败
                        print(f"[Queue] 生成向量失败 {di_code}: {e}")
                        cursor.execute('''
                            UPDATE embedding_update_queue
                            SET status = 'failed', processed_at = CURRENT_TIMESTAMP,
                                error_message = ?
                            WHERE id = ?
                        ''', (error_msg[:200], item_id))
                        failed += 1
            else:
                # 无 API 配置，标记为失败
                cursor.execute('''
                    UPDATE embedding_update_queue
                    SET status = 'failed', processed_at = CURRENT_TIMESTAMP,
                        error_message = 'API not configured'
                    WHERE id = ?
                ''', (item_id,))
                failed += 1

        except Exception as e:
            print(f"[Queue] 处理失败 {di_code}: {e}")
            cursor.execute('''
                UPDATE embedding_update_queue
                SET status = 'failed', processed_at = CURRENT_TIMESTAMP,
                    error_message = ?
                WHERE id = ?
            ''', (str(e)[:200], item_id))
            failed += 1

    conn.commit()
    print(f"[Queue] 处理完成: 成功 {processed}, 失败 {failed}")

    return {
        'success': failed == 0,
        'partial': processed > 0 and failed > 0,
        'processed': processed,
        'failed': failed
    }
