"""
UDID 同步监控服务器
==================

提供HTTP API供前端监控页面调用：
- GET /api/status - 获取同步状态
- POST /api/sync/full - 执行完整同步
- POST /api/sync/data - 仅同步数据
- POST /api/sync/vectors - 仅生成向量
- GET /api/logs - 获取同步日志

用法：
    python sync_server.py

默认端口：8888
"""

import os
import sys
import json
import sqlite3
import threading
import hmac
from datetime import datetime, date, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from typing import List
from db_backend import connect as db_connect

# 加载 .env 文件（如果存在）
def _load_env_file():
    """从 .env 文件加载环境变量"""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

_load_env_file()

# 配置
DB_PATH = os.path.join(os.path.dirname(__file__), 'udid_hybrid_lake.db')
AUTO_SYNC_LOG = os.path.join(os.path.dirname(__file__), 'data', 'auto_sync.log')
PORT = 8888

# API 认证配置
SYNC_API_KEY = os.getenv('SYNC_API_KEY', '')
_WEAK_SYNC_KEYS = {
    '',
    'change-me',
    'your_sync_api_key_here',
    'your_sync_api_key_here_change_in_production',
    'your_random_32char_string_here',
    'sync_api_key',
}


def _is_weak_sync_api_key(value: str) -> bool:
    key = (value or '').strip()
    return len(key) < 24 or key.lower() in _WEAK_SYNC_KEYS


def _parse_allowed_origins() -> List[str]:
    raw = os.getenv(
        'SYNC_CORS_ALLOWED_ORIGINS',
        'http://localhost:8080,http://127.0.0.1:8080,http://localhost:5000,http://127.0.0.1:5000'
    )
    origins = [item.strip() for item in raw.split(',') if item.strip()]
    if not origins:
        raise RuntimeError("SYNC_CORS_ALLOWED_ORIGINS 不能为空")
    return origins

# 全局状态
sync_lock = threading.RLock()
is_syncing = False
stop_requested = False

# 同步进度存储
_sync_progress = {
    'is_running': False,
    'stage': '',
    'current': 0,
    'total': 0,
    'message': '',
    'start_time': None,
    'last_update': None,
    'elapsed_seconds': 0,
    'estimated_remaining': None
}

# 同步历史记录（内存缓存，重启后从数据库加载）
_sync_history = []
SYNC_CORS_ALLOWED_ORIGINS = _parse_allowed_origins()

if _is_weak_sync_api_key(SYNC_API_KEY):
    raise RuntimeError(
        "SYNC_API_KEY 未配置或过弱。请在环境变量中设置长度>=24的随机强密钥。"
    )


def update_sync_progress(stage: str, current: int, total: int, message: str):
    """更新同步进度"""
    global _sync_progress
    with sync_lock:
        now = datetime.now(timezone.utc)

        if _sync_progress['start_time'] is None:
            _sync_progress['start_time'] = now.isoformat()

        start_time_raw = _sync_progress['start_time']
        try:
            start_time = datetime.fromisoformat(start_time_raw) if isinstance(start_time_raw, str) else now
        except Exception:
            start_time = now
        elapsed = (now - start_time).total_seconds()

        # 计算预计剩余时间
        estimated_remaining = None
        if current > 0 and total > 0:
            rate = current / elapsed if elapsed > 0 else 0
            if rate > 0:
                remaining_items = total - current
                estimated_remaining = int(remaining_items / rate)

        _sync_progress.update({
            'is_running': True,
            'stage': stage,
            'current': current,
            'total': total,
            'message': message,
            'last_update': now.isoformat(),
            'elapsed_seconds': int(elapsed),
            'estimated_remaining': estimated_remaining
        })


def reset_sync_progress():
    """重置同步进度"""
    global _sync_progress
    with sync_lock:
        _sync_progress = {
            'is_running': False,
            'stage': '',
            'current': 0,
            'total': 0,
            'message': '',
            'start_time': None,
            'last_update': None,
            'elapsed_seconds': 0,
            'estimated_remaining': None
        }


def _is_stop_requested() -> bool:
    with sync_lock:
        return bool(stop_requested)


def save_sync_history(sync_type: str, records_count: int, status: str, message: str, duration_seconds: int):
    """保存同步历史到数据库"""
    try:
        start_time_utc = (datetime.now(timezone.utc) - timedelta(seconds=int(duration_seconds))).isoformat()
        conn = db_connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT,
                start_time TIMESTAMP,
                end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                records_count INTEGER,
                status TEXT,
                message TEXT,
                duration_seconds INTEGER
            )
        ''')
        cursor.execute('''
            INSERT INTO sync_history (sync_type, start_time, records_count, status, message, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (sync_type, start_time_utc, records_count, status, message, duration_seconds))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Sync] 保存同步历史失败: {e}")


