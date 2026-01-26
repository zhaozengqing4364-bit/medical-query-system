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
import tempfile
import time
from typing import Optional, Dict
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# 导入本地数据湖模块
from udid_hybrid_system import LocalDataLake

# ==========================================
# Flask 应用初始化
# ==========================================, 

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app, supports_credentials=True)  # 允许跨域请求

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

def _load_app_config() -> Dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _ensure_secret_key():
    if SECRET_KEY:
        app.secret_key = SECRET_KEY
        return
    config = _load_app_config()
    app.secret_key = config.get('secret_key', 'change-me')
    if app.secret_key == 'change-me':
        print("[Auth] 提示: 请设置 SECRET_KEY 或 config.json 中的 secret_key")

def _init_auth_tables():
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            is_active INTEGER DEFAULT 1,
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
        default_password = os.getenv('ADMIN_DEFAULT_PASSWORD', 'admin123')
        now = datetime.now().isoformat()
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, 'admin', 1, ?, ?)
        ''', ('admin', generate_password_hash(default_password), now, now))
        data_lake.conn.commit()
        print("[Auth] 已创建默认管理员 admin")

def _log_auth_action(user_id: Optional[int], action: str):
    cursor = data_lake.conn.cursor()
    cursor.execute('''
        INSERT INTO auth_audit (user_id, action, ip, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, action, request.remote_addr, datetime.now().isoformat()))
    data_lake.conn.commit()

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
    return None

