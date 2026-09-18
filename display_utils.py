from datetime import datetime, timezone
from zoneinfo import ZoneInfo


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


def format_korea_datetime(database_timestamp):
    """SQLite UTC 생성 시각을 한국 표준시로 바꿔 표시한다."""
    if not database_timestamp:
        return "알 수 없음"

    try:
        parsed = datetime.fromisoformat(database_timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        korea_time = parsed.astimezone(KOREA_TIMEZONE)
    except (TypeError, ValueError):
        return str(database_timestamp)

    return korea_time.strftime("%Y-%m-%d %H:%M:%S (한국 시간)")
