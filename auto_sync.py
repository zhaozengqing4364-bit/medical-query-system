"""
UDID 自动化同步与向量生成服务
===============================

功能：
1. 定时增量数据同步（每日/每周）
2. 自动向量生成（支持实时API和Batch API）
3. 状态监控和日志记录
4. 失败重试机制

用法：
    # 手动运行
    python auto_sync.py

    # 添加到 crontab（每天凌晨2点运行）
    0 2 * * * cd /path/to/project && python auto_sync.py >> logs/sync.log 2>&1

    # 查看同步状态
    python auto_sync.py --status

    # 强制全量向量重建
    python auto_sync.py --rebuild-vectors
"""

import os
import sys
import sqlite3
import time
import argparse
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable
from db_backend import connect as db_connect, is_postgres_backend

# 导入现有模块
from udid_hybrid_system import LocalDataLake
from udid_sync import sync_incremental, fetch_rss_feed, download_zip, extract_and_import
from embedding_batch import (
    process_embedding_queue,
    generate_jsonl, upload_file, create_batch,
    check_batch_status, download_results, import_results,
    load_pipeline_state, save_pipeline_state,
    vector_to_blob, build_product_text, compute_text_hash,
    get_api_config
)

# 配置
DB_PATH = os.path.join(os.path.dirname(__file__), 'udid_hybrid_lake.db')
AUTO_SYNC_LOG = os.path.join(os.path.dirname(__file__), 'data', 'auto_sync.log')
LOCK_FILE = os.path.join(os.path.dirname(__file__), 'data', '.sync_lock')

# 确保目录存在
os.makedirs(os.path.dirname(AUTO_SYNC_LOG), exist_ok=True)

