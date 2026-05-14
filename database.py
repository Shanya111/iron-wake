import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL UNIQUE,
                threshold  REAL    NOT NULL,
                direction  TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        conn.commit()


def upsert_alert(user_id: int, threshold: float, direction: str) -> None:
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO alerts (user_id, threshold, direction, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                threshold  = excluded.threshold,
                direction  = excluded.direction,
                created_at = excluded.created_at
        """, (user_id, threshold, direction, created_at))
        conn.commit()
