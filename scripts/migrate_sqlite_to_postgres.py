#!/usr/bin/env python3
"""
SQLite -> PostgreSQL 数据迁移（无 pgloader 依赖）

默认迁移：
1) products
2) embeddings（默认包含 embedding BLOB）
3) sync_log
4) 业务元数据表：system_config/users/auth_audit/data_audit_log/
   embedding_update_queue/ingest_rejects/sync_history/sync_run

说明：
- 默认使用 UPSERT，避免重复执行时插入重复主键。
- 默认非破坏模式，不做 TRUNCATE/DROP。
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values


PRODUCT_COLUMNS = [
    "di_code",
    "product_name",
    "commercial_name",
    "model",
    "manufacturer",
    "description",
    "publish_date",
    "source",
    "last_updated",
    "category_code",
    "social_code",
    "cert_no",
    "status",
    "product_type",
    "phone",
    "email",
    "scope",
    "safety_info",
]

ID_TABLES = {
    "auth_audit",
    "data_audit_log",
    "ingest_rejects",
    "sync_history",
    "sync_run",
    "users",
}


def _to_bool_or_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(text)


AUX_TABLE_SPECS: Dict[str, Dict] = {
    "system_config": {
        "columns": ["key", "value", "updated_at"],
        "conflict_cols": ["key"],
        "update_cols": ["value", "updated_at"],
    },
    "users": {
        "columns": ["id", "username", "password_hash", "role", "is_active", "created_at", "updated_at", "last_login"],
        "conflict_cols": ["id"],
        "update_cols": ["username", "password_hash", "role", "is_active", "created_at", "updated_at", "last_login"],
        "transform": lambda row: (
            row["id"],
            row["username"],
            row["password_hash"],
            row["role"],
            _to_bool_or_none(row["is_active"]),
            row["created_at"],
            row["updated_at"],
            row["last_login"],
        ),
    },
    "auth_audit": {
        "columns": ["id", "user_id", "action", "ip", "created_at"],
        "conflict_cols": ["id"],
        "update_cols": ["user_id", "action", "ip", "created_at"],
    },
    "data_audit_log": {
        "columns": ["id", "di_code", "field_name", "old_value", "new_value", "source", "file_name", "created_at"],
        "conflict_cols": ["id"],
        "update_cols": ["di_code", "field_name", "old_value", "new_value", "source", "file_name", "created_at"],
    },
    "embedding_update_queue": {
        "columns": ["di_code", "action", "status", "created_at", "claimed_at", "processed_at", "error_message"],
        "conflict_cols": ["di_code", "status"],
        "update_cols": ["action", "created_at", "claimed_at", "processed_at", "error_message"],
    },
    "ingest_rejects": {
        "columns": ["id", "file_name", "di_code", "reason", "created_at"],
        "conflict_cols": ["id"],
        "update_cols": ["file_name", "di_code", "reason", "created_at"],
    },
    "sync_history": {
        "columns": ["id", "sync_type", "start_time", "end_time", "records_count", "status", "message", "duration_seconds"],
        "conflict_cols": ["id"],
        "update_cols": ["sync_type", "start_time", "end_time", "records_count", "status", "message", "duration_seconds"],
    },
    "sync_run": {
        "columns": ["id", "sync_date", "file_name", "status", "error_message", "records_count", "invalid_records", "audit_records", "file_checksum", "file_size", "created_at"],
        "conflict_cols": ["id"],
        "update_cols": ["sync_date", "file_name", "status", "error_message", "records_count", "invalid_records", "audit_records", "file_checksum", "file_size", "created_at"],
    },
}


@dataclass
class PgConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def load_pg_config() -> PgConfig:
    password = (os.getenv("POSTGRES_PASSWORD") or "").strip()
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD 为空，拒绝迁移。")
    return PgConfig(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "udid_db"),
        user=os.getenv("POSTGRES_USER", "udid_user"),
        password=password,
    )


def connect_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def connect_postgres(cfg: PgConfig):
    return psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
    )


def sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def sqlite_has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def pg_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])


def pg_has_table(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
            """,
            (table,),
        )
        return cur.fetchone() is not None


