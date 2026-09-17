import sqlite3
from datetime import datetime
from pathlib import Path

import config

DB_PATH = Path(__file__).parent / "bot.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # Алерты «касание уровня». Убирались из бота 20 августа 2026 и вернулись
        # 26 августа — уже с проверкой по бирже, а не по Yahoo (см. alerts.py).
        # Таблицу тогда намеренно НЕ дропали, поэтому в боевой базе она со старыми
        # записями; IF NOT EXISTS их не трогает. pair — код инструмента из реестра
        # ИЛИ символ контракта BingX («WIF/USDT:USDT») для своей пары, как в trades.
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
        # Чистка алертов ДОАВГУСТОВСКОЙ эпохи (решение владельца 26 августа 2026).
        # Шесть дней их никто не проверял, а сторона цены (start_above) у них записана
        # ещё по котировкам Yahoo — на биржевой цене половина сработала бы сразу же и
        # завалила людей уведомлениями по уровням, о которых те давно забыли.
        #
        # Гасим, а не удаляем: is_triggered = 1 убирает их из проверки и из /myalerts,
        # но строки остаются на месте — DELETE необратим, как и DROP таблицы, которую
        # по той же причине не тронули в августе.
        #
        # Условие по дате делает миграцию самоограниченной: она НЕ МОЖЕТ задеть алерт,
        # поставленный после возврата функции, поэтому безопасно выполняется при каждом
        # старте бота и не нуждается во флаге «уже отработала».
        conn.execute("""
            UPDATE alerts SET is_triggered = 1
            WHERE is_triggered = 0 AND created_at < '2026-08-26'
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                user_name  TEXT,
                joined_at  TEXT    NOT NULL,
                consent    INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                is_active  INTEGER NOT NULL DEFAULT 1,
                access     TEXT    NOT NULL DEFAULT 'pending'  -- 'pending'|'approved'|'denied'
            )
        """)
        # Доступ по подтверждению админом. При ПЕРВОМ добавлении колонки (старая база)
        # все, кто уже зарегистрирован, одобряются автоматически — чтобы правка не
        # выкинула текущих пользователей. На свежей базе колонка уже в CREATE → ALTER
        # бросит OperationalError, и grandfather-UPDATE не выполнится (он и не нужен).
        try:
            conn.execute("ALTER TABLE users ADD COLUMN access TEXT NOT NULL DEFAULT 'pending'")
            conn.execute("UPDATE users SET access = 'approved'")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        # ── Торговый движок (VSA + Spring) ───────────────────────────────────
        # Уровни контекстного анализа (стратегия №1). Перезаписываются при каждом
        # анализе инструмента (см. save_levels): старые удаляем, новые пишем.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS levels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument   TEXT    NOT NULL,
                timeframe    TEXT    NOT NULL,          -- 'D1' | 'H1'
                price        REAL    NOT NULL,
                type         TEXT    NOT NULL,          -- 'support' | 'resistance' | 'liquidity'
                strength     TEXT    NOT NULL,          -- 'strong' | 'weak'
                is_liquidity INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL
            )
        """)
        # Торговые сигналы (стратегия №2 — Spring/Upthrust).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument  TEXT    NOT NULL,
                pattern     TEXT    NOT NULL DEFAULT 'spring',  -- 'spring' | 'upthrust'
                level_id    INTEGER,
                direction   TEXT    NOT NULL,                   -- 'long' | 'short'
                entry_price REAL    NOT NULL,
                stop_loss   REAL    NOT NULL,
                take_profit REAL    NOT NULL,
                priority    TEXT    NOT NULL DEFAULT 'normal',  -- 'high' | 'normal'
                status      TEXT    NOT NULL DEFAULT 'pending', -- pending|hit_tp|hit_sl|expired
                created_at  TEXT    NOT NULL
            )
        """)
        # Якорь свечи пробоя — нужен трекингу исхода (2-я волна). Для баз со старой
        # схемой добавляем колонку миграцией (у старых сигналов будет NULL).
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN bar_time TEXT")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        # Владелец сигнала. Сигналы теперь персональные: каждый подписчик получает их
        # по своим порогам (см. user_settings). Старые сигналы — NULL (общие, до правки):
        # на исходе таких уведомляем всех текущих подписчиков (обратная совместимость).
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN user_id INTEGER")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        # ── Лимитный вход (переход на фьючерсы BingX) ──────────────────────────
        # Раньше вход был по закрытию свечи пробоя, то есть по рынку. Теперь бот
        # выдаёт цену ЛИМИТНОЙ заявки, и у сигнала появляется состояние ДО входа:
        #   waiting_fill     — заявка выставлена, цена до неё не дошла;
        #   filled           — заявка исполнилась, ждём цель/стоп;
        #   hit_tp / hit_sl / expired — как раньше, но отсчёт от момента ИСПОЛНЕНИЯ;
        #   expired_unfilled — цена до заявки так и не дошла, сделки НЕ БЫЛО.
        # fill_time — момент исполнения (якорь трекинга вместо bar_time),
        # signal_price — закрытие свечи пробоя (для сообщения: «сигнал по X,
        # заявка на Y»).
        for column, kind in (("fill_time", "TEXT"), ("signal_price", "REAL")):
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        # Старые сигналы были рыночным входом — то есть заведомо исполненными.
        # Переименовываем их статус, чтобы 'pending' не означал сразу две разные
        # вещи. Запрос идемпотентный: новых 'pending' мы больше не пишем.
        conn.execute("UPDATE signals SET status = 'filled' WHERE status = 'pending'")
        # Подписки пользователей на сигналы по инструменту.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                instrument TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                UNIQUE(user_id, instrument)
            )
        """)
        # ── Подписка по стратегиям (15.09.2026) ─────────────────────────────────
        # Подписка — пара «инструмент + стратегия»: одну монету можно вести по ложному
        # пробою, другую по тренду. Таблица subscriptions выше с этого дня не пишется
        # и не читается — осталась источником разовой миграции (DROP необратим).
        #
        # Миграция выполняется ровно один раз — когда таблицы ещё нет. Проверять
        # «таблица пустая» нельзя: человек, снявший все подписки, получил бы их обратно
        # при следующем рестарте. Старая подписка становится подпиской на ОБЕ стратегии,
        # кроме тех, что человек успел выключить общей галочкой (strategy_off — первая
        # версия галочек, прожила несколько часов 15.09.2026).
        fresh = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'signal_subscriptions'"
        ).fetchone() is None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_subscriptions (
                user_id    INTEGER NOT NULL,
                instrument TEXT    NOT NULL,
                strategy   TEXT    NOT NULL,   -- код из config.STRATEGIES: spring | trend
                created_at TEXT    NOT NULL,
                UNIQUE(user_id, instrument, strategy)
            )
        """)
        if fresh:
            has_off = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategy_off'"
            ).fetchone() is not None
            for strategy in config.STRATEGIES:
                sql = """
                    INSERT OR IGNORE INTO signal_subscriptions
                        (user_id, instrument, strategy, created_at)
                    SELECT user_id, instrument, ?, created_at FROM subscriptions
                """
                params: tuple = (strategy,)
                if has_off:
                    sql += " WHERE user_id NOT IN (SELECT user_id FROM strategy_off WHERE strategy = ?)"
                    params = (strategy, strategy)
                conn.execute(sql, params)
        # Журнал сделок пользователя (записывается свободным текстом через LLM).
        # instrument — код инструмента движка ИЛИ символ контракта BingX (своя пара; до
        # 26.08.2026 тут были тикеры Yahoo — те записи читаются, но не ведутся). bar_time —
        # момент записи (UTC), якорь для трекинга исхода по свечам. Журнал НЕ истекает
        # сам: сделка висит 'open', пока не дойдёт до цели/стопа или её не закроют вручную.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                instrument  TEXT    NOT NULL,
                direction   TEXT    NOT NULL,                  -- 'long' | 'short'
                entry_price REAL    NOT NULL,
                stop_loss   REAL    NOT NULL,
                take_profit REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open',   -- open|hit_tp|hit_sl|closed
                bar_time    TEXT,
                note        TEXT,
                opened_at   TEXT    NOT NULL,
                closed_at   TEXT
            )
        """)
        # Персональные пороги движка: подписчик переопределяет общие значения под себя.
        # key — из config.TUNABLE (MAX_ENTRY_DIST_ATR / MAX_RISK_ATR), value — число.
        # Чего тут нет — берётся из общих настроек (settings.json / дефолтов).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key     TEXT    NOT NULL,
                value   REAL    NOT NULL,
                UNIQUE(user_id, key)
            )
        """)
        # ── Стратегия №4 — тренд по недельному каналу (14.09.2026) ────────────
        # Позиция модели, ОДНА на инструмент и общая для всех (у стратегии нет личных
        # настроек). В signals не кладём: там цель обязательна, а у тренда её нет —
        # выход по встречному каналу. status: open | stop | exit. result_r — итог в
        # рисках по ценам входа и выхода, без комиссии и фандинга.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trend_positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument  TEXT    NOT NULL,
                direction   TEXT    NOT NULL,                 -- 'long' | 'short'
                entry_price REAL    NOT NULL,
                stop_loss   REAL    NOT NULL,
                entry_time  TEXT    NOT NULL,                 -- открытие часа входа, UTC
                signal_bar  TEXT    NOT NULL,                 -- свеча пробоя недели, UTC
                status      TEXT    NOT NULL DEFAULT 'open',  -- open | stop | exit |
                                                             -- manual (закрыто рукой
                                                             -- владельца, 17.09.2026)
                exit_price  REAL,
                exit_time   TEXT,
                result_r    REAL,
                created_at  TEXT    NOT NULL,
                closed_at   TEXT
            )
        """)
        # Последняя обработанная свеча по инструменту. С неё monitor_trend продолжает
        # после рестарта и простоя, свеча за свечой, ничего не пропуская и не повторяя.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trend_state (
                instrument TEXT PRIMARY KEY,
                last_bar   TEXT
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
                user_name = excluded.user_name,
                is_active = 1
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


# ── Доступ по подтверждению админом ──────────────────────────────────────────

def get_access(chat_id: int) -> str | None:
    """Статус доступа пользователя: 'pending' | 'approved' | 'denied',
    или None — если пользователя ещё нет в базе."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT access FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row is not None else None


