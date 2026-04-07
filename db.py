# db.py
#
# Single database layer for StructureEngine.
# ─────────────────────────────────────────────────────────────────────────────
# LOCAL DEV  : SQLite  (state/structureengine.db)  — zero config
# RAILWAY    : Postgres (DATABASE_URL env var)      — set in Railway dashboard
#
# Tables:
#   app_config    (key TEXT PK, value TEXT)          — locked_config, saved_configs
#   engine_state  (symbol TEXT PK, state_json TEXT)  — live engine state per symbol
#   trades        (id, symbol, date, trade_json TEXT) — daily trade log
#
# Usage:
#   from db import DB
#   DB.set("locked_config", {...})
#   cfg = DB.get("locked_config")
#   DB.set_engine_state("TRENT", {...})
#   st  = DB.get_engine_state("TRENT")
#   DB.save_trade("TRENT", trade_dict)
#   DB.get_trades("TRENT", date="2025-11-17")

import os
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ── Detect backend ────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith("postgres"))

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    log.info("DB backend: PostgreSQL")
else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(__file__), "state", "structureengine.db")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    log.info(f"DB backend: SQLite ({DB_PATH})")


# ── Connection helpers ────────────────────────────────────────────────────────

def _pg_conn():
    """New Postgres connection (short-lived, close after use)."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _sqlite_conn():
    return sqlite3.connect(DB_PATH)


def _conn():
    return _pg_conn() if USE_POSTGRES else _sqlite_conn()


# ── Schema init ───────────────────────────────────────────────────────────────

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_state (
    symbol     TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS trades (
    id         SERIAL PRIMARY KEY,
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    trade_json TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_sym_date ON trades(symbol, date);
"""

_SCHEMA_SQ = """
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_state (
    symbol     TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    trade_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_sym_date ON trades(symbol, date);
"""


def init_db():
    """Create tables if they don't exist. Call once at app startup."""
    schema = _SCHEMA_PG if USE_POSTGRES else _SCHEMA_SQ
    with _conn() as cx:
        if USE_POSTGRES:
            with cx.cursor() as cur:
                cur.execute(schema)
        else:
            cx.executescript(schema)
    log.info("DB schema initialised.")


# ── app_config: locked_config + saved_configs ─────────────────────────────────

def get(key: str, default=None):
    """Load a JSON value by key from app_config."""
    try:
        init_db()   # ensure tables exist (safe to call multiple times)
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
                    row = cur.fetchone()
            else:
                cur = cx.execute("SELECT value FROM app_config WHERE key=?", (key,))
                row = cur.fetchone()
        return json.loads(row[0]) if row else default
    except Exception as e:
        log.error(f"DB.get({key}): {e}")
        return default


def set(key: str, value):
    """Store a JSON-serialisable value in app_config (upsert)."""
    try:
        init_db()   # ensure tables exist (safe to call multiple times)
        raw = json.dumps(value, default=str)
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute("""
                        INSERT INTO app_config(key, value) VALUES(%s,%s)
                        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
                    """, (key, raw))
            else:
                cx.execute("""
                    INSERT INTO app_config(key, value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """, (key, raw))
    except Exception as e:
        log.error(f"DB.set({key}): {e}")


# ── engine_state: live broker state per symbol ────────────────────────────────

def get_engine_state(symbol: str) -> dict | None:
    """Load broker/engine state for a symbol."""
    try:
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute("SELECT state_json FROM engine_state WHERE symbol=%s", (symbol,))
                    row = cur.fetchone()
            else:
                cur = cx.execute("SELECT state_json FROM engine_state WHERE symbol=?", (symbol,))
                row = cur.fetchone()
        return json.loads(row[0]) if row else None
    except Exception as e:
        log.error(f"DB.get_engine_state({symbol}): {e}")
        return None


def set_engine_state(symbol: str, state: dict):
    """Upsert broker/engine state for a symbol."""
    try:
        raw = json.dumps(state, default=str)
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute("""
                        INSERT INTO engine_state(symbol, state_json) VALUES(%s,%s)
                        ON CONFLICT(symbol) DO UPDATE
                        SET state_json=EXCLUDED.state_json, updated_at=NOW()
                    """, (symbol, raw))
            else:
                cx.execute("""
                    INSERT INTO engine_state(symbol, state_json) VALUES(?,?)
                    ON CONFLICT(symbol) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=datetime('now')
                """, (symbol, raw))
    except Exception as e:
        log.error(f"DB.set_engine_state({symbol}): {e}")


