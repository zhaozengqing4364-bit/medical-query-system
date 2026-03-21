"""
Database backend compatibility layer.

Default backend is PostgreSQL for production cutover.
SQLite remains available only as explicit rollback backend.
"""

import os
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional, Sequence, Tuple


_WEAK_DB_PASSWORDS = {
    "",
    "your_secure_password",
    "password",
    "123456",
    "admin",
}

_POSTGRES_POOL = None
_POSTGRES_POOL_LOCK = threading.Lock()


def get_backend() -> str:
    backend = (os.getenv("DB_BACKEND") or "postgres").strip().lower()
    if backend not in ("postgres", "sqlite"):
        raise RuntimeError(f"DB_BACKEND 非法值: {backend}")
    return backend


def is_postgres_backend() -> bool:
    return get_backend() == "postgres"


def _get_postgres_config() -> dict:
    password = (os.getenv("POSTGRES_PASSWORD") or "").strip()
    if password.lower() in _WEAK_DB_PASSWORDS:
        raise RuntimeError("POSTGRES_PASSWORD 未设置或为弱值，拒绝启动。")
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "udid_db"),
        "user": os.getenv("POSTGRES_USER", "udid_user"),
        "password": password,
    }


def _get_pool_size() -> Tuple[int, int]:
    def _parse_env_int(name: str, default: int, min_value: int, max_value: int) -> int:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise RuntimeError(f"{name} 必须是整数，当前值: {raw}")
        if value < min_value or value > max_value:
            raise RuntimeError(
                f"{name} 超出范围，要求 {min_value}~{max_value}，当前值: {value}"
            )
        return value

    min_conn = _parse_env_int("POSTGRES_POOL_MIN", 1, 1, 100)
    max_conn = _parse_env_int("POSTGRES_POOL_MAX", 12, 1, 500)
    if min_conn > max_conn:
        raise RuntimeError(
            f"POSTGRES_POOL_MIN({min_conn}) 不能大于 POSTGRES_POOL_MAX({max_conn})"
        )
    return min_conn, max_conn


def _get_pool_acquire_timeout(default_timeout: int) -> float:
    raw = (os.getenv("POSTGRES_POOL_ACQUIRE_TIMEOUT") or "").strip()
    if not raw:
        return float(default_timeout)
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"POSTGRES_POOL_ACQUIRE_TIMEOUT 必须是数字，当前值: {raw}")
    if value <= 0 or value > 300:
        raise RuntimeError(
            f"POSTGRES_POOL_ACQUIRE_TIMEOUT 超出范围，要求 0~300（不含 0），当前值: {value}"
        )
    return value


def _acquire_pool_connection(pool, acquire_timeout: float):
    deadline = time.monotonic() + acquire_timeout
    while True:
        try:
            return pool.getconn()
        except Exception as e:
            # ThreadedConnectionPool 在连接池耗尽时会立即抛错，这里转为短轮询等待。
            if "connection pool exhausted" not in str(e).lower():
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"PostgreSQL 连接池获取超时 ({acquire_timeout:.1f}s): {e}"
                )
            time.sleep(0.05)


def _get_postgres_pool(connect_timeout: int):
    global _POSTGRES_POOL
    if _POSTGRES_POOL is not None:
        return _POSTGRES_POOL

    with _POSTGRES_POOL_LOCK:
        if _POSTGRES_POOL is not None:
            return _POSTGRES_POOL
        try:
            from psycopg2.pool import ThreadedConnectionPool
        except Exception as e:
            raise RuntimeError(f"PostgreSQL backend 需要 psycopg2-binary: {e}")

        cfg = _get_postgres_config()
        min_conn, max_conn = _get_pool_size()
        _POSTGRES_POOL = ThreadedConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            host=cfg["host"],
            port=cfg["port"],
            dbname=cfg["dbname"],
            user=cfg["user"],
            password=cfg["password"],
            connect_timeout=connect_timeout,
        )
        return _POSTGRES_POOL


def _replace_qmark_placeholders(sql: str) -> str:
    out = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            if in_double and i + 1 < len(sql) and sql[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if ch == "?" and not in_single and not in_double:
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_literal_percents(sql: str) -> str:
    """
    psycopg2 使用 pyformat，占位符是 %s。
    非占位符用途的 '%' 必须转义为 '%%'，否则会触发参数解析异常。
    """
    out = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue

        if i + 1 < len(sql):
            nxt = sql[i + 1]
            # 已转义的 %% 和位置占位符 %s 保持不变。
            if nxt == "%" or nxt == "s":
                out.append("%")
                out.append(nxt)
                i += 2
                continue
            # 兼容命名占位符 %(name)s 场景。
            if nxt == "(":
                out.append("%")
                i += 1
                continue

        out.append("%%")
        i += 1
    return "".join(out)


def _convert_insert_or_replace(sql: str) -> str:
    pattern = re.compile(
        r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.match(sql.strip())
    if not m:
        return sql
    table = m.group(1)
    cols_raw = m.group(2)
    values_raw = m.group(3)
    columns = [c.strip() for c in cols_raw.split(",") if c.strip()]
    if not columns:
        return sql
    conflict_col = columns[0]
    update_cols = columns[1:] or [conflict_col]
    update_clause = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values_raw}) "
        f"ON CONFLICT ({conflict_col}) DO UPDATE SET {update_clause}"
    )


