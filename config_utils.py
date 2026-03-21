"""
共享配置工具
================

统一处理：
1. .env 加载（幂等）
2. JSON 配置文件读取
3. SQLite system_config 读取
4. 多来源配置合并（环境变量优先）
"""

import json
import os
import sqlite3
import threading
from typing import Dict, Mapping, Optional, Sequence
from db_backend import connect as db_connect, is_postgres_backend

_ENV_LOAD_LOCK = threading.Lock()
_LOADED_ENV_FILES = set()


def _manual_load_env_file(env_path: str) -> None:
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def load_env_file_once(base_dir: str, filename: str = '.env', log_prefix: str = '[Config]') -> bool:
    """
    幂等加载 .env：同一路径在当前进程仅加载一次。
    优先使用 python-dotenv，不可用时回退手写解析。
    """
    env_path = os.path.join(base_dir, filename)
    with _ENV_LOAD_LOCK:
        if env_path in _LOADED_ENV_FILES:
            return False
        _LOADED_ENV_FILES.add(env_path)

    if not os.path.exists(env_path):
        return False

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path, override=False)
        return True
    except Exception:
        pass

    try:
        _manual_load_env_file(env_path)
        return True
    except Exception as e:
        print(f"{log_prefix} 加载 .env 失败: {e}")
        return False


def load_json_config(path: str, log_prefix: str = '[Config]') -> Dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print(f"{log_prefix} 加载配置文件失败 ({path}): {e}")
        return {}


def load_system_config(db_path: str, log_prefix: str = '[Config]') -> Dict:
    if not db_path:
        return {}
    if not is_postgres_backend() and not os.path.exists(db_path):
        return {}

    conn: Optional[object] = None
    try:
        conn = db_connect(db_path, timeout=10)
        try:
            conn.execute('PRAGMA busy_timeout = 5000')
        except Exception:
            pass
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM system_config')
        rows = cursor.fetchall()
        return {row[0]: row[1] for row in rows if row and row[0]}
    except Exception as e:
        print(f"{log_prefix} 读取数据库配置失败: {e}")
        return {}
    finally:
        if conn is not None:
            conn.close()


def merge_config_sources(
    *,
    defaults: Optional[Mapping[str, object]] = None,
    config_paths: Optional[Sequence[str]] = None,
    db_path: Optional[str] = None,
    env_mapping: Optional[Mapping[str, str]] = None,
    log_prefix: str = '[Config]',
    log_env_updates: bool = False,
    env_overrides_db: bool = True,
) -> Dict:
    """
    合并配置，优先级：
    当 env_overrides_db=True:
        defaults < config_paths < system_config < env_mapping
    当 env_overrides_db=False:
        defaults < config_paths < env_mapping < system_config
    """
    config: Dict = dict(defaults or {})

    for path in config_paths or []:
        file_config = load_json_config(path, log_prefix=log_prefix)
        if file_config:
            config.update(file_config)

    def _apply_db():
        if not db_path:
            return
        db_config = load_system_config(db_path, log_prefix=log_prefix)
        for key, value in db_config.items():
            if value:
                config[key] = value

    def _apply_env():
        for env_var, config_key in (env_mapping or {}).items():
            value = os.getenv(env_var)
            if value:
                config[config_key] = value
                if log_env_updates:
                    print(f"{log_prefix} 从环境变量加载: {config_key}")

    if env_overrides_db:
        _apply_db()
        _apply_env()
    else:
        _apply_env()
        _apply_db()

    return config