def sync_pg_sequence(conn, table: str, id_column: str = "id") -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{id_column}'), "
            f"COALESCE((SELECT MAX({id_column}) FROM {table}), 1), true)"
        )
    conn.commit()


def chunked_rows(cur, batch_size: int) -> Iterable[List[sqlite3.Row]]:
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        yield rows


def migrate_products(sqlite_conn, pg_conn, batch_size: int) -> None:
    src_total = sqlite_count(sqlite_conn, "products")
    log(f"[products] source rows: {src_total}")
    if src_total == 0:
        return

    sql_select = f"SELECT {', '.join(PRODUCT_COLUMNS)} FROM products"
    updatable_cols = [c for c in PRODUCT_COLUMNS if c != "di_code"]
    sql_insert = (
        f"INSERT INTO products ({', '.join(PRODUCT_COLUMNS)}) VALUES %s "
        "ON CONFLICT (di_code) DO UPDATE SET "
        + ", ".join([f"{c} = EXCLUDED.{c}" for c in updatable_cols])
    )

    processed = 0
    start = time.time()
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute(sql_select)

    with pg_conn.cursor() as cur_pg:
        for rows in chunked_rows(cur_sqlite, batch_size):
            values: List[Tuple] = []
            for row in rows:
                values.append(tuple(row[col] for col in PRODUCT_COLUMNS))
            execute_values(cur_pg, sql_insert, values, page_size=batch_size)
            pg_conn.commit()
            processed += len(values)
            elapsed = max(0.001, time.time() - start)
            speed = processed / elapsed
            log(f"[products] migrated {processed}/{src_total} ({speed:.0f} rows/s)")

    dst_total = pg_count(pg_conn, "products")
    log(f"[products] target rows: {dst_total}")