def _require_admin() -> Optional[tuple]:
    user = _get_current_user()
    if user and user.get('is_active') and user.get('role') == 'admin':
        return None

    api_key = ADMIN_API_KEY or _get_admin_key_from_config()
    if api_key:
        request_key = request.headers.get('X-Admin-Key', '')
        if request_key == api_key:
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
    return jsonify({"success": True, "data": user})

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    body = request.get_json() or {}
    username = body.get('username', '').strip()
    password = body.get('password', '')
    if not username or not password:
        return jsonify({"success": False, "error": "请输入账号和密码"}), 400

    cursor = data_lake.conn.cursor()
    cursor.execute('''
        SELECT id, password_hash, role, is_active
        FROM users WHERE username = ?
    ''', (username,))
    row = cursor.fetchone()
    if not row:
        _log_auth_action(None, f"login_failed:{username}")
        return jsonify({"success": False, "error": "账号或密码错误"}), 401

    user_id, password_hash, role, is_active = row
    if not is_active or not check_password_hash(password_hash, password):
        _log_auth_action(user_id, "login_failed")
        return jsonify({"success": False, "error": "账号或密码错误"}), 401

    session['user_id'] = user_id
    session['role'] = role
    cursor.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user_id))
    data_lake.conn.commit()
    _log_auth_action(user_id, "login")
    return jsonify({"success": True, "data": {"username": username, "role": role}})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
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
            VALUES (?, ?, ?, 1, ?, ?)
        ''', (username, generate_password_hash(password), role, now, now))
        data_lake.conn.commit()
        _log_auth_action(session.get('user_id'), f"create_user:{username}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"创建失败: {e}"}), 500

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
        params.append(1 if body['is_active'] else 0)
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
        return jsonify({"success": False, "error": str(e)}), 500

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
    try:
        # 获取参数
        keyword = request.args.get('keyword', '')
        status = request.args.get('status', '')
        product_type = request.args.get('type', '')
        category_code = request.args.get('category_code', '')
        manufacturer = request.args.get('manufacturer', '')
        # 新增筛选参数
        cert_no = request.args.get('cert_no', '')  # 注册证号
        model = request.args.get('model', '')  # 规格型号
        commercial_name = request.args.get('commercial_name', '')  # 商品名称
        date_from = request.args.get('date_from', '')  # 发布日期起
        date_to = request.args.get('date_to', '')  # 发布日期止
        
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 50))
        
        # 构建查询
        cursor = data_lake.conn.cursor()
        
        # 检查是否有 FTS5 索引
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products_fts'")
        has_fts = cursor.fetchone() is not None
        
        if has_fts and (keyword or cert_no or model or commercial_name or manufacturer):
            # 智能加速模式：只要有文本查询条件，就使用 FTS5
            fts_parts = []
            
            # 1. 关键词（全字段匹配）
            if keyword:
                fts_parts.append(f'"{keyword}"')
            
            # 2. 特定字段匹配 (FTS 语法: column: query)
            if cert_no:
                fts_parts.append(f'cert_no: "{cert_no}"')
            if model:
                fts_parts.append(f'model: "{model}"')
            if commercial_name:
                fts_parts.append(f'commercial_name: "{commercial_name}"')
            if manufacturer:
                fts_parts.append(f'manufacturer: "{manufacturer}"')
                
            # 构建 FTS 查询字符串 (AND 关系)
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
            
            # 分页查询
            offset = (page - 1) * page_size
            query_sql = f"""
                SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                       p.description, p.publish_date, p.source, p.last_updated, p.category_code
                FROM products p
                INNER JOIN products_fts ON p.rowid = products_fts.rowid
                WHERE products_fts MATCH ? AND {where_sql}
                ORDER BY p.last_updated DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query_sql, [fts_query] + params + [page_size, offset])
            rows = cursor.fetchall()
        else:
            # 普通 LIKE 搜索（无关键词或无 FTS）
            conditions = []
            params = []
            
            if keyword:
                conditions.append("(product_name LIKE ? OR manufacturer LIKE ? OR model LIKE ? OR description LIKE ?)")
                keyword_pattern = f"%{keyword}%"
                params.extend([keyword_pattern] * 4)
            
            if status:
                conditions.append("status = ?")
                params.append(status)
            
            if product_type:
                conditions.append("product_type = ?")
                params.append(product_type)
                
            if category_code:
                conditions.append("category_code LIKE ?")
                params.append(f"{category_code}%")
                
            if manufacturer:
                conditions.append("manufacturer LIKE ?")
                params.append(f"%{manufacturer}%")
            
            # 新增筛选条件
            if cert_no:
                conditions.append("cert_no LIKE ?")
                params.append(f"%{cert_no}%")
            
            if model:
                conditions.append("model LIKE ?")
                params.append(f"%{model}%")
            
            if commercial_name:
                conditions.append("commercial_name LIKE ?")
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
                       description, publish_date, source, last_updated, category_code
                FROM products 
                WHERE {where_clause}
                ORDER BY last_updated DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query_sql, params + [page_size, offset])
            rows = cursor.fetchall()
        
        # 转换为字典列表
        columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                   'description', 'publish_date', 'source', 'last_updated', 'category_code']
        data = [dict(zip(columns, row)) for row in rows]
        
        return jsonify({
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": page_size
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
            count = data_lake.ingest_xml(tmp_path)
            return jsonify({
                "success": True,
                "message": f"成功导入 {count} 条记录",
                "count": count
            })
        finally:
            # 清理临时文件
            os.unlink(tmp_path)
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
        return jsonify({"success": False, "error": str(e)}), 500

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
    try:
        import jieba.analyse
    except ImportError:
        return jsonify({"success": False, "error": "请先安装 jieba 库: pip install jieba"}), 501

    try:
        body = request.get_json()
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
        params = []
        sql_conditions = []
        
        # keyword_filter 已移入 FTS，此处不再需要 LIKE 硬过滤，避免重复且 LIKE 效率低
        keyword_filter = filters.get('keyword', '')
        if keyword_filter:
            # 关键词过滤（来自搜索框），作为硬性约束 (LIKE)
            # 确保结果集与普通搜索一致
            sql_conditions.append("(p.product_name LIKE ? OR p.manufacturer LIKE ? OR p.model LIKE ? OR p.description LIKE ?)")
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
            sql_conditions.append("p.category_code LIKE ?")
            params.append(f"{category_code}%")
        if manufacturer:
            sql_conditions.append("p.manufacturer LIKE ?")
            params.append(f"%{manufacturer}%")
        if cert_no:
            sql_conditions.append("p.cert_no LIKE ?")
            params.append(f"%{cert_no}%")
        if model:
            sql_conditions.append("p.model LIKE ?")
            params.append(f"%{model}%")
        if commercial_name:
            sql_conditions.append("p.commercial_name LIKE ?")
            params.append(f"%{commercial_name}%")
        if date_from:
            sql_conditions.append("p.publish_date >= ?")
            params.append(date_from)
        if date_to:
            sql_conditions.append("p.publish_date <= ?")
            params.append(date_to)
            
        where_sql = " AND ".join(sql_conditions) if sql_conditions else "1=1"
        
        # 4. 执行 BM25 查询
        # FTS5 的 rank 值越小越好 (通常是个负数或者基于 BM25 的分数)
        # 我们使用 ORDER BY rank 来获取最佳匹配
        cursor = data_lake.conn.cursor()
        
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
        return jsonify({"success": False, "error": str(e)}), 500

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
    try:
        body = request.get_json()
        requirement = body.get('requirement', '')  # 参数需求
        product_name = body.get('product_name', '')  # 产品名称（新增）
        filters = body.get('filters', {})
        use_vector = body.get('use_vector', True)
        
        # 至少需要产品名称或参数需求
        if not requirement and not product_name:
            return jsonify({"success": False, "error": "请输入产品名称或参数需求"}), 400
        
        # 准备筛选条件
        keyword = filters.get('keyword', '')
        category_code = filters.get('category_code', '')
        manufacturer = filters.get('manufacturer', '')
        
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
                    'category_code': category_code,
                    'manufacturer': manufacturer
                }
                
                # 传入产品名称和参数需求
                vector_results = hybrid_search(
                    query=requirement,  # 参数需求（用于向量排序）
                    conn=data_lake.conn,
                    top_k=50,
                    filters=vector_filters,
                    product_name=product_name  # 产品名称（用于过滤）
                )
                
                if vector_results:
                    candidates = vector_results
                    print(f"[AiMatch] 两阶段检索召回 {len(candidates)} 个候选")
            except ImportError:
                print("[AiMatch] 向量服务未安装，使用 FTS 召回")
            except Exception as e:
                print(f"[AiMatch] 向量检索失败: {e}，降级到 FTS")
        
        # -------------------------------------------------------
        # 降级：使用 FTS 关键词检索
        # -------------------------------------------------------
        if not candidates:
            # 提取关键词
            keywords = []
            try:
                from ai_service import expand_search_keywords
                keywords = expand_search_keywords(requirement)
            except Exception as e:
                print(f"[AiMatch] AI 关键词扩展失败: {e}")
            
            if not keywords:
                try:
                    import jieba.analyse
                    keywords = jieba.analyse.extract_tags(requirement, topK=5)
                except:
                    pass
            
            if not keywords:
                return jsonify({"success": False, "error": "无法提取有效关键词"}), 400
            
            fts_query = " OR ".join([f'"{k}"' for k in keywords])
            
            if keywords:
                # 使用 FTS5 全文检索按相关性排序
                where_sql_fts = (
                    where_clause.replace("product_name", "p.product_name")
                                .replace("manufacturer", "p.manufacturer")
                                .replace("model", "p.model")
                                .replace("description", "p.description")
                                .replace("category_code", "p.category_code")
                )

                query_sql = (
                    "SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, "
                    "p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope "
                    "FROM products p "
                    "INNER JOIN products_fts f ON p.rowid = f.rowid "
                    f"WHERE f.products_fts MATCH ? AND {where_sql_fts} "
                    "ORDER BY f.rank "
                    "LIMIT 50"
                )
                params_final = [fts_query] + params
                
            else:
                # 回退到按时间排序
                query_sql = (
                    "SELECT di_code, product_name, commercial_name, model, manufacturer, "
                    "description, publish_date, source, last_updated, category_code, scope "
                    "FROM products "
                    f"WHERE {where_clause} "
                    "ORDER BY last_updated DESC "
                    "LIMIT 50"
                )
                params_final = params

            cursor = data_lake.conn.cursor()
            cursor.execute(query_sql, params_final)
            rows = cursor.fetchall()
            
            # 零结果回退
            if not rows and where_clause != "1=1":
                print("[AiMatch] No FTS match found, falling back to Filter-only candidates.")
                fallback_sql = f"""
                    SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer, 
                           p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope
                    FROM products p
                    WHERE {where_clause}
                    ORDER BY p.last_updated DESC
                    LIMIT 50
                """
                cursor.execute(fallback_sql, params)
                rows = cursor.fetchall()
                
            columns = ['di_code', 'product_name', 'commercial_name', 'model', 'manufacturer',
                       'description', 'publish_date', 'source', 'last_updated', 'category_code', 'scope']
            candidates = [dict(zip(columns, row)) for row in rows]
            recall_method = "fts"
            print(f"[AiMatch] FTS 检索召回 {len(candidates)} 个候选")
        
        if not candidates:
            return jsonify({
                "success": False, 
                "error": "没有符合筛选条件的候选产品"
            }), 400
        
        # 向量检索结果已经包含 matchScore，直接返回
        if recall_method == "vector":
            return jsonify({
                "success": True,
                "data": candidates,
                "total": len(candidates),
                "method": "vector"
            })
        
        # FTS 结果需要添加 matchScore
        for i, item in enumerate(candidates):
            if 'matchScore' not in item:
                # 基于排名的分数：第1名=85，第50名=55
                item['matchScore'] = max(55, 85 - i)
        
        return jsonify({
            "success": True,
            "data": candidates,
            "total": len(candidates),
            "method": recall_method
        })
        
    except ImportError as e:
        return jsonify({ "success": False, "error": "向量服务模块不可用" }), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ==========================================
# API: 测试 AI 连通性
# ==========================================
@app.route('/api/test-ai', methods=['POST'])
def test_ai():
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        from ai_service import call_ai_api
        import time
        
        body = request.get_json() or {}
        
        # 从数据库获取配置
        config = _get_all_config()
        
        # 如果前端传了专用配置就用，覆盖数据库配置
        if body.get('api_base_url'):
            config['api_base_url'] = body['api_base_url']
        if body.get('api_key'):
            config['api_key'] = body['api_key']
        if body.get('model'):
            config['model'] = body['model']
        
        # 简单测试
        start = time.time()
        res = call_ai_api("Test connection. Reply 'OK'.", config)
        latency = (time.time() - start) * 1000
        
        if res:
            return jsonify({
                "success": True, 
                "message": f"连接成功! (延迟: {latency:.0f}ms) 响应: {res[:20]}..."
            })
        else:
            return jsonify({
                "success": False, 
                "error": "连接失败: 无响应或认证错误"
            }), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
        return jsonify({"success": False, "error": str(e)}), 500

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
                    cursor.execute('SELECT COUNT(*) FROM embeddings')
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
        
        cursor.execute('SELECT COUNT(*) FROM embeddings')
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
            
            _embedding_build_progress['running'] = False
            _embedding_build_progress['step'] = '完成'
            _embedding_build_progress['current'] = result['processed']
            
            return jsonify({
                "success": result['success'],
                "message": f"已完成！处理 {result['processed']} 条，跳过 {result['skipped']} 条",
                "data": {**result, "mode": "single", "pending": pending}
            })
        else:
            # 批处理模式（异步）
            from embedding_batch import generate_jsonl, upload_file, create_batch, save_batch_task
            
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
            _embedding_build_progress['step'] = '正在准备数据文件...'
            result = generate_jsonl(data_lake.conn, incremental=True)
            
            if result['count'] == 0:
                _embedding_build_progress['running'] = False
                return jsonify({
                    "success": True,
                    "message": "所有产品已有最新向量",
                    "data": {"pending": 0, "mode": "batch"}
                })
            
            # Step 2: 上传文件
            _embedding_build_progress['step'] = '正在上传数据...'
            _embedding_build_progress['current'] = 1
            file_id = upload_file(result['file_path'])
            if not file_id:
                _embedding_build_progress['running'] = False
                _embedding_build_progress['error'] = '上传失败'
                return jsonify({"success": False, "error": "上传数据失败"}), 500
            
            # Step 3: 创建批处理任务
            _embedding_build_progress['step'] = '正在创建处理任务...'
            _embedding_build_progress['current'] = 2
            batch_id = create_batch(file_id)
            if not batch_id:
                _embedding_build_progress['running'] = False
                _embedding_build_progress['error'] = '创建任务失败'
                return jsonify({"success": False, "error": "创建批处理任务失败"}), 500
            
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
        _embedding_build_progress['running'] = False
        return jsonify({
            "success": False,
            "error": f"模块未安装: {e}"
        }), 501
    except Exception as e:
        _embedding_build_progress['running'] = False
        _embedding_build_progress['error'] = str(e)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/embedding/progress', methods=['GET'])
def embedding_progress():
    """获取向量构建进度"""
    global _embedding_build_progress
    
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    
    import time
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
        
        batch_id = _embedding_build_progress.get('batch_id')
        if not batch_id:
            body = request.get_json() or {}
            batch_id = body.get('batch_id')
        
        if not batch_id:
            return jsonify({"success": False, "error": "没有待导入的任务"}), 400
        
        _embedding_build_progress['step'] = '正在下载结果...'
        result_file = download_results(batch_id)
        if not result_file:
            return jsonify({"success": False, "error": "下载结果失败，任务可能未完成"}), 400
        
        _embedding_build_progress['step'] = '正在导入数据库...'
        result = import_results(result_file, data_lake.conn)
        
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
        return jsonify({"success": False, "error": str(e)}), 500

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
        from embedding_service import get_single_embedding
        import time
        
        body = request.get_json() or {}
        
        # 从数据库获取配置
        config = _get_all_config()
        
        # 如果前端传了专用配置就用，覆盖数据库配置
        if body.get('embedding_api_url'):
            config['api_base_url'] = body['embedding_api_url']
        if body.get('embedding_api_key'):
            config['api_key'] = body['embedding_api_key']
        if body.get('embedding_model'):
            config['embedding_model'] = body['embedding_model']
        
        if not config.get('api_key'):
            return jsonify({
                "success": False,
                "error": "请填写 API Key"
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
                "message": f"连接成功! 延迟: {latency:.0f}ms, 向量维度: {len(result)}"
            })
        else:
            return jsonify({
                "success": False,
                "error": "连接失败: 无响应或认证错误"
            }), 502
            
    except ImportError:
        return jsonify({
            "success": False,
            "error": "向量服务模块未安装"
        }), 501
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

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
            cursor.execute('SELECT COUNT(*) FROM embeddings')
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
        return jsonify({"success": False, "error": str(e)}), 500

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
        return jsonify({"success": False, "error": str(e)}), 500

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
        return jsonify({"success": False, "error": str(e)}), 500

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
        return jsonify({"success": False, "error": str(e)}), 500


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

@app.route('/api/config', methods=['GET'])
def get_config():
    """获取 API 配置"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        config = _get_all_config()
        return jsonify({"success": True, "data": config})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/config', methods=['POST'])
def save_config():
    """保存 API 配置到数据库"""
    auth_error = _require_admin()
    if auth_error:
        return auth_error
    try:
        body = request.get_json()
        config_keys = ['api_base_url', 'api_key', 'model', 'embedding_api_url', 'embedding_api_key', 'embedding_model']
        
        for key in config_keys:
            value = body.get(key, '').strip()
            if value:  # 只保存非空值
                _set_db_config(key, value)
        
        return jsonify({"success": True, "message": "配置已保存到数据库"})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
