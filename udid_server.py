"""
UDID 医疗器械查询系统 - Flask 后端服务
======================================

提供以下 API 接口:
- GET  /api/stats    - 获取数据库统计信息
- GET  /api/search   - 传统搜索（关键词+筛选条件）
- POST /api/upload   - 上传 XML 文件
- POST /api/sync     - 手动触发数据同步
- POST /api/ai-match - AI 语义匹配排序
- GET/POST /api/config - 获取/保存 API 配置

版本: 1.0.0
作者: UDID System
"""

import os
import json
import sqlite3
import tempfile
import time
import hmac
import secrets
import threading
from typing import Optional, Dict
from datetime import datetime
import requests
from flask import Flask, request, jsonify, send_from_directory, session, redirect, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# 导入本地数据湖模块
from udid_hybrid_system import LocalDataLake
from db_backend import is_postgres_backend
from config_utils import load_env_file_once
from search_query_utils import (
    build_keyword_or_clause as shared_build_keyword_or_clause,
    build_postgres_keyword_clause,
    build_postgres_keywords_clause,
    collect_highlight_keywords,
)
from sync_schedule import (
    AUTO_SYNC_PUBLIC_KEYS,
    compute_next_run_iso,
    format_schedule_summary,
    normalize_auto_sync_settings,
)

load_env_file_once(os.path.dirname(__file__), log_prefix='[Config]')

# ==========================================
# Flask 应用初始化
# ==========================================, 

def _to_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_allowed_origins() -> list:
    raw = os.getenv(
        'CORS_ALLOWED_ORIGINS',
        'http://localhost:5000,http://127.0.0.1:5000,http://localhost:8080,http://127.0.0.1:8080'
    )
    origins = [item.strip() for item in raw.split(',') if item.strip()]
    if not origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS 不能为空")
    return origins


app = Flask(__name__, static_folder='.', static_url_path='')
CORS(
    app,
    supports_credentials=True,
    resources={r"/api/*": {"origins": _parse_allowed_origins()}}
)

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
app.config['SESSION_COOKIE_SECURE'] = _to_bool(
    os.getenv('SESSION_COOKIE_SECURE'),
    default=os.getenv('FLASK_ENV', '').strip().lower() == 'production'
)

# 初始化数据湖
DB_PATH = os.path.join(os.path.dirname(__file__), 'udid_hybrid_lake.db')
data_lake = LocalDataLake(db_path=DB_PATH)

# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

# Session 配置
SECRET_KEY = os.getenv('SECRET_KEY', '')

# 管理接口保护
ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')
EMBEDDING_RATE_LIMIT_WINDOW = 60
EMBEDDING_RATE_LIMIT_MAX = 3
_EMBEDDING_RATE_LIMIT = {}
LOGIN_RATE_LIMIT_WINDOW = 300
LOGIN_RATE_LIMIT_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 900
_LOGIN_ATTEMPTS = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_DB_ACCESS_LOCK = threading.RLock()
_WEAK_SECRET_VALUES = {
    '',
    'change-me',
    'your_secret_key_here',
    'your_secret_key_here_change_in_production',
    'secret',
    'default',
}


def _is_weak_secret(value: str) -> bool:
    normalized = (value or '').strip()
    if len(normalized) < 32:
        return True
    lowered = normalized.lower()
    if lowered in _WEAK_SECRET_VALUES:
        return True
    if lowered.startswith('your_') and lowered.endswith('_here'):
        return True
    return False


def _mask_secret(value: str) -> str:
    if not value:
        return ''
    value = str(value)
    if len(value) <= 8:
        return '*' * len(value)
    return f"{value[:3]}...{value[-4:]}(len={len(value)})"

def _normalize_config_value(key: str, value: str) -> str:
    if value is None:
        return ''
    value = str(value).strip()
    if not value:
        return ''
    if key.endswith('_url'):
        return value.rstrip('/')
    return value

