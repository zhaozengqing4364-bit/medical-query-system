"""
Shared PostgreSQL query helpers for search and semantic recall.
"""

from typing import Iterable, List, Optional, Sequence, Tuple


DEFAULT_POSTGRES_FALLBACK_COLUMNS = (
    "manufacturer",
    "product_name",
    "commercial_name",
)


def collect_highlight_keywords(values: Iterable[str]) -> List[str]:
    seen = set()
    keywords: List[str] = []
    for raw in values:
        value = (raw or "").strip()
        if len(value) < 2 or value in seen:
            continue
        seen.add(value)
        keywords.append(value)
    return keywords


def build_keyword_or_clause(
    alias: str,
    keywords: Sequence[str],
    columns: Sequence[str],
    like_op: str,
) -> Tuple[str, List[str]]:
    prefix = f"{alias}." if alias else ""
    clauses = []
    params: List[str] = []
    for raw_kw in keywords:
        kw = (raw_kw or "").strip()
        if not kw:
            continue
        pattern = f"%{kw}%"
        field_clauses = []
        for col in columns:
            field_clauses.append(f"{prefix}{col} {like_op} ?")
            params.append(pattern)
        clauses.append(f"({' OR '.join(field_clauses)})")
    return (" OR ".join(clauses) if clauses else "1=1", params)


def detect_postgres_keyword_strategy(cursor, keyword: str) -> str:
    value = (keyword or "").strip()
    if not value:
        return "none"
    try:
        cursor.execute(
            "SELECT 1 FROM products WHERE search_vector @@ plainto_tsquery('chinese', ?) LIMIT 1",
            (value,),
        )
        return "fts" if cursor.fetchone() else "fallback"
    except Exception:
        return "fallback"


def build_postgres_keyword_clause(
    cursor,
    alias: str,
    keyword: str,
    like_op: str = "ILIKE",
    fallback_columns: Optional[Sequence[str]] = None,
) -> Tuple[str, List[str], str]:
    value = (keyword or "").strip()
    if not value:
        return "1=1", [], "none"

    strategy = detect_postgres_keyword_strategy(cursor, value)
    prefix = f"{alias}." if alias else ""

    if strategy == "fts":
        return (
            f"{prefix}search_vector @@ plainto_tsquery('chinese', ?)",
            [value],
            strategy,
        )

    columns = tuple(fallback_columns or DEFAULT_POSTGRES_FALLBACK_COLUMNS)
    clause, params = build_keyword_or_clause(alias, [value], columns, like_op)
    return clause, params, strategy


def build_postgres_keywords_clause(
    cursor,
    alias: str,
    keywords: Sequence[str],
    like_op: str = "ILIKE",
    fallback_columns: Optional[Sequence[str]] = None,
) -> Tuple[str, List[str], List[str]]:
    clauses = []
    params: List[str] = []
    strategies: List[str] = []

    for keyword in keywords:
        clause, clause_params, strategy = build_postgres_keyword_clause(
            cursor=cursor,
            alias=alias,
            keyword=keyword,
            like_op=like_op,
            fallback_columns=fallback_columns,
        )
        if clause == "1=1":
            continue
        clauses.append(f"({clause})")
        params.extend(clause_params)
        strategies.append(strategy)

    return (" OR ".join(clauses) if clauses else "1=1", params, strategies)
