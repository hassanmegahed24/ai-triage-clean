# app/utils/time.py
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")

def now_et() -> datetime:
    """Timezone-aware 'now' in Eastern Time."""
    return datetime.now(ET)