def _safe_int(value, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    """安全转换整数并做边界裁剪"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if min_value is not None:
        number = max(min_value, number)
    if max_value is not None:
        number = min(max_value, number)
    return number

def _escape_fts_value(value: str) -> str:
    """
    转义 FTS5 查询值，避免引号导致语法错误。
    仅做最小转义，不改变词义。
    """
    if value is None:
        return ''
    return str(value).replace('"', '""').strip()


def _like_op() -> str:
    return "ILIKE" if is_postgres_backend() else "LIKE"


def _build_keyword_or_clause(alias: str, keywords: list, columns: list, like_op: str) -> tuple:
    return shared_build_keyword_or_clause(alias, keywords, columns, like_op)


def _internal_error(log_message: str, error: Exception):
    print(f"[Server] {log_message}: {error}")
    return jsonify({"success": False, "error": "服务器内部错误"}), 500


@app.before_request
def _acquire_db_lock_for_request():
    """
    SQLite 使用全局共享连接，需请求级串行化避免并发写冲突。
    PostgreSQL 使用线程隔离连接，不加全局锁以释放并发能力。
    """
    if is_postgres_backend():
        g._db_lock_acquired = False
        return
    _DB_ACCESS_LOCK.acquire()
    g._db_lock_acquired = True


@app.teardown_request
def _release_db_lock_for_request(_exc):
    if getattr(g, '_db_lock_acquired', False):
        _DB_ACCESS_LOCK.release()
    if is_postgres_backend():
        data_lake.release_thread_connection()

def _load_app_config() -> Dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _ensure_csrf_token() -> str:
    token = session.get('csrf_token')
    if token:
        return token
    token = secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return token


def _verify_csrf_token() -> Optional[tuple]:
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    # 仅对基于 Session 的请求校验 CSRF；API Key 认证由 _require_admin 分支处理。
    if not session.get('user_id'):
        return None
    expected_token = session.get('csrf_token', '')
    provided_token = request.headers.get('X-CSRF-Token', '')
    if not expected_token or not provided_token or not hmac.compare_digest(expected_token, provided_token):
        return jsonify({"success": False, "error": "CSRF 校验失败"}), 403
    return None


def _ensure_secret_key():
    config = _load_app_config()
    candidate = SECRET_KEY or config.get('secret_key', '')
    if _is_weak_secret(candidate):
        raise RuntimeError(
            "SECRET_KEY 未设置或过弱。请使用 >=32 位随机强密钥（例如: openssl rand -hex 32）。"
        )
    app.secret_key = candidate

def _init_auth_tables():
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TEXT,
            updated_at TEXT,
            last_login TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auth_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            ip TEXT,
            created_at TEXT
        )
    ''')
    # 系统配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')
    data_lake.conn.commit()

def _ensure_default_admin():
    cursor = data_lake.conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    if cursor.fetchone()[0] == 0:
        default_password = (os.getenv('ADMIN_DEFAULT_PASSWORD') or '').strip()
        if not default_password:
            raise RuntimeError(
                "首次启动必须提供 ADMIN_DEFAULT_PASSWORD。"
            )
        now = datetime.now().isoformat()
        try:
            cursor.execute('''
                INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, 'admin', ?, ?, ?)
            ''', ('admin', generate_password_hash(default_password), True, now, now))
            data_lake.conn.commit()
            print("[Auth] 已创建默认管理员 admin")
        except sqlite3.IntegrityError:
            # 并发情况下可能其他 worker 已创建
            data_lake.conn.rollback()
        except Exception as e:
            data_lake.conn.rollback()
            raise RuntimeError(f"创建默认管理员失败: {e}")

def _log_auth_action(user_id: Optional[int], action: str):
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        INSERT INTO auth_audit (user_id, action, ip, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, action, _get_client_ip(), datetime.now().isoformat()))
    data_lake.conn.commit()


def _get_client_ip() -> str:
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or request.remote_addr or 'unknown'


def _login_attempt_key(username: str) -> str:
    return f"{(username or '').strip().lower()}|{_get_client_ip()}"


def _is_login_rate_limited(username: str) -> int:
    key = _login_attempt_key(username)
    now = time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        entry = _LOGIN_ATTEMPTS.get(key)
        if not entry:
            return 0
        failures = [ts for ts in entry.get('failures', []) if now - ts <= LOGIN_RATE_LIMIT_WINDOW]
        entry['failures'] = failures
        locked_until = float(entry.get('locked_until', 0))
        if locked_until > now:
            _LOGIN_ATTEMPTS[key] = entry
            return int(locked_until - now) + 1
        if failures:
            _LOGIN_ATTEMPTS[key] = entry
        else:
            _LOGIN_ATTEMPTS.pop(key, None)
        return 0


def _record_login_failure(username: str):
    key = _login_attempt_key(username)
    now = time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        entry = _LOGIN_ATTEMPTS.get(key, {})
        failures = [ts for ts in entry.get('failures', []) if now - ts <= LOGIN_RATE_LIMIT_WINDOW]
        failures.append(now)
        locked_until = float(entry.get('locked_until', 0))
        if len(failures) >= LOGIN_RATE_LIMIT_MAX_FAILURES:
            locked_until = max(locked_until, now + LOGIN_LOCK_SECONDS)
        _LOGIN_ATTEMPTS[key] = {
            'failures': failures,
            'locked_until': locked_until
        }


def _clear_login_failures(username: str):
    key = _login_attempt_key(username)
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)

def _get_current_user() -> Optional[Dict]:
    user_id = session.get('user_id')
    if not user_id:
        return None
    # 确保 user_id 是整数类型
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        SELECT id, username, role, is_active, last_login
        FROM users WHERE id = ?
    ''', (user_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return {
        'id': row[0],
        'username': row[1],
        'role': row[2],
        'is_active': bool(row[3]),
        'last_login': row[4]
    }

def _get_admin_key_from_config() -> str:
    return _load_app_config().get('admin_api_key', '')

def _require_login() -> Optional[tuple]:
    user = _get_current_user()
    if not user or not user.get('is_active'):
        return jsonify({"success": False, "error": "未登录"}), 401
    csrf_error = _verify_csrf_token()
    if csrf_error:
        return csrf_error
    return None

def _require_admin() -> Optional[tuple]:
    user = _get_current_user()
    if user and user.get('is_active') and user.get('role') == 'admin':
        csrf_error = _verify_csrf_token()
        if csrf_error:
            return csrf_error
        return None

    api_key = ADMIN_API_KEY or _get_admin_key_from_config()
    if api_key:
        request_key = request.headers.get('X-Admin-Key', '')
        if request_key and hmac.compare_digest(request_key, api_key):
            return None

    return jsonify({"success": False, "error": "无权限访问"}), 403

def _check_embedding_rate_limit() -> Optional[tuple]:
    now = time.time()
    ip = request.remote_addr or 'unknown'
    records = _EMBEDDING_RATE_LIMIT.get(ip, [])
    records = [ts for ts in records if now - ts <= EMBEDDING_RATE_LIMIT_WINDOW]
    if len(records) >= EMBEDDING_RATE_LIMIT_MAX:
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429
    records.append(now)
    _EMBEDDING_RATE_LIMIT[ip] = records
    return None


@app.after_request
def _set_security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    )
    return resp

# 初始化认证配置
_ensure_secret_key()
_init_auth_tables()
_ensure_default_admin()

# ==========================================
# 静态文件服务
# ==========================================

@app.route('/')
def serve_index():
    """提供前端 HTML 页面 - 需要登录"""
    user = _get_current_user()
    if not user or not user.get('is_active'):
        return redirect('/login')
    return send_from_directory('.', 'udid_viewer.html')

@app.route('/login')
def serve_login():
    user = _get_current_user()
    if user and user.get('is_active'):
        # 根据角色重定向
        if user.get('role') == 'admin':
            return redirect('/admin')
        return redirect('/')
    return send_from_directory('.', 'login.html')

@app.route('/admin')
def serve_admin():
    user = _get_current_user()
    if not user or not user.get('is_active'):
        return redirect('/login')
    # 只有管理员可以访问后台
    if user.get('role') != 'admin':
        return redirect('/')
    return send_from_directory('.', 'admin.html')

@app.route('/admin.html')
def serve_admin_html():
    user = _get_current_user()
    if not user or not user.get('is_active'):
        return redirect('/login')
    if user.get('role') != 'admin':
        return redirect('/')
    return send_from_directory('.', 'admin.html')

@app.route('/login.html')
def serve_login_html():
    user = _get_current_user()
    if user and user.get('is_active'):
        if user.get('role') == 'admin':
            return redirect('/admin')
        return redirect('/')
    return send_from_directory('.', 'login.html')

# ==========================================
# API: 认证
# ==========================================

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    user = _get_current_user()
    if not user:
        return jsonify({"success": False, "error": "未登录"}), 401
    token = _ensure_csrf_token()
    return jsonify({"success": True, "data": {**user, "csrf_token": token}})

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    body = request.get_json(silent=True) or {}
    username = body.get('username', '').strip()
    password = body.get('password', '')
    if not username or not password:
        return jsonify({"success": False, "error": "请输入账号和密码"}), 400

    retry_after = _is_login_rate_limited(username)
    if retry_after > 0:
        resp = jsonify({"success": False, "error": "登录失败次数过多，请稍后再试"})
        resp.status_code = 429
        resp.headers['Retry-After'] = str(retry_after)
        return resp

    cursor = data_lake.conn.cursor()
    cursor.execute('''
        SELECT id, password_hash, role, is_active
        FROM users WHERE username = ?
    ''', (username,))
    row = cursor.fetchone()
    if not row:
        _log_auth_action(None, f"login_failed:{username}")
        _record_login_failure(username)
        return jsonify({"success": False, "error": "账号或密码错误"}), 401

    user_id, password_hash, role, is_active = row
    if not is_active or not check_password_hash(password_hash, password):
        _log_auth_action(user_id, "login_failed")
        _record_login_failure(username)
        return jsonify({"success": False, "error": "账号或密码错误"}), 401

    _clear_login_failures(username)
    session['user_id'] = user_id
    session['role'] = role
    session['csrf_token'] = secrets.token_urlsafe(32)
    cursor.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user_id))
    data_lake.conn.commit()
    _log_auth_action(user_id, "login")
    return jsonify({
        "success": True,
        "data": {
            "username": username,
            "role": role,
            "csrf_token": session['csrf_token']
        }
    })

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    auth_error = _require_login()
    if auth_error:
        return auth_error
    user = _get_current_user()
    session.clear()
    if user:
        _log_auth_action(user.get('id'), "logout")
    return jsonify({"success": True})

# ==========================================
# API: 管理员用户管理
# ==========================================

@app.route('/api/admin/users', methods=['GET'])
def list_users():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        SELECT id, username, role, is_active, created_at, updated_at, last_login
        FROM users ORDER BY id ASC
    ''')
    users = [
        {
            'id': row[0],
            'username': row[1],
            'role': row[2],
            'is_active': bool(row[3]),
            'created_at': row[4],
            'updated_at': row[5],
            'last_login': row[6]
        }
        for row in cursor.fetchall()
    ]
    return jsonify({"success": True, "data": users})

@app.route('/api/admin/users', methods=['POST'])
def create_user():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    body = request.get_json() or {}
    username = body.get('username', '').strip()
    password = body.get('password', '')
    role = body.get('role', 'user')
    if not username or not password:
        return jsonify({"success": False, "error": "账号和密码不能为空"}), 400
    if role not in ['admin', 'user']:
        return jsonify({"success": False, "error": "角色不合法"}), 400

    cursor = data_lake.conn.cursor()
    now = datetime.now().isoformat()
    try:
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (username, generate_password_hash(password), role, True, now, now))
        data_lake.conn.commit()
        _log_auth_action(session.get('user_id'), f"create_user:{username}")
        return jsonify({"success": True})
    except Exception as e:
        print(f"[Auth] 创建用户失败: {e}")
        return jsonify({"success": False, "error": "创建失败"}), 500

@app.route('/api/admin/users/<int:user_id>', methods=['PATCH'])
def update_user(user_id: int):
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    body = request.get_json() or {}
    fields = []
    params = []
    if 'role' in body:
        if body['role'] not in ['admin', 'user']:
            return jsonify({"success": False, "error": "角色不合法"}), 400
        fields.append('role = ?')
        params.append(body['role'])
    if 'is_active' in body:
        fields.append('is_active = ?')
        params.append(_to_bool(body.get('is_active'), default=False))
    if 'password' in body and body['password']:
        fields.append('password_hash = ?')
        params.append(generate_password_hash(body['password']))

    if not fields:
        return jsonify({"success": False, "error": "没有可更新字段"}), 400

    fields.append('updated_at = ?')
    params.append(datetime.now().isoformat())
    params.append(user_id)

    cursor = data_lake.conn.cursor()
    cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
    data_lake.conn.commit()
    _log_auth_action(session.get('user_id'), f"update_user:{user_id}")
    return jsonify({"success": True})

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id: int):
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    current_user_id = session.get('user_id')
    if current_user_id == user_id:
        return jsonify({"success": False, "error": "不能删除当前登录用户"}), 400

    cursor = data_lake.conn.cursor()
    cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
    data_lake.conn.commit()
    _log_auth_action(current_user_id, f"delete_user:{user_id}")
    return jsonify({"success": True})

@app.route('/api/admin/audit', methods=['GET'])
def list_auth_audit():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        SELECT id, user_id, action, ip, created_at
        FROM auth_audit
        ORDER BY id DESC
        LIMIT 200
    ''')
    rows = cursor.fetchall()
    data = [
        {
            'id': row[0],
            'user_id': row[1],
            'action': row[2],
            'ip': row[3],
            'created_at': row[4]
        }
        for row in rows
    ]
    return jsonify({"success": True, "data": data})

# ==========================================
# API: 数据库统计信息
# ==========================================

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """
    获取数据库统计信息（使用缓存加速）
    
    Returns:
        {
            "total_products": int,
            "last_sync": str (ISO 8601),
            "manufacturers_count": int
        }
    """
    try:
        cursor = data_lake.conn.cursor()
        
        # 尝试从缓存读取（极速）
        try:
            cursor.execute("SELECT value FROM stats_cache WHERE key = 'total_products'")
            total_products = cursor.fetchone()[0]
            
            cursor.execute("SELECT value FROM stats_cache WHERE key = 'manufacturers_count'")
            manufacturers_count = cursor.fetchone()[0]
        except:
            # 缓存不存在，使用慢速查询
            cursor.execute("SELECT COUNT(*) FROM products")
            total_products = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(DISTINCT manufacturer) FROM products")
            manufacturers_count = cursor.fetchone()[0]
        
        # 最后更新时间（有索引，速度快）
        cursor.execute("SELECT MAX(last_updated) FROM products")
        last_sync = cursor.fetchone()[0] or "从未同步"
        
        return jsonify({
            "success": True,
            "data": {
                "total_products": total_products,
                "manufacturers_count": manufacturers_count,
                "last_sync": last_sync
            }
        })
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 搜索
# ==========================================

@app.route('/api/search', methods=['GET'])
def search():
    """
    传统搜索（关键词+筛选条件）
    
    Query Parameters:
        keyword: str - 搜索关键词
        status: str - 状态筛选 (新增/更新)
        type: str - 类型筛选 (耗材/器械/体外诊断试剂)
        category_code: str - 分类编码
        manufacturer: str - 企业名称
        page: int - 页码 (默认 1)
        page_size: int - 每页数量 (默认 50)
    
    Returns:
        {
            "success": bool,
            "data": [...],
            "total": int,
            "page": int,
            "page_size": int
        }
    """
    auth_error = _require_login()
    if auth_error:
        return auth_error
    try:
        # 获取参数（兼容 keyword/q、page_size/limit）
        keyword = (request.args.get('keyword') or request.args.get('q') or '').strip()
        status = (request.args.get('status') or '').strip()
        product_type = (request.args.get('type') or '').strip()
        category_code = (request.args.get('category_code') or '').strip()
        manufacturer = (request.args.get('manufacturer') or '').strip()
        # 新增筛选参数
        cert_no = (request.args.get('cert_no') or '').strip()  # 注册证号
        model = (request.args.get('model') or '').strip()  # 规格型号
        commercial_name = (request.args.get('commercial_name') or '').strip()  # 商品名称
        date_from = (request.args.get('date_from') or '').strip()  # 发布日期起
        date_to = (request.args.get('date_to') or '').strip()  # 发布日期止

        page = _safe_int(request.args.get('page', 1), 1, min_value=1)
        page_size_arg = request.args.get('page_size')
        if page_size_arg is None:
            page_size_arg = request.args.get('limit', 50)
        page_size = _safe_int(page_size_arg, 50, min_value=1, max_value=200)
        
        # 构建查询
        like_op = _like_op()
        cursor = data_lake.conn.cursor()
        highlight_keywords = collect_highlight_keywords([
            keyword,
            manufacturer,
            model,
            commercial_name,
            cert_no,
        ])

        # PostgreSQL 走统一 ILIKE 分支；SQLite 保留 FTS5 快速路径用于回滚。
        has_fts = False
        if not is_postgres_backend():
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products_fts'")
            has_fts = cursor.fetchone() is not None
        
        if has_fts and (keyword or cert_no or model or commercial_name or manufacturer):
            # 智能加速模式：只要有文本查询条件，就使用 FTS5
            fts_parts = []
            
            # 1. 关键词（全字段匹配）
            if keyword:
                safe_keyword = _escape_fts_value(keyword)
                if safe_keyword:
                    fts_parts.append(f'"{safe_keyword}"')
            
            # 2. 特定字段匹配 (FTS 语法: column: query)
            if cert_no:
                safe_cert_no = _escape_fts_value(cert_no)
                if safe_cert_no:
                    fts_parts.append(f'cert_no: "{safe_cert_no}"')
            if model:
                safe_model = _escape_fts_value(model)
                if safe_model:
                    fts_parts.append(f'model: "{safe_model}"')
            if commercial_name:
                safe_commercial_name = _escape_fts_value(commercial_name)
                if safe_commercial_name:
                    fts_parts.append(f'commercial_name: "{safe_commercial_name}"')
            if manufacturer:
                safe_manufacturer = _escape_fts_value(manufacturer)
                if safe_manufacturer:
                    fts_parts.append(f'manufacturer: "{safe_manufacturer}"')
                
            # 构建 FTS 查询字符串 (AND 关系)
            if not fts_parts:
                return jsonify({"success": False, "error": "无效的检索条件"}), 400
            fts_query = " AND ".join(fts_parts)
            
            # 3. 结构化筛选条件 (SQL Filter)
            sql_conditions = []
            params = []
            
            if status:
                sql_conditions.append("p.status = ?")
                params.append(status)
            if product_type:
                sql_conditions.append("p.product_type = ?")
                params.append(product_type)
            if category_code:
                sql_conditions.append("p.category_code LIKE ?")
                params.append(f"{category_code}%")
            if date_from:
                sql_conditions.append("p.publish_date >= ?")
                params.append(date_from)
            if date_to:
                sql_conditions.append("p.publish_date <= ?")
                params.append(date_to)
            
            where_sql = " AND ".join(sql_conditions) if sql_conditions else "1=1"
            
            # 计数
            if where_sql == "1=1":
                # 只有 FTS 条件，直接查虚拟表（极速 0.005s）
                count_sql = "SELECT COUNT(*) FROM products_fts WHERE products_fts MATCH ?"
                cursor.execute(count_sql, [fts_query])
            else:
                # 混合条件，必须 JOIN (较慢 0.1s - 3s)
                count_sql = f"""
                    SELECT COUNT(*) FROM products p
                    INNER JOIN products_fts ON p.rowid = products_fts.rowid
                    WHERE products_fts MATCH ? AND {where_sql}
                """
                cursor.execute(count_sql, [fts_query] + params)
            total = cursor.fetchone()[0]
            
            # 分页查询（性能优化：先按 FTS rank 限定候选池，再按更新时间排序）
            offset = (page - 1) * page_size
            # 不再固定 5000 上限，避免深分页被截断导致“有 total 但翻页为空”
            fts_scan_limit = max(1000, offset + page_size * 20)

            query_sql = f"""
                WITH ranked_hits AS (
                    SELECT rowid, rank
                    FROM products_fts
                    WHERE products_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                )
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                       p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope
                FROM ranked_hits h
                INNER JOIN products p ON p.rowid = h.rowid
                WHERE {where_sql}
                ORDER BY p.last_updated DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query_sql, [fts_query, fts_scan_limit] + params + [page_size, offset])
            rows = cursor.fetchall()

            # 如果候选窗口过小导致分页不足，自动放大窗口重试一次
            if len(rows) < page_size and total > (offset + len(rows)):
                larger_scan_limit = max(fts_scan_limit * 2, offset + page_size * 40)
                cursor.execute(query_sql, [fts_query, larger_scan_limit] + params + [page_size, offset])
                rows = cursor.fetchall()
        else:
            # 普通 LIKE 搜索（无关键词或无 FTS）
            conditions = []
            params = []
            
            if keyword:
                if is_postgres_backend():
                    keyword_sql, keyword_params, _keyword_strategy = build_postgres_keyword_clause(
                        cursor=cursor,
                        alias='',
                        keyword=keyword,
                        like_op=like_op,
                    )
                    conditions.append(f"({keyword_sql})")
                    params.extend(keyword_params)
                else:
                    conditions.append(
                        f"(product_name {like_op} ? OR manufacturer {like_op} ? OR model {like_op} ? OR description {like_op} ?)"
                    )
                    keyword_pattern = f"%{keyword}%"
                    params.extend([keyword_pattern] * 4)
            
            if status:
                conditions.append("status = ?")
                params.append(status)
            
            if product_type:
                conditions.append("product_type = ?")
                params.append(product_type)
                
            if category_code:
                conditions.append(f"category_code {like_op} ?")
                params.append(f"{category_code}%")
                
            if manufacturer:
                conditions.append(f"manufacturer {like_op} ?")
                params.append(f"%{manufacturer}%")
            
            # 新增筛选条件
            if cert_no:
                conditions.append(f"cert_no {like_op} ?")
                params.append(f"%{cert_no}%")
            
            if model:
                conditions.append(f"model {like_op} ?")
                params.append(f"%{model}%")
            
            if commercial_name:
                conditions.append(f"commercial_name {like_op} ?")
                params.append(f"%{commercial_name}%")
            
            if date_from:
                conditions.append("publish_date >= ?")
                params.append(date_from)
            
            if date_to:
                conditions.append("publish_date <= ?")
                params.append(date_to)
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            # 获取总数（优化：无条件时使用缓存）
            if where_clause == "1=1":
                # 无筛选条件，使用缓存的总数
                try:
                    cursor.execute("SELECT value FROM stats_cache WHERE key = 'total_products'")
                    total = cursor.fetchone()[0]
                except:
                    cursor.execute("SELECT COUNT(*) FROM products")
                    total = cursor.fetchone()[0]
            else:
                # 有筛选条件，需要精确计算
                count_sql = f"SELECT COUNT(*) FROM products WHERE {where_clause}"
                cursor.execute(count_sql, params)
                total = cursor.fetchone()[0]
            
            # 获取分页数据
            offset = (page - 1) * page_size
            query_sql = f"""
                SELECT di_code, product_name, commercial_name, model, manufacturer, 
                       description, publish_date, source, last_updated, category_code, scope
                FROM products 
                WHERE {where_clause}
                ORDER BY last_updated DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query_sql, params + [page_size, offset])
            rows = cursor.fetchall()
        
        # 转换为字典列表
        columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                   'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
        data = [dict(zip(columns, row)) for row in rows]
        if highlight_keywords:
            for item in data:
                item['highlightKeywords'] = highlight_keywords
        
        return jsonify({
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": page_size
        })
        
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 上传 XML
# ==========================================

@app.route('/api/upload', methods=['POST'])
def upload_xml():
    """
    上传 XML 文件并导入数据库
    
    Args:
        file: XML 文件
    """
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "缺少文件字段 file"}), 400
        # 获取上传的文件
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "未选择文件"}), 400
        
        if not file.filename.endswith('.xml'):
            return jsonify({"success": False, "error": "请上传 XML 文件"}), 400
        
        # 保存到临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xml') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        try:
            # 导入数据
            result = data_lake.ingest_xml(tmp_path)
            # ingest_xml 返回 dict，提取统计信息
            if isinstance(result, dict):
                count = result.get('total', 0)
                return jsonify({
                    "success": True,
                    "message": f"成功导入 {count} 条记录（新增: {result.get('inserted', 0)}, 变更: {result.get('updated', 0)}, 跳过: {result.get('skipped', 0)}）",
                    "count": count,
                    "details": result
                })
            else:
                count = result if isinstance(result, int) else 0
                return jsonify({
                    "success": True,
                    "message": f"成功导入 {count} 条记录",
                    "count": count
                })
        finally:
            # 清理临时文件
            os.unlink(tmp_path)
            
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 手动同步
# ==========================================

@app.route('/api/sync', methods=['POST'])
def sync_data():
    """
    手动触发数据同步
    """
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        # 导入同步模块（延迟导入避免循环依赖）
        from udid_sync import sync_incremental
        print("[Admin] 开始同步数据...")
        result = sync_incremental(data_lake)
        print(f"[Admin] 同步完成: {result}")

        if not result.get("success", False):
            return jsonify({
                "success": False,
                "error": result.get("message", "同步失败"),
                "synced_days": result.get("synced_days", 0),
                "total_records": result.get("total_records", 0)
            }), 502

        return jsonify({
            "success": True,
            "message": result.get("message", "同步完成"),
            "synced_days": result.get("synced_days", 0),
            "total_records": result.get("total_records", 0)
        })
        
    except ImportError:
        return jsonify({
            "success": False, 
            "error": "同步模块尚未实现，请使用手动上传功能"
        }), 501
    except Exception as e:
        return _internal_error("接口处理失败", e)


SYNC_SERVER_BASE = os.getenv('SYNC_SERVER_BASE', 'http://127.0.0.1:8888').rstrip('/')


def _proxy_to_sync_server(path: str, method: str = 'GET'):
    """
    代理同步监控接口到独立 sync_server，保证前端 `/api/sync/*` 契约一致。
    """
    url = f"{SYNC_SERVER_BASE}{path}"
    headers = {}
    for key in ('X-API-Key', 'X-Timestamp', 'Content-Type'):
        value = request.headers.get(key)
        if value:
            headers[key] = value
    try:
        if method == 'POST':
            payload = request.get_json(silent=True)
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        else:
            response = requests.get(url, headers=headers, timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"success": False, "error": f"sync_server 响应非 JSON: {response.text[:200]}"}
        return jsonify(data), response.status_code
    except requests.RequestException as e:
        return jsonify({"success": False, "error": f"sync_server 不可用: {e}"}), 502


@app.route('/api/sync/start', methods=['POST'])
def sync_start_proxy():
    return _proxy_to_sync_server('/api/sync/start', method='POST')


@app.route('/api/sync/stop', methods=['POST'])
def sync_stop_proxy():
    return _proxy_to_sync_server('/api/sync/stop', method='POST')


@app.route('/api/sync/progress', methods=['GET'])
def sync_progress_proxy():
    return _proxy_to_sync_server('/api/sync/progress', method='GET')


@app.route('/api/sync/history', methods=['GET'])
def sync_history_proxy():
    return _proxy_to_sync_server('/api/sync/history', method='GET')


@app.route('/api/sync/status', methods=['GET'])
def sync_status_proxy():
    return _proxy_to_sync_server('/api/sync/status', method='GET')


@app.route('/api/sync/logs', methods=['GET'])
def sync_logs_proxy():
    return _proxy_to_sync_server('/api/sync/logs', method='GET')


@app.route('/api/sync/full', methods=['POST'])
def sync_full_proxy():
    return _proxy_to_sync_server('/api/sync/full', method='POST')


@app.route('/api/sync/data', methods=['POST'])
def sync_data_proxy():
    return _proxy_to_sync_server('/api/sync/data', method='POST')


@app.route('/api/sync/vectors', methods=['POST'])
def sync_vectors_proxy():
    return _proxy_to_sync_server('/api/sync/vectors', method='POST')

# ==========================================
# API: 纯算法智能匹配 (No-AI)
# ==========================================


# ==========================================
# 辅助函数: 结果去重
# ==========================================
def deduplicate_results(results: list) -> list:
    """
    按企业名称去重，保留排名最靠前的产品
    """
    seen_manufacturers = set()
    unique_results = []
    
    for item in results:
        mfr = item.get('manufacturer', '').strip()
        # 如果企业名为空，或者未出现过，则保留
        if not mfr or mfr not in seen_manufacturers:
            unique_results.append(item)
            if mfr:
                seen_manufacturers.add(mfr)
                
    return unique_results

@app.route('/api/algo-match', methods=['POST'])
def algo_match():
    """
    纯算法智能匹配 (Jieba分词 + BM25)
    """
    auth_error = _require_login()
    if auth_error:
        return auth_error
    try:
        import jieba.analyse
    except ImportError:
        return jsonify({"success": False, "error": "请先安装 jieba 库: pip install jieba"}), 501

    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"success": False, "error": "请求体必须为 JSON 对象"}), 400
        requirement = body.get('requirement', '')
        filters = body.get('filters', {})
        mode = body.get('mode', 'simple') # 'simple'(极速算法) 或 'semantic'(AI语义)
        
        if not requirement:
            return jsonify({"success": False, "error": "请输入需求描述"}), 400
            
        # 1. 提取关键词
        keywords = []
        
        if mode == 'semantic':
            try:
                from ai_service import expand_search_keywords
                # AI 可能会比较慢，但在接受范围内
                keywords = expand_search_keywords(requirement)
            except Exception as e:
                print(f"AI Keyword expansion failed: {e}")
                
            # 如果 AI 失败或没结果，自动降级为 Jieba
            if not keywords:
                keywords = jieba.analyse.extract_tags(requirement, topK=10)
        else:
            # 默认：纯本地算法
            keywords = jieba.analyse.extract_tags(requirement, topK=10)
            
        if not keywords:
            return jsonify({"success": False, "error": "无法提取有效关键词"}), 400
            
        # 2. 构建 FTS5 查询 (OR 关系，越多匹配越好)
        # 语法: "词1" OR "词2" OR "词3"
        fts_query = " OR ".join([f'"{k}"' for k in keywords])
        
        # 3. 构建过滤条件
        like_op = _like_op()
        params = []
        sql_conditions = []
        
        # keyword_filter 已移入 FTS，此处不再需要 LIKE 硬过滤，避免重复且 LIKE 效率低
        keyword_filter = filters.get('keyword', '')
        if keyword_filter:
            # 关键词过滤（来自搜索框），作为硬性约束 (LIKE)
            # 确保结果集与普通搜索一致
            sql_conditions.append(
                f"(p.product_name {like_op} ? OR p.manufacturer {like_op} ? OR p.model {like_op} ? OR p.description {like_op} ?)"
            )
            pattern = f"%{keyword_filter}%"
            params.extend([pattern, pattern, pattern, pattern])
        
        status = filters.get('status', '')
        product_type = filters.get('type', '')
        category_code = filters.get('category_code', '')
        manufacturer = filters.get('manufacturer', '')
        cert_no = filters.get('cert_no', '')
        model = filters.get('model', '')
        commercial_name = filters.get('commercial_name', '')
        date_from = filters.get('date_from', '')
        date_to = filters.get('date_to', '')
        # keyword_filter handled in FTS above

        if status:
            sql_conditions.append("p.status = ?")
            params.append(status)
        if product_type:
            sql_conditions.append("p.product_type = ?")
            params.append(product_type)
        if category_code:
            sql_conditions.append(f"p.category_code {like_op} ?")
            params.append(f"{category_code}%")
        if manufacturer:
            sql_conditions.append(f"p.manufacturer {like_op} ?")
            params.append(f"%{manufacturer}%")
        if cert_no:
            sql_conditions.append(f"p.cert_no {like_op} ?")
            params.append(f"%{cert_no}%")
        if model:
            sql_conditions.append(f"p.model {like_op} ?")
            params.append(f"%{model}%")
        if commercial_name:
            sql_conditions.append(f"p.commercial_name {like_op} ?")
            params.append(f"%{commercial_name}%")
        if date_from:
            sql_conditions.append("p.publish_date >= ?")
            params.append(date_from)
        if date_to:
            sql_conditions.append("p.publish_date <= ?")
            params.append(date_to)
            
        where_sql = " AND ".join(sql_conditions) if sql_conditions else "1=1"
        
        # 4. 执行召回查询
        cursor = data_lake.conn.cursor()
        if is_postgres_backend():
            keyword_sql, keyword_params = _build_keyword_or_clause(
                alias='p',
                keywords=keywords,
                columns=['product_name', 'commercial_name', 'model', 'manufacturer', 'description', 'cert_no'],
                like_op=like_op
            )
            query_sql = f"""
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer,
                       p.description, p.publish_date, p.source, p.last_updated, p.category_code
                FROM products p
                WHERE ({keyword_sql}) AND {where_sql}
                ORDER BY p.last_updated DESC
                LIMIT 50
            """
            cursor.execute(query_sql, keyword_params + params)
            rows = cursor.fetchall()
        else:
            # SQLite: 保留 FTS5 BM25 快速路径
            query_sql = f"""
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                       p.description, p.publish_date, p.source, p.last_updated, p.category_code
                FROM products p
                INNER JOIN products_fts ON p.rowid = products_fts.rowid
                WHERE products_fts MATCH ? AND {where_sql}
                ORDER BY products_fts.rank
                LIMIT 50
            """
            cursor.execute(query_sql, [fts_query] + params)
            rows = cursor.fetchall()
        
        # 5. 零结果回退机制 (Fallback)
        # 如果由 Requirement + Filter 组合查询没有结果，但 Filter 本身有结果
        # 为了避免用户看到“0条结果”的困惑，我们回退到仅显示 Filter 的结果，但给低分
        if not rows and where_sql != "1=1":
            print("[AlgoMatch] No FTS match found, falling back to Filter-only search.")
            fallback_sql = f"""
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                       p.description, p.publish_date, p.source, p.last_updated, p.category_code
                FROM products p
                WHERE {where_sql}
                ORDER BY p.last_updated DESC
                LIMIT 50
            """
            cursor.execute(fallback_sql, params)
            rows = cursor.fetchall()
            
            # 标记为回退模式，后续评分逻辑会处理
            is_fallback = True
        else:
            is_fallback = False
        
        columns = [column[0] for column in cursor.description]
        results = []
        
        # 计算算法置信度分 (基于位置的归一化，范围 60-80)
        # 不再使用伪造的 99 分，而是诚实地表示这是一个算法匹配结果
        for i, row in enumerate(rows):
            item = dict(zip(columns, row))
            
            if is_fallback:
                # 回退模式：0 分
                match_score = 0
                match_reason = "未找到语义匹配项，仅展示基础筛选结果"
            else:
                # Position-based decay: Rank 1 = 80, Rank 50 ~= 60
                # 这是一个“算法中等置信度”区间，留出 >80 分给 AI 深度匹配
                match_score = int(max(60, 80 - (i * 0.4)))
                
                # 高亮匹配词
                matched_keywords = [k for k in keywords if k in str(item.values())]
                match_reason = f"算法关键词快速匹配: {', '.join(matched_keywords)}" if matched_keywords else "算法模糊匹配"
            
            item['matchScore'] = match_score
            item['matchReason'] = match_reason
            results.append(item)
            
            # 按企业去重 (Moved to Frontend)
        # results = deduplicate_results(results)
            
        return jsonify({
            "success": True, 
            "data": results,
            "keywords": keywords,  # 返回提取的关键词供调试
            "count": len(results)
        })

    except Exception as e:
        return _internal_error("接口处理失败", e)

@app.route('/api/ai-match', methods=['POST'])
def ai_match():
    """
    AI 语义匹配排序
    
    Body: JSON
        {
            "requirement": str - 用户需求描述
            "filters": object - 筛选条件（可选）
            "use_vector": bool - 是否使用向量检索（可选，默认 True）
        }
    
    Returns:
        {
            "success": bool,
            "data": [产品列表，带有 matchScore 和 matchReason 字段]
        }
    """
    auth_error = _require_login()
    if auth_error:
        return auth_error
    try:
        body = request.get_json(silent=True) or {}
        requirement = (body.get('requirement') or '').strip()  # 参数需求
        product_name = (body.get('product_name') or '').strip()  # 产品名称
        specs = (body.get('specs') or '').strip()  # 规格型号
        filters = body.get('filters', {})
        if not isinstance(filters, dict):
            filters = {}

        use_vector = bool(body.get('use_vector', True))
        page = _safe_int(body.get('page', 1), 1, min_value=1)
        page_size = _safe_int(body.get('page_size', 50), 50, min_value=1, max_value=1000)
        min_score = _safe_int(body.get('min_score', 0), 0, min_value=0, max_value=100)
        recall_k = min(1000, max(50, page * page_size))

        # 至少需要产品名称或参数需求
        if not requirement and not product_name:
            return jsonify({"success": False, "error": "请输入产品名称或参数需求"}), 400

        # 准备筛选条件
        keyword = (filters.get('keyword') or '').strip()
        status = (filters.get('status') or '').strip()
        product_type = (filters.get('type') or '').strip()
        category_code = (filters.get('category_code') or '').strip()
        manufacturer = (filters.get('manufacturer') or '').strip()
        cert_no = (filters.get('cert_no') or '').strip()
        model = (filters.get('model') or '').strip()
        commercial_name = (filters.get('commercial_name') or '').strip()
        date_from = (filters.get('date_from') or '').strip()
        date_to = (filters.get('date_to') or '').strip()
        like_op = _like_op()
        highlight_keywords = collect_highlight_keywords([keyword, product_name, specs, requirement])

        candidates = []
        recall_method = "vector"

        # -------------------------------------------------------
        # 两阶段检索：产品名称过滤 + 参数需求排序
        # -------------------------------------------------------
        if use_vector:
            try:
                from embedding_service import hybrid_search

                vector_filters = {
                    'keyword': keyword,
                    'status': status,
                    'type': product_type,
                    'category_code': category_code,
                    'manufacturer': manufacturer,
                    'cert_no': cert_no,
                    'model': model,
                    'commercial_name': commercial_name,
                    'date_from': date_from,
                    'date_to': date_to,
                }
                vector_filters = {k: v for k, v in vector_filters.items() if v}

                vector_payload = hybrid_search(
                    query=requirement,  # 参数需求（用于向量排序）
                    conn=data_lake.conn,
                    top_k=recall_k,
                    filters=vector_filters if vector_filters else None,
                    product_name=product_name,  # 产品名称（用于过滤）
                    specs=specs,  # 规格型号（用于加权）
                    min_score=min_score,  # 最低匹配分
                    return_metadata=True,
                )

                vector_results = vector_payload.get('results') if isinstance(vector_payload, dict) else vector_payload
                vector_method = vector_payload.get('recall_method') if isinstance(vector_payload, dict) else None
                if vector_results:
                    candidates = vector_results
                    recall_method = vector_method or "vector"
                    print(f"[AiMatch] 两阶段检索召回 {len(candidates)} 个候选，method={recall_method}")
            except ImportError:
                print("[AiMatch] 向量服务未安装，使用 FTS 召回")
            except Exception as e:
                print(f"[AiMatch] 向量检索失败: {e}，降级到 FTS")

        # -------------------------------------------------------
        # 降级：使用 FTS 关键词检索
        # -------------------------------------------------------
        if not candidates:
            query_text = requirement or product_name or keyword

            # 提取关键词（优先本地算法，避免网络抖动影响检索速度）
            keywords = []
            if query_text:
                try:
                    import jieba.analyse
                    keywords = jieba.analyse.extract_tags(query_text, topK=5)
                except Exception:
                    keywords = []

            # 仅在需要时做 AI 扩展（避免每次回退都走远程 API）
            if query_text and use_vector and len(keywords) < 2:
                try:
                    from ai_service import expand_search_keywords
                    ai_keywords = expand_search_keywords(query_text)
                    for kw in ai_keywords:
                        if kw and kw not in keywords:
                            keywords.append(kw)
                    keywords = keywords[:8]
                except Exception as e:
                    print(f"[AiMatch] AI 关键词扩展失败: {e}")

            if not keywords and query_text:
                keywords = [query_text[:20]]

            safe_keywords = [_escape_fts_value(k) for k in keywords if _escape_fts_value(k)]
            if not safe_keywords:
                return jsonify({"success": False, "error": "无法提取有效关键词"}), 400

            fts_query = " OR ".join([f'"{k}"' for k in safe_keywords])

            cursor = data_lake.conn.cursor()
            sql_conditions = []
            params = []
            if keyword:
                if is_postgres_backend():
                    filter_keyword_sql, filter_keyword_params, _filter_keyword_strategy = build_postgres_keyword_clause(
                        cursor=cursor,
                        alias='p',
                        keyword=keyword,
                        like_op=like_op,
                    )
                    sql_conditions.append(f"({filter_keyword_sql})")
                    params.extend(filter_keyword_params)
                else:
                    sql_conditions.append(
                        f"(p.product_name {like_op} ? OR p.manufacturer {like_op} ? OR p.model {like_op} ? OR p.description {like_op} ?)"
                    )
                    keyword_pattern = f"%{keyword}%"
                    params.extend([keyword_pattern] * 4)
            if status:
                sql_conditions.append("p.status = ?")
                params.append(status)
            if product_type:
                sql_conditions.append("p.product_type = ?")
                params.append(product_type)
            if category_code:
                sql_conditions.append(f"p.category_code {like_op} ?")
                params.append(f"{category_code}%")
            if manufacturer:
                sql_conditions.append(f"p.manufacturer {like_op} ?")
                params.append(f"%{manufacturer}%")
            if cert_no:
                sql_conditions.append(f"p.cert_no {like_op} ?")
                params.append(f"%{cert_no}%")
            if model:
                sql_conditions.append(f"p.model {like_op} ?")
                params.append(f"%{model}%")
            if commercial_name:
                sql_conditions.append(f"p.commercial_name {like_op} ?")
                params.append(f"%{commercial_name}%")
            if date_from:
                sql_conditions.append("p.publish_date >= ?")
                params.append(date_from)
            if date_to:
                sql_conditions.append("p.publish_date <= ?")
                params.append(date_to)
            where_clause = " AND ".join(sql_conditions) if sql_conditions else "1=1"
            highlight_keywords = collect_highlight_keywords(safe_keywords + [keyword, product_name, specs])

            if is_postgres_backend():
                keyword_sql, keyword_params, _keyword_strategies = build_postgres_keywords_clause(
                    cursor=cursor,
                    alias='p',
                    keywords=safe_keywords,
                    like_op=like_op,
                )
                query_sql = f"""
                    SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer,
                           p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope
                    FROM products p
                    WHERE ({keyword_sql}) AND {where_clause}
                    ORDER BY p.last_updated DESC
                    LIMIT ?
                """
                cursor.execute(query_sql, keyword_params + params + [recall_k])
            else:
                query_sql = (
                    "SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, "
                    "p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope "
                    "FROM products p "
                    "INNER JOIN products_fts ON p.rowid = products_fts.rowid "
                    f"WHERE products_fts MATCH ? AND {where_clause} "
                    "ORDER BY products_fts.rank "
                    "LIMIT ?"
                )
                params_final = [fts_query] + params + [recall_k]
                cursor.execute(query_sql, params_final)
            rows = cursor.fetchall()

            # 零结果回退：仅按筛选条件返回（保障可用性）
            if not rows and where_clause != "1=1":
                print("[AiMatch] No FTS match found, falling back to Filter-only candidates.")
                fallback_sql = f"""
                    SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                           p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope
                    FROM products p
                    WHERE {where_clause}
                    ORDER BY p.last_updated DESC
                    LIMIT ?
                """
                cursor.execute(fallback_sql, params + [recall_k])
                rows = cursor.fetchall()

            columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                       'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
            candidates = [dict(zip(columns, row)) for row in rows]
            recall_method = "fts"
            print(f"[AiMatch] FTS/关键词 检索召回 {len(candidates)} 个候选")

        if not candidates:
            return jsonify({
                "success": False,
                "error": "没有符合筛选条件的候选产品"
            }), 400

        # FTS / 混合回退结果需要补充统一字段
        if recall_method != "vector":
            for i, item in enumerate(candidates):
                if 'matchScore' not in item:
                    # 基于排名的分数：第1名=85，第50名=55
                    item['matchScore'] = max(55, 85 - i)
                if 'highlightKeywords' not in item and highlight_keywords:
                    item['highlightKeywords'] = highlight_keywords

        # 最低分过滤（向量结果通常已在 hybrid_search 内过滤，这里做兜底）
        if min_score > 0:
            candidates = [item for item in candidates if item.get('matchScore', 0) >= min_score]

        if not candidates:
            return jsonify({
                "success": False,
                "error": f"没有达到最低匹配分数（{min_score}%）的候选产品"
            }), 400

        filtered_total = len(candidates)
        offset = (page - 1) * page_size
        paged_candidates = candidates[offset: offset + page_size]

        return jsonify({
            "success": True,
            "data": paged_candidates,
            "total": filtered_total,
            "filtered_total": filtered_total,
            "page": page,
            "page_size": page_size,
            "method": recall_method
        })

    except ImportError:
        return jsonify({"success": False, "error": "向量服务模块不可用"}), 500
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 招标信息智能解析
# ==========================================

def _build_ai_error_message(ai_err: dict, default_error: str) -> str:
    """将 ai_service 的错误快照映射为可读业务错误信息。"""
    err_type = (ai_err or {}).get('type')
    status_code = (ai_err or {}).get('status_code')
    provider = (ai_err or {}).get('provider') or '上游服务'

    if err_type == 'ssl_error':
        return f"AI 服务 TLS 握手失败（{provider}），请稍后重试或更换可用网关。"
    if err_type == 'timeout':
        return f"AI 服务请求超时（{provider}），请稍后重试。"
    if err_type == 'auth_error' or status_code == 401:
        return f"AI 鉴权失败（{provider}），请检查 API Key 是否有效。"
    if err_type == 'rate_limit' or status_code == 429:
        return f"AI 调用频率受限（{provider}），请稍后重试。"
    if err_type == 'upstream_unavailable':
        return f"AI 上游暂不可用（{provider}），请稍后重试。"
    return default_error


@app.route('/api/parse-bid', methods=['POST'])
def parse_bid():
    """
    AI 解析招标信息，提取产品名称、规格型号、参数需求
    
    Body: JSON
        {
            "bid_text": str - 招标文本描述
        }
    
    Returns:
        {
            "success": bool,
            "data": {
                "product_name": str,
                "specs": str,
                "requirement": str
            }
        }
    """
    auth_error = _require_login()
    if auth_error:
        return auth_error
    try:
        body = request.get_json(silent=True) or {}
        bid_text = (body.get('bid_text', '') or '').strip()
        
        if not bid_text:
            return jsonify({"success": False, "error": "请输入招标信息"}), 400
        
        if len(bid_text) > 5000:
            bid_text = bid_text[:5000]
        
        from ai_service import call_ai_api, load_config as load_ai_config, sanitize_user_input, get_last_ai_error
        
        config = load_ai_config()
        if not config.get('api_key'):
            return jsonify({"success": False, "error": "AI 服务未配置，请先在管理后台设置 API Key"}), 400
        
        safe_text = sanitize_user_input(bid_text)
        
        prompt = f"""你是医疗器械采购专家。请从以下招标/采购描述中提取结构化信息。