def set_access(chat_id: int, value: str) -> None:
    """Меняет статус доступа: 'approved' (одобрить) / 'denied' (отклонить) / 'pending'."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET access = ? WHERE chat_id = ?", (value, chat_id))
        conn.commit()


def get_pending_users() -> list[dict]:
    """Заявки на доступ, ожидающие решения админа (для команды /requests)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT chat_id, user_name FROM users
            WHERE access = 'pending' ORDER BY joined_at
        """).fetchall()
    return [dict(row) for row in rows]


def get_all_users() -> list[dict]:
    """Все пользователи со статусами (для админской команды /users).
    Сортировка: сначала ожидающие, затем одобренные, затем отклонённые."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT chat_id, user_name, access, consent, is_active FROM users
            ORDER BY CASE access WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                     joined_at
        """).fetchall()
    return [dict(row) for row in rows]


# ── Алерты «касание уровня» ─────────────────────────────────────────────────

def get_pending_alerts() -> list[dict]:
    """Все несработавшие алерты (is_triggered = 0) — их проверяет scheduler."""
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
    """Добавляет алерт-уровень на инструмент `pair`. Алертов у пользователя может быть
    много. start_above = NULL — сторону цены проставит первая проверка."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO alerts (user_id, threshold, pair, start_above, created_at, is_triggered)
            VALUES (?, ?, ?, NULL, ?, 0)
        """, (user_id, threshold, pair, created_at))
        conn.commit()


def get_user_alerts(user_id: int, pair: str | None = None) -> list[dict]:
    """Активные (несработавшие) алерты пользователя. pair — только по одному
    инструменту (нужно /analyze: отметить уровни, на которые алерт уже стоит)."""
    sql = "SELECT id, threshold, pair FROM alerts WHERE user_id = ? AND is_triggered = 0"
    args: list = [user_id]
    if pair is not None:
        sql += " AND pair = ?"
        args.append(pair)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql + " ORDER BY pair, threshold", args).fetchall()
    return [dict(row) for row in rows]


def delete_alert(alert_id: int, user_id: int) -> bool:
    """Удаляет алерт пользователя по id. True — если что-то удалилось."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


