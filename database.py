import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from instruments import infer_decimals

DB_PATH = Path(__file__).parent / "bot.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                threshold    REAL    NOT NULL,
                pair         TEXT    NOT NULL DEFAULT 'USDJPY',
                start_above  INTEGER,
                created_at   TEXT    NOT NULL,
                is_triggered INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Миграция со старой схемы (один алерт на пользователя + direction).
        # Старое направление переводим в start_above: "выше" — цена шла снизу
        # вверх (start_above=0), "ниже" — сверху вниз (start_above=1).
        cols = [row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()]
        if "direction" in cols:
            conn.execute("ALTER TABLE alerts RENAME TO alerts_old")
            conn.execute("""
                CREATE TABLE alerts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    threshold    REAL    NOT NULL,
                    pair         TEXT    NOT NULL DEFAULT 'USDJPY',
                    start_above  INTEGER,
                    created_at   TEXT    NOT NULL,
                    is_triggered INTEGER NOT NULL DEFAULT 0
                )
            """)
            # pair не указываем — старые алерты были по USD/JPY, подставится DEFAULT.
            conn.execute("""
                INSERT INTO alerts (user_id, threshold, start_above, created_at, is_triggered)
                SELECT user_id, threshold,
                       CASE direction WHEN 'ниже' THEN 1 ELSE 0 END,
                       created_at, is_triggered
                FROM alerts_old
            """)
            conn.execute("DROP TABLE alerts_old")
        # Для баз с новой схемой, но ещё без колонки pair — добавляем (миграция).
        try:
            conn.execute("ALTER TABLE alerts ADD COLUMN pair TEXT NOT NULL DEFAULT 'USDJPY'")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                user_name  TEXT,
                joined_at  TEXT    NOT NULL,
                consent    INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                is_active  INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.commit()


def save_user(chat_id: int, user_name: str) -> None:
    joined_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (chat_id, user_name, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_name = excluded.user_name
        """, (chat_id, user_name, joined_at))
        conn.commit()


def set_consent(chat_id: int, value: int) -> None:
    consent_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users SET consent = ?, consent_at = ? WHERE chat_id = ?
        """, (value, consent_at, chat_id))
        conn.commit()


def get_consent(chat_id: int) -> int | None:
    """Возвращает согласие одного пользователя: 1 (согласен), 0 (нет),
    или None — если такого пользователя в базе ещё нет."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT consent FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row is not None else None


def get_active_consented_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT chat_id FROM users WHERE consent = 1 AND is_active = 1
        """).fetchall()
    return [row[0] for row in rows]


def mark_inactive(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE chat_id = ?", (chat_id,))
        conn.commit()


def get_price_window(ticker: str, decimals: int | None = None, minutes: int = 7) -> dict:
    """Минимум, максимум и последняя цена инструмента за последние `minutes` минут
    по минутным свечам. `ticker` — любой символ Yahoo Finance.

    Зачем минимум/максимум, а не одна точка: проверка идёт раз в 5 минут, и если
    цена за это время сходила к уровню и вернулась («фитиль»), одна точка это
    пропустит. По свечам видно весь диапазон, куда цена заходила между проверками.
    Окно берём с запасом (7 мин > 5 мин интервала), чтобы не было дырки между
    соседними проверками.

    `decimals=None` (своя пара) — точность подбираем по цене через infer_decimals.
    Возвращает {low, high, last, decimals}. Бросает ValueError, если по тикеру нет
    данных — это используется для валидации своей пары при вводе.
    """
    tk = yf.Ticker(ticker)
    df = tk.history(period="1d", interval="1m")
    if df is None or df.empty:
        # Фолбэк: минутных свечей нет — пробуем одну текущую цену как точку.
        try:
            last = tk.fast_info.last_price
        except Exception:
            last = None
        if last is None:
            raise ValueError(f"нет данных по тикеру {ticker}")
        d = decimals if decimals is not None else infer_decimals(last)
        last = round(float(last), d)
        return {"low": last, "high": last, "last": last, "decimals": d}

    cutoff = pd.Timestamp.now(tz=df.index.tz) - pd.Timedelta(minutes=minutes)
    recent = df[df.index >= cutoff]
    if recent.empty:
        recent = df.tail(1)

    last = float(df["Close"].iloc[-1])
    d = decimals if decimals is not None else infer_decimals(last)
    return {
        "low": round(float(recent["Low"].min()), d),
        "high": round(float(recent["High"].max()), d),
        "last": round(last, d),
        "decimals": d,
    }


def get_pending_alerts() -> list[dict]:
    """Возвращает все несработавшие алерты (is_triggered = 0)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, user_id, threshold, pair, start_above
            FROM alerts WHERE is_triggered = 0
        """).fetchall()
    return [dict(row) for row in rows]


def set_alert_side(alert_id: int, start_above: int) -> None:
    """Запоминает, с какой стороны от уровня была цена при первой проверке."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE alerts SET start_above = ? WHERE id = ?", (start_above, alert_id))
        conn.commit()


def mark_alert_triggered(alert_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE alerts SET is_triggered = 1 WHERE id = ?", (alert_id,))
        conn.commit()


def add_alert(user_id: int, pair: str, threshold: float) -> None:
    """Добавляет новый алерт-уровень на инструмент `pair`. Алертов у пользователя
    может быть много. start_above = NULL — сторону цены проставит первая проверка."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO alerts (user_id, threshold, pair, start_above, created_at, is_triggered)
            VALUES (?, ?, ?, NULL, ?, 0)
        """, (user_id, threshold, pair, created_at))
        conn.commit()


def get_user_alerts(user_id: int) -> list[dict]:
    """Активные (несработавшие) алерты одного пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, threshold, pair FROM alerts
            WHERE user_id = ? AND is_triggered = 0
            ORDER BY pair, threshold
        """, (user_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_alert(alert_id: int, user_id: int) -> bool:
    """Удаляет алерт пользователя по id. Возвращает True, если что-то удалилось."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0