招标描述：
"{safe_text}"

请严格按照以下 JSON 格式返回（不要 Markdown 标记）：
{{
  "product_name": "产品名称（通用名，如：负压引流器、心电监护仪）",
  "specs": "规格型号（如有，如：双瓶2000ml、12导联）",
  "requirement": "功能/用途/参数需求（如：用于手术废液及痰液收集）"
}}

规则：
1. product_name 必须是标准医疗器械通用名称，去掉品牌和厂家信息
2. 如果包含多个产品，只提取第一个主要产品
3. specs 没有明确规格时留空字符串
4. requirement 提取用途、功能要求、技术参数等"""

        response = call_ai_api(prompt, config)
        
        if not response:
            ai_err = get_last_ai_error()
            return jsonify({
                "success": False,
                "error": _build_ai_error_message(ai_err, "AI 调用失败，请检查 API Key / 模型配置后重试")
            }), 502
        
        # 解析 AI 返回的 JSON
        import json as json_module
        import re
        
        # 清理可能的 markdown 标记
        clean_text = response.replace('```json', '').replace('```', '').strip()
        
        # 尝试提取 JSON 对象
        json_match = re.search(r'\{[\s\S]*\}', clean_text)
        if json_match:
            clean_text = json_match.group()
        
        try:
            parsed = json_module.loads(clean_text)
        except json_module.JSONDecodeError:
            print(f"[ParseBid] AI 返回解析失败: {response[:200]}")
            return jsonify({"success": False, "error": "AI 返回格式异常，请重试"}), 502
        
        result = {
            "product_name": str(parsed.get('product_name', '')).strip(),
            "specs": str(parsed.get('specs', '')).strip(),
            "requirement": str(parsed.get('requirement', '')).strip()
        }
        
        if not result['product_name']:
            return jsonify({"success": False, "error": "未能从文本中识别出产品名称"}), 400
        
        print(f"[ParseBid] 解析成功: {result['product_name']} | {result['specs']} | {result['requirement'][:30]}...")
        
        return jsonify({
            "success": True,
            "data": result
        })
        
    except ImportError:
        return jsonify({"success": False, "error": "AI 服务模块不可用"}), 500
    except Exception as e:
        print(f"[ParseBid] 异常: {e}")
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 测试 AI 连通性
# ==========================================
@app.route('/api/test-ai', methods=['POST'])
def test_ai():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        from ai_service import call_ai_api, load_config as load_ai_config, get_last_ai_error
        import time
        
        body = request.get_json() or {}
        
        # 读取“有效配置”（文件/数据库/环境变量）
        config = load_ai_config()
        
        # 如果前端传了专用配置就用，覆盖数据库配置
        if body.get('api_base_url'):
            config['api_base_url'] = body['api_base_url']
        if body.get('api_key'):
            config['api_key'] = body['api_key']
        if body.get('model'):
            config['model'] = body['model']

        config['api_base_url'] = _normalize_config_value('api_base_url', config.get('api_base_url', ''))
        config['model'] = _normalize_config_value('model', config.get('model', ''))

        if not config.get('api_base_url') or not config.get('api_key') or not config.get('model'):
            return jsonify({
                "success": False,
                "error": "AI 配置不完整，请检查 Base URL / API Key / 模型名称",
                "debug": {
                    "api_base_url": config.get('api_base_url', ''),
                    "model": config.get('model', ''),
                    "has_api_key": bool(config.get('api_key'))
                }
            }), 400

        # 简单测试
        start = time.time()
        res = call_ai_api("Test connection. Reply 'OK'.", config)
        latency = (time.time() - start) * 1000
        
        if res:
            return jsonify({
                "success": True, 
                "message": f"连接成功! (延迟: {latency:.0f}ms) Base: {config.get('api_base_url')} 模型: {config.get('model')} 响应: {res[:20]}..."
            })
        else:
            ai_err = get_last_ai_error()
            return jsonify({
                "success": False, 
                "error": _build_ai_error_message(ai_err, "连接失败: 无响应或认证错误"),
                "debug": {
                    "api_base_url": config.get('api_base_url', ''),
                    "model": config.get('model', ''),
                    "api_key": _mask_secret(config.get('api_key', '')),
                    "provider_status_code": ai_err.get('status_code'),
                    "provider_error_type": ai_err.get('type')
                }
            }), 502
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 获取产品规格列表
# ==========================================

@app.route('/api/product-specs', methods=['GET'])
def get_product_specs():
    """
    获取指定厂家+产品名的所有规格型号
    
    Query Parameters:
        manufacturer: str - 厂家名称
        product_name: str - 产品名称
    
    Returns:
        {
            "success": bool,
            "data": [{"model": "规格1"}, {"model": "规格2"}, ...]
        }
    """
    auth_error = _require_login()
    if auth_error:
        return auth_error
    try:
        manufacturer = request.args.get('manufacturer', '')
        product_name = request.args.get('product_name', '')
        
        if not manufacturer or not product_name:
            return jsonify({"success": False, "error": "缺少参数"}), 400
        
        cursor = data_lake.conn.cursor()
        cursor.execute('''
            SELECT DISTINCT model 
            FROM products 
            WHERE manufacturer = ? AND product_name = ?
            ORDER BY model
            LIMIT 100
        ''', (manufacturer, product_name))
        
        specs = [row[0] for row in cursor.fetchall() if row[0]]
        
        return jsonify({
            "success": True,
            "data": specs,
            "count": len(specs)
        })
        
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 向量索引管理
# ==========================================

# 全局变量：记录构建进度
_embedding_build_progress = {
    'running': False,
    'step': '',
    'current': 0,
    'total': 0,
    'mode': '',  # 'single' or 'batch'
    'batch_id': None,
    'error': None,
    'start_time': None
}
_EMBEDDING_BUILD_LOCK = threading.RLock()

@app.route('/api/embedding/build', methods=['POST'])
def build_embedding():
    """
    智能构建向量索引（自动选择单条或批处理模式）
    - 待处理 < 500：逐条处理（实时完成）
    - 待处理 >= 500：批处理模式（后台异步）
    """
    global _embedding_build_progress
    
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    rate_error = _check_embedding_rate_limit()
    if rate_error:
        return rate_error

    with _EMBEDDING_BUILD_LOCK:
        if _embedding_build_progress['running']:
            return jsonify({
                "success": False,
                "error": "已有构建任务在运行中"
            }), 409

    try:
        import time
        from embedding_service import build_embeddings, init_embedding_table
        
        body = request.get_json() or {}
        force = body.get('force', False)
        skip_import_check = body.get('skip_import_check', False)
        
        # 初始化表
        init_embedding_table(data_lake.conn)
        cursor = data_lake.conn.cursor()
        
        # ========== 智能检测：先检查是否有已完成的待导入任务 ==========
        if not skip_import_check:
            try:
                from embedding_batch import check_and_import_completed_tasks
                print(f"[Admin] 智能检测：检查是否有待导入的批处理任务...")
                
                import_result = check_and_import_completed_tasks(data_lake.conn)
                
                if import_result['imported'] > 0:
                    # 有已完成的任务被导入
                    print(f"[Admin] ✓ 自动导入了 {import_result['imported']} 条向量")
                    
                    # 重新计算待处理数量
                    cursor.execute('SELECT COUNT(*) FROM products')
                    total_products = cursor.fetchone()[0]
                    cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
                    total_embeddings = cursor.fetchone()[0]
                    new_pending = max(0, total_products - total_embeddings)
                    
                    return jsonify({
                        "success": True,
                        "message": f"已自动导入 {import_result['imported']} 条向量！剩余待处理: {new_pending} 条",
                        "data": {
                            "mode": "auto_import",
                            "imported": import_result['imported'],
                            "pending": new_pending,
                            "tasks": import_result['tasks']
                        }
                    })
                
                # 检查是否有进行中的任务
                in_progress = [t for t in import_result.get('tasks', []) if t.get('success') is None]
                if in_progress:
                    task = in_progress[0]
                    print(f"[Admin] ⏳ 有任务正在处理中: {task['batch_id']}")
                    with _EMBEDDING_BUILD_LOCK:
                        _embedding_build_progress['batch_id'] = task['batch_id']
                        _embedding_build_progress['running'] = True
                        _embedding_build_progress['mode'] = 'batch'
                        _embedding_build_progress['step'] = '有任务正在云端处理中...'
                        _embedding_build_progress['total'] = task.get('count', 0)
                    
                    return jsonify({
                        "success": True,
                        "message": f"有任务正在处理中，请稍后再试",
                        "data": {
                            "mode": "in_progress",
                            "batch_id": task['batch_id'],
                            "count": task.get('count', 0),
                            "created_at": task.get('created_at')
                        }
                    })
                    
            except Exception as e:
                print(f"[Admin] 智能检测失败（继续正常流程）: {e}")
        
        # ========== 正常流程：计算待处理数量 ==========
        cursor.execute('SELECT COUNT(*) FROM products')
        total_products = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
        total_embeddings = cursor.fetchone()[0]
        
        pending = max(0, total_products - total_embeddings)
        
        print(f"[Admin] 待处理向量: 约 {pending} 条 (产品 {total_products}, 已有向量 {total_embeddings})")
        
        if pending == 0:
            return jsonify({
                "success": True,
                "message": "所有产品已有最新向量，无需处理",
                "data": {"pending": 0, "mode": "none"}
            })
        
        # 自动选择处理模式（从配置读取阈值，默认 500）
        config = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        batch_threshold = config.get('batch_threshold', 500)
        
        if pending < batch_threshold:
            # 逐条处理模式（实时）
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress = {
                    'running': True,
                    'step': '正在生成向量...',
                    'current': 0,
                    'total': pending,
                    'mode': 'single',
                    'batch_id': None,
                    'error': None,
                    'start_time': time.time()
                }
            
            print(f"[Admin] 使用逐条模式处理 {pending} 条")
            result = build_embeddings(data_lake.conn, force=force)
            
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['running'] = False
                _embedding_build_progress['step'] = '完成'
                _embedding_build_progress['current'] = result['processed']
                _embedding_build_progress['error'] = None if result.get('success') else '部分或全部向量生成失败'
            
            message = (
                f"已完成！处理 {result['processed']} 条，跳过 {result['skipped']} 条"
                if result.get('success')
                else f"处理失败：成功 {result['processed']} 条，失败 {result.get('failed', 0)} 条"
            )
            return jsonify({
                "success": result['success'],
                "message": message,
                "data": {**result, "mode": "single", "pending": pending}
            })
        else:
            # 批处理模式（异步）
            from embedding_batch import generate_jsonl, upload_file, create_batch, save_batch_task
            
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress = {
                    'running': True,
                    'step': '准备数据...',
                    'current': 0,
                    'total': pending,
                    'mode': 'batch',
                    'batch_id': None,
                    'error': None,
                    'start_time': time.time()
                }
            
            print(f"[Admin] 使用批处理模式处理 {pending} 条")
            
            # Step 1: 生成 JSONL
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['step'] = '正在准备数据文件...'
            result = generate_jsonl(data_lake.conn, incremental=True)
            
            if result['count'] == 0:
                with _EMBEDDING_BUILD_LOCK:
                    _embedding_build_progress['running'] = False
                return jsonify({
                    "success": True,
                    "message": "所有产品已有最新向量",
                    "data": {"pending": 0, "mode": "batch"}
                })
            
            # Step 2: 上传文件
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['step'] = '正在上传数据...'
                _embedding_build_progress['current'] = 1
            file_id = upload_file(result['file_path'])
            if not file_id:
                with _EMBEDDING_BUILD_LOCK:
                    _embedding_build_progress['running'] = False
                    _embedding_build_progress['error'] = '上传失败'
                return jsonify({"success": False, "error": "上传数据失败"}), 500
            
            # Step 3: 创建批处理任务
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['step'] = '正在创建处理任务...'
                _embedding_build_progress['current'] = 2
            batch_id = create_batch(file_id)
            if not batch_id:
                with _EMBEDDING_BUILD_LOCK:
                    _embedding_build_progress['running'] = False
                    _embedding_build_progress['error'] = '创建任务失败'
                return jsonify({"success": False, "error": "创建批处理任务失败"}), 500
            
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['batch_id'] = batch_id
                _embedding_build_progress['step'] = '后台处理中，请稍后查看进度...'
                _embedding_build_progress['current'] = 3
            
            # 保存任务记录（用于智能检测）
            save_batch_task(batch_id, result['count'])
            
            # 预估时间（约 1000 条/分钟）
            est_minutes = max(1, pending // 1000)
            
            return jsonify({
                "success": True,
                "message": f"已提交 {result['count']} 条数据，预计 {est_minutes} 分钟完成",
                "data": {
                    "mode": "batch",
                    "pending": pending,
                    "count": result['count'],
                    "batch_id": batch_id,
                    "estimated_minutes": est_minutes
                }
            })
        
    except ImportError as e:
        with _EMBEDDING_BUILD_LOCK:
            _embedding_build_progress['running'] = False
        return jsonify({
            "success": False,
            "error": f"模块未安装: {e}"
        }), 501
    except Exception as e:
        with _EMBEDDING_BUILD_LOCK:
            _embedding_build_progress['running'] = False
            _embedding_build_progress['error'] = str(e)
        import traceback
        traceback.print_exc()
        return _internal_error("接口处理失败", e)

@app.route('/api/embedding/progress', methods=['GET'])
def embedding_progress():
    """获取向量构建进度"""
    global _embedding_build_progress
    
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    import time
    with _EMBEDDING_BUILD_LOCK:
        progress = _embedding_build_progress.copy()
    
    # 如果是批处理模式且正在运行，检查状态
    if progress['running'] and progress['mode'] == 'batch' and progress['batch_id']:
        try:
            from embedding_batch import check_batch_status
            status = check_batch_status(progress['batch_id'])
            if status:
                batch_status = status.get('status', 'unknown')
                if batch_status == 'completed':
                    progress['step'] = '处理完成，可以导入结果'
                    progress['current'] = progress['total']
                elif batch_status == 'failed':
                    progress['step'] = '处理失败'
                    progress['error'] = status.get('error', '未知错误')
                    progress['running'] = False
                    with _EMBEDDING_BUILD_LOCK:
                        _embedding_build_progress['step'] = progress['step']
                        _embedding_build_progress['error'] = progress['error']
                        _embedding_build_progress['running'] = False
                elif batch_status in ('validating', 'in_progress'):
                    # 计算预估进度
                    elapsed = time.time() - (progress['start_time'] or time.time())
                    est_total = max(60, progress['total'] / 1000 * 60)  # 预估总时间
                    est_progress = min(95, int(elapsed / est_total * 100))
                    progress['step'] = f'正在处理中 ({est_progress}%)...'
                    progress['current'] = int(progress['total'] * est_progress / 100)
        except Exception as e:
            print(f"[Admin] 检查批处理状态失败: {e}")
    
    # 计算已用时间
    if progress['start_time']:
        elapsed = int(time.time() - progress['start_time'])
        progress['elapsed_seconds'] = elapsed
        progress['elapsed_display'] = f"{elapsed // 60}分{elapsed % 60}秒"
    
    return jsonify({"success": True, "data": progress})

@app.route('/api/embedding/import', methods=['POST'])
def embedding_import():
    """导入批处理结果"""
    global _embedding_build_progress
    
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    try:
        from embedding_batch import download_results, import_results
        
        with _EMBEDDING_BUILD_LOCK:
            batch_id = _embedding_build_progress.get('batch_id')
        if not batch_id:
            body = request.get_json() or {}
            batch_id = body.get('batch_id')
        
        if not batch_id:
            return jsonify({"success": False, "error": "没有待导入的任务"}), 400
        
        with _EMBEDDING_BUILD_LOCK:
            _embedding_build_progress['step'] = '正在下载结果...'
        result_file = download_results(batch_id)
        if not result_file:
            return jsonify({"success": False, "error": "下载结果失败，任务可能未完成"}), 400
        
        with _EMBEDDING_BUILD_LOCK:
            _embedding_build_progress['step'] = '正在导入数据库...'
        result = import_results(result_file, data_lake.conn)
        if not result.get('success', True):
            with _EMBEDDING_BUILD_LOCK:
                _embedding_build_progress['running'] = False
                _embedding_build_progress['error'] = '导入失败或无有效结果'
            return jsonify({
                "success": False,
                "error": f"导入失败: 成功 {result.get('imported', 0)} 条, 失败 {result.get('failed', 0)} 条",
                "data": result
            }), 502
        
        with _EMBEDDING_BUILD_LOCK:
            _embedding_build_progress['running'] = False
            _embedding_build_progress['step'] = '完成'
            _embedding_build_progress['batch_id'] = None
        
        return jsonify({
            "success": True,
            "message": f"导入完成！成功 {result['imported']} 条",
            "data": result
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _internal_error("接口处理失败", e)

@app.route('/api/embedding/test', methods=['POST'])
def test_embedding():
    """测试 Embedding API 连接"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    rate_error = _check_embedding_rate_limit()
    if rate_error:
        return rate_error

    try:
        from embedding_service import get_single_embedding, load_config as load_embedding_config
        import time
        
        body = request.get_json() or {}
        
        # 读取“有效配置”（文件/数据库/环境变量）
        config = load_embedding_config()
        
        # 如果前端传了专用配置就用，覆盖数据库配置
        if body.get('embedding_api_url'):
            config['embedding_api_url'] = body['embedding_api_url']
        if body.get('embedding_api_key'):
            config['embedding_api_key'] = body['embedding_api_key']
        if body.get('embedding_model'):
            config['embedding_model'] = body['embedding_model']
        
        config['embedding_api_url'] = _normalize_config_value('embedding_api_url', config.get('embedding_api_url', ''))
        config['embedding_model'] = _normalize_config_value('embedding_model', config.get('embedding_model', ''))

        has_key = bool(config.get('embedding_api_key') or config.get('api_key') or os.getenv('EMBEDDING_API_KEY'))
        if not has_key:
            return jsonify({
                "success": False,
                "error": "请填写 Embedding API Key（或先在配置里保存）"
            }), 400
        
        print("[Admin] 测试 Embedding 连通性...")
        # 测试调用
        start = time.time()
        result = get_single_embedding("测试文本", config)
        latency = (time.time() - start) * 1000
        print(f"[Admin] Embedding 响应: {'OK' if result else 'EMPTY'} {latency:.0f}ms")
        
        if result and len(result) > 0:
            return jsonify({
                "success": True,
                "message": f"连接成功! 延迟: {latency:.0f}ms, 向量维度: {len(result)} Base: {(config.get('embedding_api_url') or config.get('api_base_url') or '').rstrip('/')} 模型: {config.get('embedding_model')}"
            })
        else:
            return jsonify({
                "success": False,
                "error": "连接失败: 无响应或认证错误",
                "debug": {
                    "embedding_api_url": config.get('embedding_api_url', ''),
                    "embedding_model": config.get('embedding_model', ''),
                    "has_embedding_api_key": bool(config.get('embedding_api_key')),
                    "has_fallback_api_key": bool(config.get('api_key'))
                }
            }), 502
            
    except ImportError:
        return jsonify({
            "success": False,
            "error": "向量服务模块未安装"
        }), 501
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _internal_error("接口处理失败", e)