def migrate_embeddings(sqlite_conn, pg_conn, batch_size: int, with_blob: bool) -> None:
    src_total = sqlite_count(sqlite_conn, "embeddings")
    log(f"[embeddings] source rows: {src_total}")
    if src_total == 0:
        return

    if with_blob:
        src_cols = ["di_code", "embedding", "text_hash", "created_at"]
        insert_cols = src_cols
        update_cols = ["embedding", "text_hash", "created_at"]
        update_where = (
            "embeddings.embedding IS NULL "
            "OR embeddings.text_hash IS DISTINCT FROM EXCLUDED.text_hash "
            "OR embeddings.created_at IS DISTINCT FROM EXCLUDED.created_at"
        )
    else:
        src_cols = ["di_code", "text_hash", "created_at"]
        insert_cols = src_cols
        update_cols = ["text_hash", "created_at"]
        update_where = (
            "embeddings.text_hash IS DISTINCT FROM EXCLUDED.text_hash "
            "OR embeddings.created_at IS DISTINCT FROM EXCLUDED.created_at"
        )

    sql_select = f"SELECT {', '.join(src_cols)} FROM embeddings"
    sql_insert = (
        f"INSERT INTO embeddings ({', '.join(insert_cols)}) VALUES %s "
        "ON CONFLICT (di_code) DO UPDATE SET "
        + ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])
        + f" WHERE {update_where}"
    )

    processed = 0
    start = time.time()
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute(sql_select)

    with pg_conn.cursor() as cur_pg:
        for rows in chunked_rows(cur_sqlite, batch_size):
            values = [tuple(row[col] for col in src_cols) for row in rows]
            execute_values(cur_pg, sql_insert, values, page_size=max(200, batch_size // 4))
            pg_conn.commit()
            processed += len(values)
            elapsed = max(0.001, time.time() - start)
            speed = processed / elapsed
            log(f"[embeddings] migrated {processed}/{src_total} ({speed:.0f} rows/s)")

    dst_total = pg_count(pg_conn, "embeddings")
    with pg_conn.cursor() as cur_pg:
        cur_pg.execute("SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL")
        non_null = int(cur_pg.fetchone()[0])
    log(f"[embeddings] target rows: {dst_total}, non_null_embedding: {non_null}")


def migrate_sync_log(sqlite_conn, pg_conn, batch_size: int) -> None:
    src_total = sqlite_count(sqlite_conn, "sync_log")
    log(f"[sync_log] source rows: {src_total}")
    if src_total == 0:
        return

    cols = ["id", "sync_date", "data_date", "file_name", "records_count", "created_at"]
    sql_select = f"SELECT {', '.join(cols)} FROM sync_log ORDER BY id ASC"
    sql_insert = (
        f"INSERT INTO sync_log ({', '.join(cols)}) VALUES %s "
        "ON CONFLICT (id) DO UPDATE SET "
        "sync_date = EXCLUDED.sync_date, "
        "data_date = EXCLUDED.data_date, "
        "file_name = EXCLUDED.file_name, "
        "records_count = EXCLUDED.records_count, "
        "created_at = EXCLUDED.created_at"
    )

    processed = 0
    start = time.time()
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute(sql_select)

    with pg_conn.cursor() as cur_pg:
        for rows in chunked_rows(cur_sqlite, batch_size):
            values = [tuple(row[col] for col in cols) for row in rows]
            execute_values(cur_pg, sql_insert, values, page_size=batch_size)
            pg_conn.commit()
            processed += len(values)
            elapsed = max(0.001, time.time() - start)
            speed = processed / elapsed
            log(f"[sync_log] migrated {processed}/{src_total} ({speed:.0f} rows/s)")
        cur_pg.execute(
            "SELECT setval(pg_get_serial_sequence('sync_log', 'id'), "
            "COALESCE((SELECT MAX(id) FROM sync_log), 1), true)"
        )
        pg_conn.commit()

    dst_total = pg_count(pg_conn, "sync_log")
    log(f"[sync_log] target rows: {dst_total}")


def migrate_aux_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    columns: Sequence[str],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str],
    batch_size: int,
    row_transform: Callable[[sqlite3.Row], Tuple] = None,
) -> None:
    if not sqlite_has_table(sqlite_conn, table):
        log(f"[{table}] skip: source table missing")
        return
    if not pg_has_table(pg_conn, table):
        log(f"[{table}] skip: target table missing")
        return

    src_total = sqlite_count(sqlite_conn, table)
    log(f"[{table}] source rows: {src_total}")
    if src_total == 0:
        return

    sql_select = f"SELECT {', '.join(columns)} FROM {table}"
    conflict_clause = ", ".join(conflict_cols)
    if update_cols:
        sql_insert = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
            f"ON CONFLICT ({conflict_clause}) DO UPDATE SET "
            + ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])
        )
    else:
        sql_insert = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
            f"ON CONFLICT ({conflict_clause}) DO NOTHING"
        )

    processed = 0
    start = time.time()
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute(sql_select)

    with pg_conn.cursor() as cur_pg:
        for rows in chunked_rows(cur_sqlite, batch_size):
            if row_transform:
                values = [row_transform(row) for row in rows]
            else:
                values = [tuple(row[col] for col in columns) for row in rows]
            execute_values(cur_pg, sql_insert, values, page_size=min(batch_size, 2000))
            pg_conn.commit()
            processed += len(values)
            elapsed = max(0.001, time.time() - start)
            speed = processed / elapsed
            log(f"[{table}] migrated {processed}/{src_total} ({speed:.0f} rows/s)")

    if table in ID_TABLES:
        sync_pg_sequence(pg_conn, table, "id")

    dst_total = pg_count(pg_conn, table)
    log(f"[{table}] target rows: {dst_total}")