# ── Уровни (контекстный анализ, стратегия №1) ───────────────────────────────

def save_levels(instrument: str, levels: list[dict]) -> None:
    """Перезаписывает уровни инструмента: старые удаляем, новые вставляем одной
    транзакцией. Каждый уровень — dict с price/type/strength/is_liquidity/timeframe."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM levels WHERE instrument = ?", (instrument,))
        conn.executemany("""
            INSERT INTO levels (instrument, timeframe, price, type, strength, is_liquidity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (instrument, lvl.get("timeframe", "H1"), lvl["price"], lvl["type"],
             lvl.get("strength", "weak"), int(lvl.get("is_liquidity", 0)), created_at)
            for lvl in levels
        ])
        conn.commit()


def get_levels(instrument: str) -> list[dict]:
    """Все сохранённые уровни инструмента (для мониторинга паттернов и /analyze)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, instrument, timeframe, price, type, strength, is_liquidity
            FROM levels WHERE instrument = ? ORDER BY price
        """, (instrument,)).fetchall()
    return [dict(row) for row in rows]


# ── Сигналы (стратегия №2 — Spring/Upthrust) ────────────────────────────────

def add_signal(instrument: str, pattern: str, direction: str, entry_price: float,
               stop_loss: float, take_profit: float, priority: str = "normal",
               level_id: int | None = None, bar_time: str | None = None,
               user_id: int | None = None, signal_price: float | None = None) -> int:
    """Сохраняет новый сигнал со статусом 'waiting_fill'. Возвращает его id.

    entry_price — цена ЛИМИТНОЙ заявки (её и ставит пользователь), поэтому сигнал
    рождается «заявка выставлена, ещё не исполнена». Сделка начнётся, только когда
    цена дойдёт до заявки (scheduler.track_signals переведёт в 'filled').
    bar_time — время свечи пробоя (UTC), от него отсчитывается срок жизни заявки.
    signal_price — закрытие свечи пробоя (для сообщения и статистики).
    user_id — владелец (сигнал персональный, по его порогам)."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO signals (instrument, pattern, level_id, direction,
                                 entry_price, stop_loss, take_profit, priority,
                                 status, created_at, bar_time, user_id, signal_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting_fill', ?, ?, ?, ?)
        """, (instrument, pattern, level_id, direction, entry_price, stop_loss,
              take_profit, priority, created_at, bar_time, user_id, signal_price))
        conn.commit()
        return cur.lastrowid