def _translate_sql_for_postgres(sql: str) -> Optional[str]:
    raw = sql.strip()
    upper = raw.upper()

    # SQLite pragmas are not applicable in PostgreSQL.
    if upper.startswith("PRAGMA "):
        return "SELECT 1"

    # "BEGIN TRANSACTION" -> "BEGIN"
    if upper == "BEGIN TRANSACTION":
        return "BEGIN"

    # SQLite catalog query compatibility.
    if "FROM SQLITE_MASTER" in upper and "PRODUCTS_FTS" in upper:
        # Return empty result so upper layer falls back to non-FTS branch.
        return "SELECT NULL WHERE FALSE"

    translated = sql
    translated = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(r"\bAUTOINCREMENT\b", "", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bBLOB\b", "BYTEA", translated, flags=re.IGNORECASE)
    translated = _convert_insert_or_replace(translated)
    translated = _replace_qmark_placeholders(translated)
    translated = _escape_literal_percents(translated)
    return translated


def _map_pg_exception(exc: Exception) -> Exception:
    exc_text = str(exc)
    try:
        import psycopg2
        if isinstance(exc, psycopg2.IntegrityError):
            return sqlite3.IntegrityError(exc_text)
        if isinstance(exc, psycopg2.OperationalError):
            return sqlite3.OperationalError(exc_text)
        if isinstance(exc, psycopg2.ProgrammingError):
            return sqlite3.OperationalError(exc_text)
        if isinstance(exc, psycopg2.DatabaseError):
            return sqlite3.DatabaseError(exc_text)
    except Exception:
        pass
    return sqlite3.DatabaseError(exc_text)


class PostgresCompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        sql_pg = _translate_sql_for_postgres(sql)
        if sql_pg is None:
            return self
        exec_params: Sequence[Any]
        if params is None:
            exec_params = ()
        elif isinstance(params, (list, tuple)):
            exec_params = tuple(params)
        else:
            exec_params = (params,)
        try:
            self._cursor.execute(sql_pg, exec_params)
        except Exception as e:
            raise _map_pg_exception(e)
        return self

    def executemany(self, sql: str, param_list: Iterable[Sequence[Any]]):
        sql_pg = _translate_sql_for_postgres(sql)
        if sql_pg is None:
            return self
        try:
            self._cursor.executemany(sql_pg, param_list)
        except Exception as e:
            raise _map_pg_exception(e)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchmany(self, size: Optional[int] = None):
        if size is None:
            return self._cursor.fetchmany()
        return self._cursor.fetchmany(size)

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PostgresCompatConnection:
    def __init__(self, conn, release_conn=None):
        self._conn = conn
        self._release_conn = release_conn
        self._closed = False

    def cursor(self):
        self._ensure_open()
        return PostgresCompatCursor(self._conn.cursor())

    def commit(self):
        self._ensure_open()
        self._conn.commit()

    def rollback(self):
        self._ensure_open()
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        try:
            if self._release_conn is None:
                self._conn.close()
            else:
                # Ensure pool connection is returned with a clean transaction state.
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._release_conn(self._conn)
        finally:
            self._closed = True

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        self._ensure_open()
        cur = self.cursor()
        try:
            cur.execute(sql, params)
            return cur
        except Exception:
            cur.close()
            raise

    def _ensure_open(self):
        if self._closed:
            raise sqlite3.OperationalError("数据库连接已关闭")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def connect(
    db_path: Optional[str] = None,
    check_same_thread: bool = False,
    timeout: int = 10,
):
    if get_backend() == "sqlite":
        return sqlite3.connect(
            db_path or os.getenv("SQLITE_DB_PATH", "udid_hybrid_lake.db"),
            check_same_thread=check_same_thread,
            timeout=timeout,
        )

    try:
        import psycopg2
    except Exception as e:
        raise RuntimeError(f"PostgreSQL backend 需要 psycopg2-binary: {e}")

    try:
        pool = _get_postgres_pool(connect_timeout=timeout)
        acquire_timeout = _get_pool_acquire_timeout(timeout)
        pg_conn = _acquire_pool_connection(pool, acquire_timeout)
        pg_conn.autocommit = False
        return PostgresCompatConnection(pg_conn, release_conn=pool.putconn)
    except Exception as e:
        raise RuntimeError(f"连接 PostgreSQL 失败: {e}")
