from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
AUTO_SYNC_PUBLIC_KEYS = (
    "auto_sync_enabled",
    "auto_sync_schedule",
    "auto_sync_time",
    "auto_sync_weekday",
    "auto_sync_type",
)
VALID_SCHEDULES = {"daily", "weekly"}
VALID_SYNC_TYPES = {"full", "data", "vectors"}


def _parse_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_auto_sync_settings(raw: dict | None) -> dict:
    payload = dict(raw or {})

    schedule = str(payload.get("auto_sync_schedule") or "daily").strip().lower()
    if schedule not in VALID_SCHEDULES:
        schedule = "daily"

    sync_type = str(payload.get("auto_sync_type") or "full").strip().lower()
    if sync_type not in VALID_SYNC_TYPES:
        sync_type = "full"

    time_value = str(payload.get("auto_sync_time") or "02:00").strip()
    if len(time_value) != 5 or time_value[2] != ":":
        time_value = "02:00"
    try:
        hour = int(time_value[:2])
        minute = int(time_value[3:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        hour = 2
        minute = 0
        time_value = "02:00"

    try:
        weekday = int(payload.get("auto_sync_weekday") or 1)
    except (TypeError, ValueError):
        weekday = 1
    if weekday < 1 or weekday > 7:
        weekday = 1

    return {
        "auto_sync_enabled": _parse_bool(payload.get("auto_sync_enabled")),
        "auto_sync_schedule": schedule,
        "auto_sync_time": f"{hour:02d}:{minute:02d}",
        "auto_sync_weekday": weekday,
        "auto_sync_type": sync_type,
        "timezone": DEFAULT_TIMEZONE,
    }


def get_timezone() -> ZoneInfo:
    return ZoneInfo(DEFAULT_TIMEZONE)


def compute_next_run(settings: dict, now: datetime | None = None) -> datetime | None:
    config = normalize_auto_sync_settings(settings)
    if not config["auto_sync_enabled"]:
        return None

    tz = get_timezone()
    current = now.astimezone(tz) if now else datetime.now(tz)
    hour, minute = map(int, config["auto_sync_time"].split(":"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if config["auto_sync_schedule"] == "daily":
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    target_weekday = config["auto_sync_weekday"] - 1  # Python Monday=0
    days_ahead = (target_weekday - current.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    if candidate <= current:
        candidate += timedelta(days=7)
    return candidate


def compute_next_run_iso(settings: dict, now: datetime | None = None) -> str:
    next_run = compute_next_run(settings, now=now)
    return next_run.isoformat() if next_run else ""


def get_due_slot_id(settings: dict, now: datetime | None = None) -> str:
    config = normalize_auto_sync_settings(settings)
    if not config["auto_sync_enabled"]:
        return ""

    tz = get_timezone()
    current = now.astimezone(tz) if now else datetime.now(tz)
    hour, minute = map(int, config["auto_sync_time"].split(":"))

    if current.hour != hour or current.minute != minute:
        return ""

    if config["auto_sync_schedule"] == "weekly":
        target_weekday = config["auto_sync_weekday"] - 1
        if current.weekday() != target_weekday:
            return ""

    return current.replace(second=0, microsecond=0).isoformat()


def format_schedule_summary(settings: dict) -> str:
    config = normalize_auto_sync_settings(settings)
    if not config["auto_sync_enabled"]:
        return "自动更新已关闭"

    sync_type_map = {
        "full": "完整同步",
        "data": "仅同步数据",
        "vectors": "仅生成向量",
    }
    weekday_map = {
        1: "每周一",
        2: "每周二",
        3: "每周三",
        4: "每周四",
        5: "每周五",
        6: "每周六",
        7: "每周日",
    }

    type_label = sync_type_map.get(config["auto_sync_type"], "完整同步")
    if config["auto_sync_schedule"] == "daily":
        return f"每天 {config['auto_sync_time']} 自动执行{type_label}"
    return f"{weekday_map[config['auto_sync_weekday']]} {config['auto_sync_time']} 自动执行{type_label}"
