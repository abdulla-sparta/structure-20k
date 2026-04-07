# engine/session.py
#
# IMPORTANT: Times are parsed on EVERY CALL, not at module import.
# The old version parsed at module load time which meant config changes
# during the same process (e.g. multiple backtest runs) were ignored.

from datetime import datetime
from config import CONFIG


def _t(s: str):
    """Parse HH:MM string to time object on demand."""
    return datetime.strptime(s, "%H:%M").time()


def is_entry_allowed(current_time) -> bool:
    return _t(CONFIG["entry_start_time"]) <= current_time <= _t(CONFIG["entry_end_time"])


def is_force_exit_time(current_time) -> bool:
    return current_time >= _t(CONFIG["force_exit_time"])