# Статусы сигнала, у которых ещё может измениться исход: заявка либо ждёт
# исполнения, либо сделка открыта. Всё остальное — терминальные состояния.
OPEN_SIGNAL_STATUSES = ("waiting_fill", "filled")


def get_open_signals() -> list[dict]:
    """Сигналы, за которыми ещё следим: заявка ждёт исполнения либо сделка открыта."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, instrument, direction, entry_price, stop_loss, take_profit,
                   bar_time, fill_time, signal_price, status, user_id
            FROM signals WHERE status IN ('waiting_fill', 'filled')
        """).fetchall()
    return [dict(row) for row in rows]


def update_signal_status(signal_id: int, status: str) -> None:
    """Меняет статус сигнала (waiting_fill/filled → hit_tp | hit_sl | expired |
    expired_unfilled)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
        conn.commit()


def mark_signal_filled(signal_id: int, fill_time: str) -> None:
    """Заявка исполнилась: сделка открыта, а fill_time — точка отсчёта её исхода.

    Отсчитывать исход от свечи пробоя после лимитного входа нельзя: между сигналом
    и входом проходит до ENTRY_WAIT_BARS часов, и на этом отрезке цена уже могла
    сходить к стопу — но нас там ещё не было."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE signals SET status = 'filled', fill_time = ? WHERE id = ?",
            (fill_time, signal_id),
        )
        conn.commit()


def recent_signal_exists(instrument: str, pattern: str, direction: str, since_iso: str,
                         user_id: int | None = None) -> bool:
    """Есть ли уже такой сигнал не старше since_iso — защита от дублей в мониторинге.
    Дедуп персональный: проверяем по конкретному пользователю (user_id)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT 1 FROM signals
            WHERE instrument = ? AND pattern = ? AND direction = ? AND created_at >= ?
              AND user_id IS ?
            LIMIT 1
        """, (instrument, pattern, direction, since_iso, user_id)).fetchone()
    return row is not None