def migrate_aux_tables(sqlite_conn, pg_conn, batch_size: int) -> None:
    log("[aux] start migrating metadata/audit/config tables")
    for table, spec in AUX_TABLE_SPECS.items():
        migrate_aux_table(
            sqlite_conn=sqlite_conn,
            pg_conn=pg_conn,
            table=table,
            columns=spec["columns"],
            conflict_cols=spec["conflict_cols"],
            update_cols=spec["update_cols"],
            batch_size=min(batch_size, 5000),
            row_transform=spec.get("transform"),
        )
    log("[aux] done")


def refresh_stats_cache(sqlite_conn, pg_conn) -> None:
    log("[stats_cache] refreshing total_products/manufacturers_count")
    total_products = sqlite_count(sqlite_conn, "products")
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute("SELECT COUNT(DISTINCT manufacturer) FROM products")
    manufacturers_count = int(cur_sqlite.fetchone()[0])
    updated_at = datetime.now().isoformat()

    rows = [
        ("total_products", str(total_products), updated_at),
        ("manufacturers_count", str(manufacturers_count), updated_at),
    ]
    with pg_conn.cursor() as cur_pg:
        execute_values(
            cur_pg,
            "INSERT INTO stats_cache (key, value, updated_at) VALUES %s "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            rows,
            page_size=100,
        )
        pg_conn.commit()

    log(
        "[stats_cache] refreshed "
        f"total_products={total_products}, manufacturers_count={manufacturers_count}"
    )


def parse_args():
    parser = argparse.ArgumentParser(description="SQLite -> PostgreSQL 迁移脚本（无 pgloader）")
    parser.add_argument(
        "--sqlite-path",
        default=os.getenv("SQLITE_DB_PATH", "udid_hybrid_lake.db"),
        help="SQLite 数据库路径",
    )
    parser.add_argument("--batch-size", type=int, default=5000, help="批量写入大小")
    parser.add_argument("--skip-products", action="store_true", help="跳过 products 表")
    parser.add_argument("--skip-embeddings", action="store_true", help="跳过 embeddings 表")
    parser.add_argument("--skip-sync-log", action="store_true", help="跳过 sync_log 表")
    parser.add_argument("--skip-aux", action="store_true", help="跳过元数据/审计/配置表迁移")
    parser.add_argument(
        "--skip-embedding-blob",
        action="store_true",
        help="跳过迁移 embeddings.embedding BLOB（默认迁移）",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.batch_size < 100 or args.batch_size > 50000:
        raise RuntimeError("--batch-size 建议范围 100~50000")

    sqlite_path = os.path.abspath(args.sqlite_path)
    if not os.path.exists(sqlite_path):
        raise RuntimeError(f"SQLite 文件不存在: {sqlite_path}")

    cfg = load_pg_config()
    log(f"source sqlite: {sqlite_path}")
    log(f"target pg: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname}")
    log(f"batch_size: {args.batch_size}")
    with_embedding_blob = not args.skip_embedding_blob
    log(f"with_embedding_blob: {with_embedding_blob}")

    sqlite_conn = connect_sqlite(sqlite_path)
    pg_conn = connect_postgres(cfg)
    try:
        if not args.skip_products:
            migrate_products(sqlite_conn, pg_conn, args.batch_size)
        if not args.skip_embeddings:
            migrate_embeddings(sqlite_conn, pg_conn, args.batch_size, with_embedding_blob)
        if not args.skip_sync_log:
            migrate_sync_log(sqlite_conn, pg_conn, args.batch_size)
        if not args.skip_aux:
            migrate_aux_tables(sqlite_conn, pg_conn, args.batch_size)
        refresh_stats_cache(sqlite_conn, pg_conn)
    finally:
        try:
            sqlite_conn.close()
        finally:
            pg_conn.close()

    log("migration done")


if __name__ == "__main__":
    main()