def log_message(message: str, level: str = 'INFO'):
    """记录日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = f"[{timestamp}] [{level}] {message}"
    print(log_line)

    # 写入日志文件
    with open(AUTO_SYNC_LOG, 'a', encoding='utf-8') as f:
        f.write(log_line + '\n')

def acquire_lock() -> bool:
    """获取锁（兼容旧入口，内部统一委托到文件描述符实现）"""
    return acquire_lock_with_fd()

# 全局锁文件描述符（保持锁）
_lock_fd = None

def acquire_lock_with_fd() -> bool:
    """获取锁并保存文件描述符（推荐方式）"""
    global _lock_fd
    import fcntl

    try:
        _lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False

def release_lock():
    """释放锁"""
    global _lock_fd
    import fcntl

    try:
        if _lock_fd:
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
    except:
        pass

    # 不删除锁文件，避免“解锁后删文件”竞态导致并发实例绕过互斥。

def get_sync_status() -> Dict:
    """获取同步状态"""
    conn = db_connect(DB_PATH)
    cursor = conn.cursor()

    status = {
        'total_products': 0,
        'total_vectors': 0,
        'pending_vectors': 0,
        'last_sync': None,
        'last_vector_update': None
    }

    try:
        # 产品总数
        cursor.execute('SELECT COUNT(*) FROM products')
        status['total_products'] = cursor.fetchone()[0]

        # 向量总数（仅统计有效向量）
        cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
        status['total_vectors'] = cursor.fetchone()[0]

        # 待处理向量（显式队列）
        cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
        pending_queue = cursor.fetchone()[0]
        # 待处理向量（隐式缺口：产品总数 - 有效向量数）
        pending_gap = max(0, status['total_products'] - status['total_vectors'])
        status['pending_vectors'] = max(pending_queue, pending_gap)

        # 最后同步时间
        cursor.execute('SELECT MAX(created_at) FROM sync_log')
        result = cursor.fetchone()
        if result and result[0]:
            status['last_sync'] = result[0]

        # 最后向量更新时间
        cursor.execute("SELECT MAX(processed_at) FROM embedding_update_queue WHERE status = 'completed'")
        result = cursor.fetchone()
        if result and result[0]:
            status['last_vector_update'] = result[0]
        else:
            cursor.execute("SELECT MAX(created_at) FROM embeddings WHERE embedding IS NOT NULL")
            result = cursor.fetchone()
            if result and result[0]:
                status['last_vector_update'] = result[0]

    except Exception as e:
        log_message(f"获取状态失败: {e}", 'ERROR')

    conn.close()
    return status

def run_data_sync() -> Dict:
    """运行数据同步"""
    log_message("=" * 60)
    log_message("开始数据同步")
    log_message("=" * 60)

    data_lake = LocalDataLake(db_path=DB_PATH)
    result = sync_incremental(data_lake)

    if result['success']:
        log_message(f"✓ 同步成功: {result['message']}")
    else:
        log_message(f"✗ 同步失败: {result['message']}", 'ERROR')

    return result

def run_vector_sync_quick(conn: sqlite3.Connection, batch_size: int = 100) -> Dict:
    """
    快速向量同步（使用实时API）
    适合小批量（<1000条）增量更新
    """
    log_message("=" * 60)
    log_message("开始快速向量同步（实时API）")
    log_message("=" * 60)

    result = process_embedding_queue(conn, batch_size=batch_size)

    if result['success']:
        log_message(f"✓ 向量同步完成: 成功 {result['processed']}, 失败 {result['failed']}")
    else:
        log_message(f"✗ 向量同步失败", 'ERROR')

    return result

def cancel_batch(batch_id: str, config: Dict = None) -> bool:
    """
    取消 Batch 任务

    Args:
        batch_id: Batch 任务 ID
        config: API 配置

    Returns:
        是否成功取消
    """
    api_base, api_key, _ = get_api_config(config)

    if not api_base or not api_key:
        return False

    try:
        # OpenAI 兼容 API 取消端点
        url = f"{api_base}/batches/{batch_id}/cancel"
        headers = {'Authorization': f'Bearer {api_key}'}

        response = requests.post(url, headers=headers, timeout=30)
        return response.status_code in [200, 202]
    except Exception as e:
        log_message(f"取消 Batch 任务失败: {e}", 'WARNING')
        return False


def run_vector_sync_batch(
    conn: sqlite3.Connection,
    max_records: int = 50000,
    should_stop: Optional[Callable[[], bool]] = None
) -> Dict:
    """
    批量向量同步（使用阿里云Batch API）
    适合大批量（>1000条）更新
    """
    log_message("=" * 60)
    log_message("开始批量向量同步（Batch API）")
    log_message("=" * 60)

    cursor = conn.cursor()

    def _update_processing_rows(ids, status_value: str, error_message: Optional[str] = None):
        if not ids:
            return
        for start in range(0, len(ids), 900):
            chunk_ids = ids[start:start + 900]
            placeholders = ','.join('?' for _ in chunk_ids)
            if status_value == 'pending':
                cursor.execute(
                    f'''
                    UPDATE embedding_update_queue
                    SET status = 'pending',
                        claimed_at = NULL,
                        processed_at = NULL,
                        error_message = ?
                    WHERE status = 'processing' AND id IN ({placeholders})
                    ''',
                    [error_message] + chunk_ids
                )
            else:
                cursor.execute(
                    f'''
                    UPDATE embedding_update_queue
                    SET status = ?,
                        claimed_at = NULL,
                        processed_at = CURRENT_TIMESTAMP,
                        error_message = ?
                    WHERE status = 'processing' AND id IN ({placeholders})
                    ''',
                    [status_value, error_message] + chunk_ids
                )
        conn.commit()

    def _fail_claimed(ids, reason: str) -> Dict:
        _update_processing_rows(ids, 'failed', reason[:200])
        return {'success': False, 'processed': 0, 'failed': len(ids), 'error': reason}

    # 获取待处理数量（显式队列）
    cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]

    if pending_count == 0:
        # Fail-fast: 队列为空并不代表所有向量都已就绪，需检查隐式缺口
        cursor.execute('SELECT COUNT(*) FROM products')
        total_products = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
        total_vectors = cursor.fetchone()[0]
        missing_vectors = max(0, total_products - total_vectors)
        if missing_vectors > 0:
            msg = (
                f"队列为空，但检测到 {missing_vectors} 条缺失向量。"
                "请先补齐迁移数据或执行 /api/embedding/build。"
            )
            log_message(msg, 'WARNING')
            return {
                'success': False,
                'processed': 0,
                'failed': 0,
                'missing_vectors': missing_vectors,
                'error': msg
            }
        log_message("没有待处理的向量")
        return {'success': True, 'processed': 0, 'failed': 0}

    if pending_count <= 1000:
        log_message(f"待处理数量较少({pending_count})，切换到快速模式")
        return run_vector_sync_quick(conn, batch_size=pending_count)

    # 准备Batch API输入
    api_base, api_key, model = get_api_config()
    if not api_base or not api_key:
        log_message("API未配置，跳过向量生成", 'WARNING')
        return {'success': False, 'error': 'API not configured'}

    log_message(f"待处理向量: {pending_count}，使用Batch API")

    # 先原子认领队列，避免并发重复消费
    claim_limit = min(max_records, 50000)
    pending_items = []
    candidate_ids = []
    try:
        if is_postgres_backend():
            conn.execute('BEGIN')
        else:
            conn.execute('BEGIN IMMEDIATE')
        cursor.execute('''
            SELECT q.id
            FROM embedding_update_queue q
            WHERE q.status = 'pending'
            ORDER BY q.created_at
            LIMIT ?
        ''', (claim_limit,))
        candidate_ids = [row[0] for row in cursor.fetchall()]
        if candidate_ids:
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
                SELECT q.id, q.di_code, p.product_name, p.model, p.description, p.scope
                FROM embedding_update_queue q
                JOIN products p ON q.di_code = p.di_code
                WHERE q.status = 'processing' AND q.id IN ({placeholders})
                ORDER BY q.created_at
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

    claimed_ids = list(candidate_ids)
    found_ids = {row[0] for row in pending_items}
    missing_product_ids = [item_id for item_id in claimed_ids if item_id not in found_ids]
    if missing_product_ids:
        _update_processing_rows(missing_product_ids, 'failed', 'Product not found for queue item')
        missing_set = set(missing_product_ids)
        claimed_ids = [item_id for item_id in claimed_ids if item_id not in missing_set]

    if not pending_items:
        return {'success': False, 'processed': 0, 'failed': len(missing_product_ids), 'error': 'Product not found'}
    submitted_ids = []
    submitted_di_codes = []
    skipped_empty_ids = []

    # 创建临时JSONL文件
    import tempfile
    import json

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    jsonl_path = os.path.join(tempfile.gettempdir(), f'auto_sync_{timestamp}.jsonl')
    result_file = None
    count = 0

    with open(jsonl_path, 'w', encoding='utf-8') as f:
        for row in pending_items:
            item_id, di_code = row[0], row[1]
            product = {
                'di_code': di_code,
                'product_name': row[2],
                'model': row[3],
                'description': row[4],
                'scope': row[5]
            }
            text = build_product_text(product)
            if not text.strip():
                skipped_empty_ids.append(item_id)
                continue

            text_hash = compute_text_hash(text)
            submitted_ids.append(item_id)
            submitted_di_codes.append(di_code)

            line = {
                "custom_id": f"{di_code}::{text_hash}",
                "method": "POST",
                "url": "/v1/embeddings",
                "body": {
                    "model": model,
                    "input": text
                }
            }
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
            count += 1

    if skipped_empty_ids:
        _update_processing_rows(skipped_empty_ids, 'completed', 'empty text skipped')

    if count == 0:
        log_message("本批次均为无有效文本记录，直接标记完成")
        return {'success': True, 'processed': len(skipped_empty_ids), 'failed': 0}

    log_message(f"生成JSONL文件: {count} 条记录")

    # 上传文件
    file_id = upload_file(jsonl_path)
    if not file_id:
        log_message("文件上传失败", 'ERROR')
        return _fail_claimed(submitted_ids, 'Upload failed')

    log_message(f"文件上传成功: {file_id}")

    # 创建Batch任务
    batch_id = create_batch(file_id)
    if not batch_id:
        log_message("创建Batch任务失败", 'ERROR')
        return _fail_claimed(submitted_ids, 'Batch creation failed')

    log_message(f"Batch任务创建成功: {batch_id}")
    log_message("等待Batch处理完成（这可能需要10-30分钟）...")

    # 等待完成（带绝对超时保护）
    import time
    ABSOLUTE_TIMEOUT = 1800  # 30分钟绝对超时
    MAX_WAIT = 3600  # 1小时最大等待
    CHECK_INTERVAL = 60  # 每分钟检查一次
    start_time = time.time()

    status = {'status': 'unknown'}
    for waited in range(0, MAX_WAIT, CHECK_INTERVAL):
        if should_stop and should_stop():
            log_message("检测到停止请求，尝试取消 Batch 任务", 'WARNING')
            try:
                cancel_batch(batch_id)
            except Exception as cancel_err:
                log_message(f"取消 Batch 任务失败: {cancel_err}", 'WARNING')
            _update_processing_rows(submitted_ids, 'pending', 'STOP_REQUESTED')
            return {'success': False, 'error': 'STOP_REQUESTED', 'stopped': True}

        elapsed = time.time() - start_time
        if elapsed > ABSOLUTE_TIMEOUT:
            log_message(f"Batch任务检查超时（已超过 {elapsed//60:.0f} 分钟），强制取消", 'ERROR')
            try:
                cancel_batch(batch_id)
            except Exception:
                pass
            return _fail_claimed(submitted_ids, f'Timeout after {elapsed//60:.0f} minutes')

        status = check_batch_status(batch_id)
        current_status = status.get('status', 'unknown')
        log_message(f"Batch状态: {current_status} (已等待 {waited//60} 分钟)")

        if current_status == 'completed':
            break
        if current_status in ['failed', 'expired', 'cancelled', 'error']:
            log_message(f"Batch任务失败: {status}", 'ERROR')
            return _fail_claimed(submitted_ids, f'Batch failed: {current_status}')

        sleep_unit = 2
        waited_sleep = 0
        while waited_sleep < CHECK_INTERVAL:
            if should_stop and should_stop():
                log_message("检测到停止请求，尝试取消 Batch 任务", 'WARNING')
                try:
                    cancel_batch(batch_id)
                except Exception as cancel_err:
                    log_message(f"取消 Batch 任务失败: {cancel_err}", 'WARNING')
                _update_processing_rows(submitted_ids, 'pending', 'STOP_REQUESTED')
                return {'success': False, 'error': 'STOP_REQUESTED', 'stopped': True}
            time.sleep(sleep_unit)
            waited_sleep += sleep_unit

    output_file_id = status.get('output_file_id')
    if not output_file_id:
        log_message("未找到输出文件ID", 'ERROR')
        return _fail_claimed(submitted_ids, 'No output file')

    result_file = download_results(batch_id)
    if not result_file:
        log_message("下载结果失败", 'ERROR')
        return _fail_claimed(submitted_ids, 'Download failed')

    import_result = import_results(result_file, conn)
    if not import_result.get('success', False):
        log_message(
            f"导入结果失败: 成功 {import_result.get('imported', 0)}, 失败 {import_result.get('failed', 0)}",
            'ERROR'
        )
        reason = import_result.get('error') or 'Import failed'
        return _fail_claimed(submitted_ids, reason)

    imported_codes = import_result.get('imported_di_codes', [])
    if imported_codes:
        for start in range(0, len(imported_codes), 900):
            chunk_codes = imported_codes[start:start + 900]
            placeholders = ','.join('?' for _ in chunk_codes)
            cursor.execute(
                f'''
                UPDATE embedding_update_queue
                SET status = 'completed',
                    claimed_at = NULL,
                    processed_at = CURRENT_TIMESTAMP,
                    error_message = NULL
                WHERE status = 'processing' AND di_code IN ({placeholders})
                ''',
                chunk_codes
            )
        conn.commit()

    # 仍然处于 processing 的视为失败（含未出现在结果集的提交项）
    still_processing_ids = []
    if submitted_ids:
        for start in range(0, len(submitted_ids), 900):
            chunk_ids = submitted_ids[start:start + 900]
            placeholders = ','.join('?' for _ in chunk_ids)
            cursor.execute(
                f'''
                SELECT id
                FROM embedding_update_queue
                WHERE status = 'processing' AND id IN ({placeholders})
                ''',
                chunk_ids
            )
            still_processing_ids.extend([row[0] for row in cursor.fetchall()])
    if still_processing_ids:
        _update_processing_rows(still_processing_ids, 'failed', 'Batch result missing for claimed item')

    # 清理临时文件
    try:
        if os.path.exists(jsonl_path):
            os.remove(jsonl_path)
    except Exception:
        pass
    try:
        if result_file and os.path.exists(result_file):
            os.remove(result_file)
    except Exception:
        pass

    processed_count = import_result.get('imported', 0) + len(skipped_empty_ids)
    failed_count = import_result.get('failed', 0) + len(still_processing_ids)
    log_message(f"✓ 批量向量同步完成: 成功 {processed_count}, 失败 {failed_count}")

    return {
        'success': failed_count == 0,
        'partial': processed_count > 0 and failed_count > 0,
        'processed': processed_count,
        'failed': failed_count,
        'batch_id': batch_id
    }

def auto_sync():
    """自动同步主流程"""
    log_message("")
    log_message("=" * 60)
    log_message("UDID 自动同步服务启动")
    log_message("=" * 60)

    # 获取锁（使用文件描述符保持锁）
    if not acquire_lock_with_fd():
        log_message("另一个同步进程正在运行，退出", 'WARNING')
        return

    try:
        conn = db_connect(DB_PATH)

        # 步骤1: 数据同步
        sync_result = run_data_sync()
        if not sync_result.get('success'):
            log_message("数据同步失败，本次任务终止，不进入向量阶段", 'ERROR')
            log_message("")
            log_message("=" * 60)
            log_message("同步报告")
            log_message("=" * 60)
            log_message("数据同步: 失败")
            log_message(f"  - 消息: {sync_result.get('message', 'N/A')}")
            log_message("向量同步: 未执行（依赖数据同步成功）")
            return

        # 获取待处理向量数量
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
        pending_count = cursor.fetchone()[0]

        if pending_count > 0:
            log_message(f"发现 {pending_count} 个待处理向量")

            # 步骤2: 向量同步
            insufficient_balance_detected = False

            if pending_count <= 100:
                # 小批量使用快速模式
                vector_result = run_vector_sync_quick(conn, batch_size=pending_count)
                if vector_result.get('error') == 'INSUFFICIENT_BALANCE':
                    insufficient_balance_detected = True
            elif pending_count <= 5000:
                # 中批量使用实时API分批处理
                total_processed = 0
                total_failed = 0

                while True:
                    result = run_vector_sync_quick(conn, batch_size=100)

                    # 检测余额不足
                    if result.get('error') == 'INSUFFICIENT_BALANCE':
                        insufficient_balance_detected = True
                        total_processed += result.get('processed', 0)
                        total_failed += result.get('failed', 0)
                        log_message(f"⚠️ 检测到余额不足，已处理 {total_processed} 条，暂停后续处理", 'WARNING')
                        break

                    total_processed += result.get('processed', 0)
                    total_failed += result.get('failed', 0)

                    # 检查是否还有未处理的
                    cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
                    remaining = cursor.fetchone()[0]

                    if remaining == 0 or result.get('processed', 0) == 0:
                        break

                    # 避免API限流
                    time.sleep(1)

                vector_result = {
                    'success': not insufficient_balance_detected,
                    'processed': total_processed,
                    'failed': total_failed,
                    'error': 'INSUFFICIENT_BALANCE' if insufficient_balance_detected else None
                }
            else:
                # 大批量使用Batch API
                vector_result = run_vector_sync_batch(conn)
                # Batch API 余额不足检测（通过状态检查）
                if not vector_result.get('success'):
                    error_msg = vector_result.get('error', '')
                    if '余额' in error_msg or 'balance' in error_msg.lower() or '402' in str(error_msg):
                        insufficient_balance_detected = True
                        log_message(f"⚠️ Batch API 检测到余额问题: {error_msg}", 'WARNING')

            # 生成同步报告
            log_message("")
            log_message("=" * 60)
            log_message("同步报告")
            log_message("=" * 60)
            log_message(f"数据同步: {'成功' if sync_result['success'] else '失败'}")
            log_message(f"  - 消息: {sync_result.get('message', 'N/A')}")

            if insufficient_balance_detected:
                log_message("向量同步: ⚠️ 暂停（余额不足）")
                log_message(f"  - 已处理: {vector_result.get('processed', 0)}")
                log_message(f"  - 失败: {vector_result.get('failed', 0)}")
                log_message(f"  - 提示: 请充值阿里云账户后重新运行，未处理记录已保留在队列中")
            else:
                log_message(f"向量同步: {'成功' if vector_result['success'] else '失败'}")
                log_message(f"  - 处理: {vector_result.get('processed', 0)}")
                log_message(f"  - 失败: {vector_result.get('failed', 0)}")
        else:
            log_message("没有待处理的向量")

        conn.close()

    except Exception as e:
        log_message(f"同步过程出错: {e}", 'ERROR')
        import traceback
        log_message(traceback.format_exc(), 'ERROR')

    finally:
        release_lock()
        log_message("=" * 60)
        log_message("自动同步服务结束")
        log_message("=" * 60)

def show_status():
    """显示当前状态"""
    status = get_sync_status()

    print("\n" + "=" * 60)
    print("UDID 系统状态")
    print("=" * 60)
    print(f"产品总数:        {status['total_products']:,}")
    print(f"向量总数:        {status['total_vectors']:,}")
    print(f"覆盖率:          {status['total_vectors']/max(status['total_products'],1)*100:.1f}%")
    print(f"待处理向量:      {status['pending_vectors']:,}")
    print(f"最后同步:        {status['last_sync'] or '从未'}")
    print(f"最后向量更新:    {status['last_vector_update'] or '从未'}")
    print("=" * 60)

    # 显示最近日志
    if os.path.exists(AUTO_SYNC_LOG):
        print("\n最近同步日志（最后5条）:")
        with open(AUTO_SYNC_LOG, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines[-5:]:
                print("  " + line.strip())
        print("")

def reset_failed_queue():
    """重置失败的向量队列（用于充值后重新处理）"""
    conn = db_connect(DB_PATH)
    cursor = conn.cursor()

    # 获取失败记录数
    cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'failed'")
    failed_count = cursor.fetchone()[0]

    # 获取余额不足导致的失败记录数
    cursor.execute('''
        SELECT COUNT(*) FROM embedding_update_queue
        WHERE status = 'failed' AND error_message LIKE '%余额%'
    ''')
    balance_failed_count = cursor.fetchone()[0]

    if failed_count == 0:
        print("没有失败的队列记录需要重置")
        conn.close()
        return

    print(f"\n发现 {failed_count} 条失败记录（其中 {balance_failed_count} 条疑似余额不足导致）")

    # 重置失败记录为 pending 状态
    cursor.execute('''
        UPDATE embedding_update_queue
        SET status = 'pending',
            error_message = NULL,
            processed_at = NULL
        WHERE status = 'failed'
    ''')

    conn.commit()
    conn.close()

    print(f"✓ 已重置 {failed_count} 条记录，下次同步时将重新处理")
    print(f"提示: 请确保阿里云账户已有足够余额后再运行同步")

def show_queue_details():
    """显示队列详情"""
    conn = db_connect(DB_PATH)
    cursor = conn.cursor()

    print("\n" + "=" * 60)
    print("向量更新队列详情")
    print("=" * 60)

    # 各状态统计
    cursor.execute('''
        SELECT status, COUNT(*) FROM embedding_update_queue GROUP BY status
    ''')
    for status, count in cursor.fetchall():
        print(f"  {status:12}: {count:6,} 条")

    # 最近的失败记录
    cursor.execute('''
        SELECT di_code, error_message, processed_at
        FROM embedding_update_queue
        WHERE status = 'failed'
        ORDER BY processed_at DESC
        LIMIT 5
    ''')
    failed = cursor.fetchall()

    if failed:
        print("\n最近的失败记录:")
        for di_code, error, time in failed:
            error_short = error[:50] + '...' if error and len(error) > 50 else error
            print(f"  {di_code}: {error_short}")

    conn.close()
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description='UDID 自动同步服务')
    parser.add_argument('--status', action='store_true', help='显示当前状态')
    parser.add_argument('--queue', action='store_true', help='显示队列详情')
    parser.add_argument('--reset-failed', action='store_true', help='重置失败的队列记录（充值后使用）')
    parser.add_argument('--quick', action='store_true', help='仅快速向量同步（实时API）')
    parser.add_argument('--batch', action='store_true', help='仅批量向量同步（Batch API）')
    parser.add_argument('--data-only', action='store_true', help='仅同步数据，不生成向量')
    parser.add_argument('--vectors-only', action='store_true', help='仅生成向量，不同步数据')

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.queue:
        show_queue_details()
        return

    if args.reset_failed:
        reset_failed_queue()
        return

    if args.quick or args.batch or args.data_only or args.vectors_only:
        if not acquire_lock_with_fd():
            log_message("另一个同步进程正在运行，退出", 'WARNING')
            return

        conn = db_connect(DB_PATH)
        try:
            if args.quick:
                run_vector_sync_quick(conn)
            elif args.batch:
                run_vector_sync_batch(conn)
            elif args.data_only:
                run_data_sync()
            elif args.vectors_only:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
                pending = cursor.fetchone()[0]

                if pending <= 100:
                    run_vector_sync_quick(conn, pending)
                else:
                    run_vector_sync_batch(conn)
        finally:
            conn.close()
            release_lock()
        return

    # 默认完整流程（内部自行加锁）
    auto_sync()

if __name__ == '__main__':
    main()