def get_recent_signals(user_id: int, limit: int = 10) -> list[dict]:
    """Последние сигналы пользователя (для команды /signals). Берём его персональные
    сигналы плюс старые «общие» (user_id IS NULL, до перехода на персональные)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT instrument, pattern, direction, entry_price, stop_loss, take_profit,
                   priority, status, created_at, signal_price, fill_time
            FROM signals
            WHERE user_id = ? OR user_id IS NULL
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit)).fetchall()
    return [dict(row) for row in rows]


def get_signals_since(user_id: int, since: str | None = None) -> list[dict]:
    """Все сигналы пользователя (плюс старые «общие» user_id IS NULL) для сводной
    статистики /stats — без LIMIT, считаем по всей истории. since — ISO-дата
    (берём created_at >= since); None → вся история без фильтра по дате.
    created_at хранится строкой ISO 8601 фиксированного формата, поэтому
    лексикографическое сравнение = хронологическое."""
    query = """
        SELECT instrument, direction, entry_price, stop_loss, take_profit, status
        FROM signals
        WHERE (user_id = ? OR user_id IS NULL)
    """
    params: list = [user_id]
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ── Стратегия №4 — тренд по недельному каналу ───────────────────────────────

def get_trend_state(instrument: str) -> tuple[str | None, dict | None]:
    """(последняя обработанная свеча, открытая позиция модели или None)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT last_bar FROM trend_state WHERE instrument = ?", (instrument,)
        ).fetchone()
        pos = conn.execute("""
            SELECT id, instrument, direction, entry_price, stop_loss, entry_time, signal_bar
            FROM trend_positions WHERE instrument = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1
        """, (instrument,)).fetchone()
    return (row["last_bar"] if row else None), (dict(pos) if pos else None)


def save_trend_step(instrument: str, last_bar: str | None, events: list[dict],
                    open_id: int | None) -> None:
    """Пишет итог одного шага модели ОДНОЙ транзакцией: события и новую last_bar.

    Одной — потому что иначе падение между записью сделки и записью last_bar при
    следующем запуске повторило бы ту же свечу и открыло бы позицию дважды.
    open_id — id позиции, открытой до шага: к ней относится первое закрытие.
    За один шаг бывает несколько событий (после простоя бота: вход, стоп, новый вход)."""
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        current = open_id
        for ev in events:
            if ev["type"] == "entry":
                cur = conn.execute("""
                    INSERT INTO trend_positions (instrument, direction, entry_price, stop_loss,
                                                 entry_time, signal_bar, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """, (instrument, ev["direction"], ev["entry_price"], ev["stop_loss"],
                      ev["entry_time"], ev["signal_bar"], now))
                current = cur.lastrowid
            elif current is not None:
                conn.execute("""
                    UPDATE trend_positions
                    SET status = ?, exit_price = ?, exit_time = ?, result_r = ?, closed_at = ?
                    WHERE id = ?
                """, (ev["type"], ev["exit_price"], ev["exit_time"], ev["result_r"], now, current))
                current = None
        if last_bar is not None:
            conn.execute("INSERT OR REPLACE INTO trend_state (instrument, last_bar) VALUES (?, ?)",
                         (instrument, last_bar))
        conn.commit()


def get_trend_positions() -> list[dict]:
    """Все позиции модели тренда, новые первыми (для /trend)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, instrument, direction, entry_price, stop_loss, entry_time, status,
                   exit_price, exit_time, result_r
            FROM trend_positions ORDER BY id DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ── Подписки на сигналы ─────────────────────────────────────────────────────

# Подписка — пара «инструмент + стратегия» (таблица signal_subscriptions, 15.09.2026).
# strategy=None в функциях ниже значит «по всем стратегиям»: так подписывает NL-роутер
# («подпиши на эфир»), где стратегию человек не называет.

def _strategies(strategy: str | None) -> list[str]:
    return list(config.STRATEGIES) if strategy is None else [strategy]


def add_subscription(user_id: int, instrument: str, strategy: str | None = None) -> None:
    """Подписать на сигналы инструмента по стратегии (None — по всем)."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO signal_subscriptions (user_id, instrument, strategy, created_at)
            VALUES (?, ?, ?, ?)
        """, [(user_id, instrument, s, created_at) for s in _strategies(strategy)])
        conn.commit()


def remove_subscription(user_id: int, instrument: str, strategy: str | None = None) -> None:
    """Отписать от сигналов инструмента по стратегии (None — по всем)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "DELETE FROM signal_subscriptions WHERE user_id = ? AND instrument = ? AND strategy = ?",
            [(user_id, instrument, s) for s in _strategies(strategy)],
        )
        conn.commit()