@app.route('/api/embedding/stats', methods=['GET'])
def embedding_stats():
    """获取向量索引统计（优化版：避免大表 JOIN）"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        cursor = data_lake.conn.cursor()
        
        # 产品总数（快速）
        cursor.execute('SELECT COUNT(*) FROM products')
        total_products = cursor.fetchone()[0]
        
        # 已生成向量数（快速）
        try:
            cursor.execute('SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL')
            total_embeddings = cursor.fetchone()[0]
        except:
            total_embeddings = 0
        
        coverage = (total_embeddings / total_products * 100) if total_products > 0 else 0
        
        # 待处理数量：简单用差值估算（避免慢查询）
        # 新增 = 产品总数 - 已有向量数
        pending = max(0, total_products - total_embeddings)
        
        return jsonify({
            "success": True,
            "data": {
                "total_products": total_products,
                "total_embeddings": total_embeddings,
                "coverage": f"{coverage:.1f}%",
                "pending": pending
            }
        })
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# API: 批量向量处理（Batch API）
# ==========================================

@app.route('/api/embedding/batch/start', methods=['POST'])
def batch_embedding_start():
    """
    启动批量向量处理任务
    
    Body: JSON
        {
            "batch_size": int - 每批数量（默认 50000）
        }
    """
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    try:
        from embedding_batch import generate_jsonl, upload_file, create_batch
        
        body = request.get_json() or {}
        batch_size = body.get('batch_size', 50000)
        
        print(f"[Admin] 启动批量向量处理, batch_size={batch_size}")
        
        # Step 1: 生成 JSONL 文件
        result = generate_jsonl(data_lake.conn, batch_size=batch_size, incremental=True)
        
        if result['count'] == 0:
            return jsonify({
                "success": True,
                "message": "无需处理，所有产品已有向量",
                "data": result
            })
        
        # Step 2: 上传文件
        file_id = upload_file(result['file_path'])
        if not file_id:
            return jsonify({
                "success": False,
                "error": "上传文件失败"
            }), 500
        
        # Step 3: 创建批处理任务
        batch_id = create_batch(file_id)
        if not batch_id:
            return jsonify({
                "success": False,
                "error": "创建批处理任务失败"
            }), 500
        
        return jsonify({
            "success": True,
            "message": f"批处理任务已创建，共 {result['count']} 条",
            "data": {
                "batch_id": batch_id,
                "file_id": file_id,
                "count": result['count'],
                "total_batches": result['total_batches']
            }
        })
        
    except ImportError:
        return jsonify({
            "success": False,
            "error": "批处理模块未安装"
        }), 501
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _internal_error("接口处理失败", e)

@app.route('/api/embedding/batch/status', methods=['GET'])
def batch_embedding_status():
    """查询批处理任务状态"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    try:
        from embedding_batch import check_batch_status, get_pipeline_state
        
        batch_id = request.args.get('batch_id')
        
        if batch_id:
            # 查询指定任务
            status = check_batch_status(batch_id)
            return jsonify({"success": True, "data": status})
        else:
            # 返回最近的任务状态
            state = get_pipeline_state()
            return jsonify({"success": True, "data": state})
            
    except ImportError:
        return jsonify({
            "success": False,
            "error": "批处理模块未安装"
        }), 501
    except Exception as e:
        return _internal_error("接口处理失败", e)