def get_sync_history(limit: int = 10) -> list:
    """获取同步历史记录"""
    try:
        conn = db_connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT,
                start_time TIMESTAMP,
                end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                records_count INTEGER,
                status TEXT,
                message TEXT,
                duration_seconds INTEGER
            )
        ''')
        conn.commit()
        cursor.execute('''
            SELECT sync_type, start_time, end_time, records_count, status, message, duration_seconds
            FROM sync_history
            ORDER BY start_time DESC
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()

        history = []
        for row in rows:
            history.append({
                'sync_type': row[0],
                'start_time': row[1],
                'end_time': row[2],
                'records_count': row[3],
                'status': row[4],
                'message': row[5],
                'duration_seconds': row[6]
            })
        return history
    except Exception as e:
        print(f"[Sync] 获取同步历史失败: {e}")
        return []

class SyncHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 自定义日志格式
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}")

    def _resolve_cors_origin(self) -> str:
        origin = (self.headers.get('Origin') or '').strip()
        if not origin:
            return ''
        if origin in SYNC_CORS_ALLOWED_ORIGINS:
            return origin
        return ''

    def _set_cors_headers(self):
        cors_origin = self._resolve_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-API-Key, X-Timestamp')

    def _set_headers(self, content_type='application/json'):
        self.send_response(200)
        self.send_header('Content-type', content_type)
        self._set_cors_headers()
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        )
        self.end_headers()

    def _send_json(self, data):
        self._set_headers()
        def _json_default(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            return str(obj)

        self.wfile.write(
            json.dumps(data, ensure_ascii=False, default=_json_default).encode('utf-8')
        )

    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self._set_cors_headers()
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}, ensure_ascii=False).encode('utf-8'))

    def _check_api_key(self):
        """检查 API Key 认证"""
        # 从请求头获取 API Key
        api_key = self.headers.get('X-API-Key', '')
        if not api_key:
            return False

        # 简单的时间戳验证（防止重放攻击，允许5分钟时间差）
        import time
        timestamp = self.headers.get('X-Timestamp', '')
        if timestamp:
            try:
                req_time = int(timestamp)
                current_time = int(time.time())
                if abs(current_time - req_time) > 300:  # 5分钟
                    return False
            except ValueError:
                return False

        return hmac.compare_digest(api_key, SYNC_API_KEY)

    def _require_auth(self):
        """返回认证错误响应"""
        self.send_response(401)
        self.send_header('Content-type', 'application/json')
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({
            'success': False,
            'error': '未授权访问，需要提供有效的 API Key'
        }, ensure_ascii=False).encode('utf-8'))
        return False

    def do_OPTIONS(self):
        self._set_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        protected_paths = (
            '/api/status',
            '/api/sync/status',
            '/api/logs',
            '/api/sync/logs',
            '/api/sync/progress',
            '/api/sync/history',
        )
        if path in protected_paths and not self._check_api_key():
            self._require_auth()
            return

        if path in ('/api/status', '/api/sync/status'):
            self.handle_status()
        elif path in ('/api/logs', '/api/sync/logs'):
            self.handle_logs()
        elif path == '/api/sync/progress':
            self.handle_sync_progress()
        elif path == '/api/sync/history':
            self.handle_sync_history()
        elif path == '/':
            self.handle_index()
        else:
            self._send_error(404, 'Not found')

    def do_POST(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        # 敏感操作需要 API Key 认证
        sensitive_paths = ['/api/sync/full', '/api/sync/data', '/api/sync/vectors', '/api/sync/start', '/api/sync/stop']

        if path in sensitive_paths:
            if not self._check_api_key():
                self._require_auth()
                return

        if path == '/api/sync/full':
            self.handle_sync('full')
        elif path == '/api/sync/data':
            self.handle_sync('data')
        elif path == '/api/sync/vectors':
            self.handle_sync('vectors')
        elif path == '/api/sync/start':
            self.handle_sync_start('full')
        elif path == '/api/sync/stop':
            self.handle_sync_stop()
        else:
            self._send_error(404, 'Not found')

    def handle_index(self):
        """返回监控页面"""
        monitor_html = os.path.join(os.path.dirname(__file__), 'sync_monitor.html')
        if os.path.exists(monitor_html):
            self._set_headers('text/html')
            with open(monitor_html, 'r', encoding='utf-8') as f:
                self.wfile.write(f.read().encode('utf-8'))
        else:
            self._send_error(404, 'Monitor page not found')

    def handle_status(self):
        """获取同步状态"""
        try:
            conn = db_connect(DB_PATH)
            cursor = conn.cursor()

            status = {
                'total_products': 0,
                'total_vectors': 0,
                'pending_vectors': 0,
                'last_sync': None,
                'last_vector_update': None,
                'is_syncing': False
            }
            with sync_lock:
                status['is_syncing'] = bool(is_syncing)

            # 产品总数
            cursor.execute('SELECT COUNT(*) FROM products')
            status['total_products'] = cursor.fetchone()[0]

            # 向量总数（仅统计有效向量）
            cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
            status['total_vectors'] = cursor.fetchone()[0]

            # 待处理向量（显式队列 + 隐式缺口）
            cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
            pending_queue = cursor.fetchone()[0]
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

            conn.close()

            self._send_json(status)

        except Exception as e:
            print(f"[Sync] 获取状态失败: {e}")
            self._send_error(500, '服务器内部错误')

    def handle_logs(self):
        """获取同步日志"""
        try:
            lines = []
            if os.path.exists(AUTO_SYNC_LOG):
                with open(AUTO_SYNC_LOG, 'r', encoding='utf-8') as f:
                    lines = f.readlines()[-100:]  # 最后100行

            logs = []
            for line in lines:
                line = line.strip()
                log_type = 'info'
                if '[ERROR]' in line:
                    log_type = 'error'
                elif '[WARNING]' in line:
                    log_type = 'warning'
                elif '成功' in line or '✓' in line:
                    log_type = 'success'

                logs.append({
                    'message': line,
                    'type': log_type
                })

            self._send_json({'logs': logs})

        except Exception as e:
            print(f"[Sync] 获取日志失败: {e}")
            self._send_error(500, '服务器内部错误')

    def handle_sync(self, sync_type):
        """处理同步请求（兼容旧接口）"""
        if sync_type not in ('full', 'data', 'vectors'):
            self._send_error(400, f'Unsupported sync type: {sync_type}')
            return
        self.handle_sync_start(sync_type)

    def handle_sync_progress(self):
        """获取同步进度"""
        global _sync_progress
        with sync_lock:
            snapshot = dict(_sync_progress)
        self._send_json({
            'success': True,
            'data': snapshot
        })

    def handle_sync_history(self):
        """获取同步历史"""
        history = get_sync_history(limit=10)
        self._send_json({
            'success': True,
            'data': history
        })

    def handle_sync_start(self, sync_type: str = 'full'):
        """开始同步（支持 full/data/vectors 三种语义）"""
        global is_syncing, stop_requested

        try:
            from auto_sync import acquire_lock_with_fd as acquire_process_lock, release_lock as release_process_lock
        except Exception as e:
            self._send_json({
                'success': False,
                'error': f'无法加载同步锁模块: {e}'
            })
            return

        with sync_lock:
            if is_syncing:
                snapshot = dict(_sync_progress)
                self._send_json({
                    'success': False,
                    'error': '同步正在进行中',
                    'data': snapshot
                })
                return

        if not acquire_process_lock():
            self._send_json({
                'success': False,
                'error': '已有其他进程在执行同步任务'
            })
            return

        with sync_lock:
            is_syncing = True
            stop_requested = False

        # 重置进度
        reset_sync_progress()
        update_sync_progress('preparing', 0, 100, f'准备执行 {sync_type} 同步...')

        # 在后台线程执行同步
        def run_sync_with_progress():
            global is_syncing, stop_requested
            start_time = datetime.now(timezone.utc)
            status = 'success'
            message = ''
            total_records = 0
            data_lake = None

            try:
                sys.path.insert(0, os.path.dirname(__file__))
                from udid_sync import sync_incremental, DB_PATH as SYNC_DB_PATH
                from udid_hybrid_system import LocalDataLake
                from auto_sync import run_vector_sync_batch, run_vector_sync_quick

                data_lake = LocalDataLake(db_path=SYNC_DB_PATH)

                def progress_callback(progress_info):
                    if _is_stop_requested():
                        raise RuntimeError('同步已被用户停止')
                    update_sync_progress(
                        stage=progress_info.get('stage', 'processing'),
                        current=progress_info.get('current', 0),
                        total=progress_info.get('total', 100),
                        message=progress_info.get('message', '处理中...')
                    )

                if sync_type in ('full', 'data'):
                    update_sync_progress('checking', 5, 100, '检查增量更新...')
                    data_result = sync_incremental(data_lake, progress_callback=progress_callback)
                    if not data_result.get('success'):
                        status = 'failed'
                        message = data_result.get('message', '数据同步失败')
                        raise RuntimeError(message)
                    total_records += data_result.get('total_records', 0)
                    message = data_result.get('message', '')

                if _is_stop_requested():
                    raise RuntimeError('同步已被用户停止')

                if sync_type in ('full', 'vectors'):
                    update_sync_progress('vectors', 75, 100, '开始生成向量...')
                    conn = db_connect(SYNC_DB_PATH)
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SELECT COUNT(*) FROM embedding_update_queue WHERE status = 'pending'")
                        pending_count = cursor.fetchone()[0]
                        if pending_count > 0:
                            if _is_stop_requested():
                                raise RuntimeError('同步已被用户停止')
                            if pending_count <= 1000:
                                vector_result = run_vector_sync_quick(conn, batch_size=pending_count)
                            else:
                                vector_result = run_vector_sync_batch(
                                    conn,
                                    max_records=min(pending_count, 50000),
                                    should_stop=_is_stop_requested
                                )
                            if vector_result.get('stopped') or vector_result.get('error') == 'STOP_REQUESTED':
                                raise RuntimeError('同步已被用户停止')
                            if not vector_result.get('success'):
                                status = 'failed'
                                message = vector_result.get('error', '向量同步失败')
                                raise RuntimeError(message)
                            message = f"向量处理完成，成功 {vector_result.get('processed', 0)} 条"
                        else:
                            message = '无待处理向量'
                    finally:
                        conn.close()

                if _is_stop_requested():
                    status = 'stopped'
                    message = '同步已停止'
                    update_sync_progress('stopped', 0, 100, message)
                else:
                    status = 'success'
                    update_sync_progress('completed', 100, 100, message or '同步完成')

            except Exception as e:
                elapsed = int((datetime.now(timezone.utc) - start_time).total_seconds())
                error_msg = str(e)
                if '已被用户停止' in error_msg:
                    status = 'stopped'
                    message = '同步已停止'
                    update_sync_progress('stopped', 0, 100, message)
                else:
                    status = 'failed'
                    message = error_msg
                    print(f"[Sync] 同步失败: {error_msg}")
                    update_sync_progress('failed', 0, 100, f'同步失败: {error_msg}')
            finally:
                elapsed = int((datetime.now(timezone.utc) - start_time).total_seconds())
                save_sync_history(
                    sync_type=sync_type,
                    records_count=total_records,
                    status=status,
                    message=message,
                    duration_seconds=elapsed
                )
                with sync_lock:
                    is_syncing = False
                    stop_requested = False
                    _sync_progress['is_running'] = False
                try:
                    if data_lake is not None:
                        data_lake.release_thread_connection()
                except Exception as release_err:
                    print(f"[Sync] 释放线程数据库连接失败: {release_err}")
                try:
                    release_process_lock()
                except Exception as lock_err:
                    print(f"[Sync] 释放进程锁失败: {lock_err}")

        thread = threading.Thread(target=run_sync_with_progress)
        thread.daemon = True
        try:
            thread.start()
        except Exception as e:
            with sync_lock:
                is_syncing = False
                stop_requested = False
                _sync_progress['is_running'] = False
            try:
                release_process_lock()
            except Exception:
                pass
            self._send_json({
                'success': False,
                'error': f'启动后台同步线程失败: {e}'
            })
            return

        with sync_lock:
            snapshot = {**_sync_progress, 'sync_type': sync_type}
        self._send_json({
            'success': True,
            'message': f'{sync_type} 同步已启动',
            'data': snapshot
        })

    def handle_sync_stop(self):
        """停止同步请求"""
        global stop_requested, is_syncing
        with sync_lock:
            running = bool(is_syncing)
            if running:
                stop_requested = True
        if not running:
            self._send_json({
                'success': False,
                'error': '当前没有进行中的同步任务'
            })
            return
        update_sync_progress('stopped', 0, 100, '已收到停止请求，正在安全停止...')
        self._send_json({
            'success': True,
            'message': '已接收停止请求'
        })

def main():
    server = HTTPServer(('0.0.0.0', PORT), SyncHandler)
    print(f"=" * 60)
    print(f"UDID 同步监控服务器启动")
    print(f"=" * 60)
    print(f"端口: {PORT}")
    print(f"访问地址: http://localhost:{PORT}")
    print(f"API 状态: http://localhost:{PORT}/api/status")
    print(f"按 Ctrl+C 停止服务器")
    print(f"=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        server.shutdown()

if __name__ == '__main__':
    main()