def set_strategy_instruments(user_id: int, strategy: str, instruments: list[str]) -> None:
    """Оставить по стратегии РОВНО этот набор инструментов («отметить все» / «снять все»)."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM signal_subscriptions WHERE user_id = ? AND strategy = ?",
                     (user_id, strategy))
        conn.executemany("""
            INSERT OR IGNORE INTO signal_subscriptions (user_id, instrument, strategy, created_at)
            VALUES (?, ?, ?, ?)
        """, [(user_id, code, strategy, created_at) for code in instruments])
        conn.commit()


def get_user_subscriptions(user_id: int, strategy: str | None = None) -> list[str]:
    """Коды инструментов, на которые подписан пользователь (по стратегии или по любой)."""
    with sqlite3.connect(DB_PATH) as conn:
        if strategy is None:
            rows = conn.execute(
                "SELECT DISTINCT instrument FROM signal_subscriptions WHERE user_id = ?",
                (user_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT instrument FROM signal_subscriptions WHERE user_id = ? AND strategy = ?",
                (user_id, strategy)).fetchall()
    return [row[0] for row in rows]


def get_subscribers(instrument: str, strategy: str | None = None) -> list[int]:
    """user_id подписчиков инструмента по стратегии — кому слать её сигнал.
    None — подписанные по любой стратегии."""
    with sqlite3.connect(DB_PATH) as conn:
        if strategy is None:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM signal_subscriptions WHERE instrument = ?",
                (instrument,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id FROM signal_subscriptions WHERE instrument = ? AND strategy = ?",
                (instrument, strategy)).fetchall()
    return [row[0] for row in rows]


def get_subscribed_instruments(strategy: str | None = None) -> list[str]:
    """Уникальные инструменты с хотя бы одной подпиской (по стратегии или по любой)."""
    with sqlite3.connect(DB_PATH) as conn:
        if strategy is None:
            rows = conn.execute("SELECT DISTINCT instrument FROM signal_subscriptions").fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT instrument FROM signal_subscriptions WHERE strategy = ?",
                (strategy,)).fetchall()
    return [row[0] for row in rows]


# ── Персональные пороги движка ──────────────────────────────────────────────

def get_user_settings(user_id: int) -> dict:
    """Личные переопределения порогов пользователя (key → value). Чего нет —
    берётся из общих настроек (см. config.effective)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def set_user_setting(user_id: int, key: str, value: float) -> None:
    """Задать (или обновить) личный порог пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
        """, (user_id, key, value))
        conn.commit()


def reset_user_settings(user_id: int) -> None:
    """Сбросить все личные пороги пользователя — вернуться к общим значениям."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()


# ── Журнал сделок ────────────────────────────────────────────────────────────

def add_trade(user_id: int, instrument: str, direction: str, entry_price: float,
              stop_loss: float, take_profit: float, bar_time: str,
              note: str | None = None) -> int:
    """Записывает сделку в журнал (status='open'). Возвращает её id.
    bar_time — момент записи (UTC ISO), якорь для трекинга исхода по свечам."""
    opened_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO trades (user_id, instrument, direction, entry_price, stop_loss,
                                take_profit, status, bar_time, note, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """, (user_id, instrument, direction, entry_price, stop_loss, take_profit,
              bar_time, note, opened_at))
        conn.commit()
        return cur.lastrowid


def get_open_trades() -> list[dict]:
    """Открытые сделки журнала (status='open') — те, чей исход ещё отслеживаем."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, user_id, instrument, direction, entry_price, stop_loss,
                   take_profit, bar_time
            FROM trades WHERE status = 'open'
        """).fetchall()
    return [dict(row) for row in rows]


def get_user_trades(user_id: int, limit: int = 20) -> list[dict]:
    """Сделки одного пользователя: сначала открытые, затем закрытые (свежие выше)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, instrument, direction, entry_price, stop_loss, take_profit, status
            FROM trades WHERE user_id = ?
            ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    return [dict(row) for row in rows]


def update_trade_status(trade_id: int, status: str) -> None:
    """Меняет статус сделки (open → hit_tp | hit_sl | closed) и ставит дату закрытия."""
    closed_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE trades SET status = ?, closed_at = ? WHERE id = ?",
            (status, closed_at, trade_id),
        )
        conn.commit()


def close_trade(trade_id: int, user_id: int) -> bool:
    """Ручное закрытие сделки пользователем. Возвращает True, если что-то закрылось."""
    closed_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            UPDATE trades SET status = 'closed', closed_at = ?
            WHERE id = ? AND user_id = ? AND status = 'open'
        """, (closed_at, trade_id, user_id))
        conn.commit()
        return cur.rowcount > 0