@app.route('/api/embedding/batch/import', methods=['POST'])
def batch_embedding_import():
    """导入批处理结果"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    try:
        from embedding_batch import download_results, import_results, get_pipeline_state
        
        body = request.get_json() or {}
        batch_id = body.get('batch_id')
        
        if not batch_id:
            # 尝试从 pipeline state 获取
            state = get_pipeline_state()
            batch_id = state.get('batch_id')
        
        if not batch_id:
            return jsonify({
                "success": False,
                "error": "请提供 batch_id"
            }), 400
        
        print(f"[Admin] 导入批处理结果: {batch_id}")
        
        # 下载结果
        result_file = download_results(batch_id)
        if not result_file:
            return jsonify({
                "success": False,
                "error": "下载结果失败，任务可能未完成"
            }), 400
        
        # 导入到数据库
        result = import_results(result_file, data_lake.conn)
        if not result.get('success', True):
            return jsonify({
                "success": False,
                "error": f"导入失败: 成功 {result.get('imported', 0)} 条, 失败 {result.get('failed', 0)} 条",
                "data": result
            }), 502
        
        return jsonify({
            "success": True,
            "message": f"导入完成: 成功 {result['imported']} 条, 失败 {result['failed']} 条",
            "data": result
        })
        
    except ImportError:
        return jsonify({
            "success": False,
            "error": "批处理模块未安装"
        }), 501
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _internal_error("接口处理失败", e)


# ==========================================
# API: 配置管理
# ==========================================

def _get_db_config(key: str, default: str = '') -> str:
    """从数据库获取配置"""
    cursor = data_lake.conn.cursor()
    cursor.execute('SELECT value FROM system_config WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def _set_db_config(key: str, value: str):
    """保存配置到数据库"""
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO system_config (key, value, updated_at)
        VALUES (?, ?, ?)
    ''', (key, value, datetime.now().isoformat()))
    data_lake.conn.commit()

def _get_all_config() -> Dict:
    """获取所有配置（优先数据库，其次 config.json）"""
    config_keys = ['api_base_url', 'api_key', 'model', 'embedding_api_url', 'embedding_api_key', 'embedding_model']
    config = {}
    
    # 先从数据库读取
    cursor = data_lake.conn.cursor()
    cursor.execute('SELECT key, value FROM system_config')
    for row in cursor.fetchall():
        config[row[0]] = row[1]
    
    # 如果数据库没有，从 config.json 迁移
    if not config and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            for key in config_keys:
                if file_config.get(key):
                    _set_db_config(key, file_config[key])
                    config[key] = file_config[key]
            print("[Config] 已从 config.json 迁移配置到数据库")
        except Exception as e:
            print(f"[Config] 迁移配置失败: {e}")
    
    # 确保所有键都有值
    for key in config_keys:
        if key not in config:
            config[key] = ''
    
    return config


def _sanitize_config_for_response(config: Dict) -> Dict:
    safe_config = dict(config or {})
    has_api_key = bool(safe_config.get('api_key'))
    has_embedding_api_key = bool(safe_config.get('embedding_api_key'))
    safe_config['api_key'] = ''
    safe_config['embedding_api_key'] = ''
    return {
        "data": safe_config,
        "meta": {
            "has_api_key": has_api_key,
            "has_embedding_api_key": has_embedding_api_key
        }
    }


def _serialize_auto_sync_config(config: Dict) -> Dict:
    normalized = normalize_auto_sync_settings(config)
    return {
        **normalized,
        "next_run_at": compute_next_run_iso(normalized),
        "summary": format_schedule_summary(normalized),
    }


def _get_auto_sync_config() -> Dict:
    raw = {key: _get_db_config(key, '') for key in AUTO_SYNC_PUBLIC_KEYS}
    return _serialize_auto_sync_config(raw)


def _save_auto_sync_config(body: Dict) -> Dict:
    normalized = normalize_auto_sync_settings(body)
    _set_db_config('auto_sync_enabled', '1' if normalized['auto_sync_enabled'] else '0')
    _set_db_config('auto_sync_schedule', normalized['auto_sync_schedule'])
    _set_db_config('auto_sync_time', normalized['auto_sync_time'])
    _set_db_config('auto_sync_weekday', str(normalized['auto_sync_weekday']))
    _set_db_config('auto_sync_type', normalized['auto_sync_type'])
    _set_db_config('auto_sync_last_slot', '')
    return _serialize_auto_sync_config(normalized)

@app.route('/api/config', methods=['GET'])
def get_config():
    """获取 API 配置"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        config = _get_all_config()
        response_payload = _sanitize_config_for_response(config)

        return jsonify({
            "success": True,
            "data": response_payload["data"],
            "meta": response_payload["meta"]
        })
    except Exception as e:
        return _internal_error("接口处理失败", e)