def clear_engine_state(symbol: str):
    """Delete engine state for a symbol (called on live session end)."""
    try:
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute("DELETE FROM engine_state WHERE symbol=%s", (symbol,))
            else:
                cx.execute("DELETE FROM engine_state WHERE symbol=?", (symbol,))
    except Exception as e:
        log.error(f"DB.clear_engine_state({symbol}): {e}")


# ── trades: daily trade log ───────────────────────────────────────────────────

def save_trade(symbol: str, trade: dict, date_str: str = None):
    """
    Append one closed trade to the trades table.

    Deduplication: skip if an identical trade (same symbol, date, entry_time,
    exit_time, direction) already exists. This prevents double-writes caused by
    engine restarts or duplicate close calls restoring state from JSON.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    entry_time = str(trade.get("entry_time", ""))
    exit_time  = str(trade.get("exit_time",  ""))
    direction  = str(trade.get("direction",  trade.get("side", "")))

    try:
        raw = json.dumps(trade, default=str)
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    # Check for existing identical trade before inserting
                    cur.execute("""
                        SELECT id FROM trades
                        WHERE symbol=%s AND date=%s
                          AND trade_json::json->>'entry_time' = %s
                          AND trade_json::json->>'exit_time'  = %s
                          AND trade_json::json->>'direction'  = %s
                        LIMIT 1
                    """, (symbol, date_str, entry_time, exit_time, direction))
                    if cur.fetchone():
                        log.warning(
                            f"DB.save_trade({symbol}): duplicate skipped "
                            f"[{direction} {entry_time}→{exit_time}]"
                        )
                        return
                    cur.execute(
                        "INSERT INTO trades(symbol,date,trade_json) VALUES(%s,%s,%s)",
                        (symbol, date_str, raw)
                    )
            else:
                # SQLite: use json_extract for dedup check
                cur = cx.execute("""
                    SELECT id FROM trades
                    WHERE symbol=? AND date=?
                      AND json_extract(trade_json,'$.entry_time') = ?
                      AND json_extract(trade_json,'$.exit_time')  = ?
                      AND json_extract(trade_json,'$.direction')  = ?
                    LIMIT 1
                """, (symbol, date_str, entry_time, exit_time, direction))
                if cur.fetchone():
                    log.warning(
                        f"DB.save_trade({symbol}): duplicate skipped "
                        f"[{direction} {entry_time}→{exit_time}]"
                    )
                    return
                cx.execute(
                    "INSERT INTO trades(symbol,date,trade_json) VALUES(?,?,?)",
                    (symbol, date_str, raw)
                )
    except Exception as e:
        log.error(f"DB.save_trade({symbol}): {e}")


def get_trades(symbol: str, date_str: str = None) -> list[dict]:
    """Load trades for a symbol, optionally filtered by date."""
    try:
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    if date_str:
                        cur.execute(
                            "SELECT trade_json FROM trades WHERE symbol=%s AND date=%s ORDER BY id",
                            (symbol, date_str)
                        )
                    else:
                        cur.execute(
                            "SELECT trade_json FROM trades WHERE symbol=%s ORDER BY id",
                            (symbol,)
                        )
                    rows = cur.fetchall()
            else:
                if date_str:
                    cur = cx.execute(
                        "SELECT trade_json FROM trades WHERE symbol=? AND date=? ORDER BY id",
                        (symbol, date_str)
                    )
                else:
                    cur = cx.execute(
                        "SELECT trade_json FROM trades WHERE symbol=? ORDER BY id",
                        (symbol,)
                    )
                rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception as e:
        log.error(f"DB.get_trades({symbol}): {e}")
        return []


def get_all_trades_today(date_str: str = None) -> dict[str, list]:
    """Load today's trades for ALL symbols. Returns {symbol: [trades]}."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    try:
        with _conn() as cx:
            if USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute(
                        "SELECT symbol, trade_json FROM trades WHERE date=%s ORDER BY id",
                        (date_str,)
                    )
                    rows = cur.fetchall()
            else:
                cur = cx.execute(
                    "SELECT symbol, trade_json FROM trades WHERE date=? ORDER BY id",
                    (date_str,)
                )
                rows = cur.fetchall()
        result: dict[str, list] = {}
        for sym, raw in rows:
            result.setdefault(sym, []).append(json.loads(raw))
        return result
    except Exception as e:
        log.error(f"DB.get_all_trades_today: {e}")
        return {}