@app.route('/api/config', methods=['POST'])
def save_config():
    """保存 API 配置到数据库"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        body = request.get_json() or {}
        if not isinstance(body, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
        config_keys = ['api_base_url', 'api_key', 'model', 'embedding_api_url', 'embedding_api_key', 'embedding_model']
        updated_keys = []
        
        for key in config_keys:
            if key not in body:
                continue
            value = _normalize_config_value(key, body.get(key, ''))
            if value:  # 只保存非空值（留空表示不修改）
                _set_db_config(key, value)
                updated_keys.append(key)
        
        config = _get_all_config()
        response_payload = _sanitize_config_for_response(config)

        # 日志仅打印掩码，便于排查“是否真的写入数据库”
        if updated_keys:
            print(f"[Config] 更新配置: {updated_keys}, api_key={_mask_secret(config.get('api_key',''))}, embedding_api_key={_mask_secret(config.get('embedding_api_key',''))}")
        else:
            print("[Config] 未更新任何配置（可能所有字段均为空）")

        return jsonify({
            "success": True,
            "message": "配置已保存到数据库",
            "data": response_payload["data"],
            "meta": {
                "updated_keys": updated_keys,
                "has_api_key": response_payload["meta"]["has_api_key"],
                "has_embedding_api_key": response_payload["meta"]["has_embedding_api_key"]
            }
        })
        
    except Exception as e:
        return _internal_error("接口处理失败", e)


@app.route('/api/auto-sync/settings', methods=['GET'])
def get_auto_sync_settings():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        return jsonify({
            "success": True,
            "data": _get_auto_sync_config()
        })
    except Exception as e:
        return _internal_error("接口处理失败", e)


@app.route('/api/auto-sync/settings', methods=['POST'])
def save_auto_sync_settings():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        body = request.get_json() or {}
        if not isinstance(body, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400

        saved = _save_auto_sync_config(body)
        _log_auth_action(session.get('user_id'), "update_auto_sync_settings")
        return jsonify({
            "success": True,
            "message": "自动更新设置已保存",
            "data": saved
        })
    except Exception as e:
        return _internal_error("接口处理失败", e)

# ==========================================
# 主入口
# ==========================================

if __name__ == '__main__':
    print("=" * 50)
    print("UDID 医疗器械查询系统 - 后端服务")
    print("=" * 50)
    print(f"数据库路径: {DB_PATH}")
    print(f"配置文件路径: {CONFIG_PATH}")
    print("API 端点:")
    print("  GET  /api/stats    - 数据库统计")
    print("  GET  /api/search   - 搜索产品")
    print("  POST /api/upload   - 上传 XML")
    print("  POST /api/sync     - 同步数据")
    print("  POST /api/ai-match - AI 匹配")
    print("  GET/POST /api/config - 配置管理")
    print("=" * 50)
    print("访问地址: http://localhost:8080")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=8080, debug=False)
