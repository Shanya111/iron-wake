"""Недельное окно торговли: вход только пн–чт, сделка закрывается до выходных.

Зачем. По форексу, золоту и нефти рынок на выходных закрыт физически — в
понедельник цена открывается гэпом и может перепрыгнуть стоп: заявка исполнится
не там, где стоит, а хуже, иногда сильно.

Отсюда два правила (оба настраиваются в config):
  • новых входов не даём в дни NO_ENTRY_WEEKDAYS (суббота, воскресенье)
    и не позже, чем за MIN_HOURS_BEFORE_CLOSE часов до закрытия недели;
  • открытый сигнал, доживший до закрытия недели, закрывается принудительно по
    рынку — на выходные не переносим ничего.

**КРИПТА ПОД ОКНО НЕ ПОПАДАЕТ** (с 7 августа 2026, решение владельца). У неё
рынок работает 24/7, гэпа выходного дня нет, и закрывать позицию к пятнице
незачем. Признак — `instruments.trades_round_clock(code)`; общий выключатель —
`config.CRYPTO_ROUND_CLOCK`. Прежний довод против выходных был не про гэп, а про
тонкую ликвидность и рваные движения; владелец решил, что это его риск.
Вызывающий код обязан передавать round_clock — функции здесь сами реестр не
читают, чтобы остаться чистыми и тестируемыми.

Всё время — UTC (как и свечи движка). Закрытие недели — пятница WEEK_CLOSE_HOUR:00,
что соответствует закрытию форекса и фьючерсов (17:00 по Нью-Йорку).
"""

from datetime import timedelta

import pandas as pd

import config

FRIDAY = 4  # индекс пятницы: понедельник = 0


def to_utc(ts) -> pd.Timestamp:
    """Любое время (строка, datetime, Timestamp) → Timestamp в UTC.
    Время без зоны считаем уже UTC — свечи движка приходят именно такими."""
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def week_close_after(ts) -> pd.Timestamp:
    """Ближайшее закрытие недели строго после `ts` — пятница WEEK_CLOSE_HOUR:00 UTC.

    Если `ts` уже позже пятничного закрытия этой недели (вечер пятницы, выходные),
    вернётся закрытие следующей недели.
    """
    ts = to_utc(ts)
    close = (ts.normalize() + timedelta(days=FRIDAY - ts.weekday())
             + timedelta(hours=config.WEEK_CLOSE_HOUR))
    if close <= ts:
        close += timedelta(days=7)
    return close


def hours_until_close(ts) -> float:
    """Сколько часов от `ts` до ближайшего закрытия недели."""
    return (week_close_after(ts) - to_utc(ts)).total_seconds() / 3600


def is_entry_allowed(ts, round_clock: bool = False) -> tuple[bool, str]:
    """Можно ли открывать сделку в момент `ts`. Возвращает (можно, причина отказа).

    Причина — короткий код для логов: 'weekday' (пятница/выходные) или 'tail'
    (до закрытия недели осталось меньше MIN_HOURS_BEFORE_CLOSE часов — сделка не
    успеет отработать).

    round_clock=True — инструмент торгуется без выходных (крипта, см.
    instruments.trades_round_clock): окно к нему не применяется вовсе, вход
    разрешён всегда. Гэпа выходного дня у него нет — блокировать нечего.
    """
    if round_clock:
        return True, ""
    ts = to_utc(ts)
    if ts.weekday() in config.NO_ENTRY_WEEKDAYS:
        return False, "weekday"
    if hours_until_close(ts) < config.MIN_HOURS_BEFORE_CLOSE:
        return False, "tail"
    return True, ""


def force_close_bar(df: pd.DataFrame, bar_time) -> tuple[pd.Timestamp, float] | None:
    """Где и по какой цене закрыть сделку, открытую на свече `bar_time`, если она
    дожила до закрытия недели.

    Возвращает (время последней свечи до закрытия, её close) — по этой цене и
    считаем результат. None, если закрытие недели ещё не наступило или до него в
    данных нет ни одной свечи после входа.
    """
    deadline = week_close_after(bar_time)
    after = df[(df.index > to_utc(bar_time)) & (df.index <= deadline)]
    if after.empty:
        return None
    # Свечей за дедлайном ещё нет → неделя не закрылась, закрывать рано.
    if df.index.max() < deadline:
        return None
    return after.index[-1], float(after["close"].iloc[-1])


def realized_r(direction: str, entry: float, stop: float, exit_price: float) -> float:
    """Результат закрытой по рынку сделки в R (риск на сделку = 1R).

    Нужен для /stats: у принудительно закрытого сигнала нет ни цели, ни стопа —
    итог считается по фактической цене выхода. Лонг: сколько прошли вверх от
    входа, делённое на риск. Шорт — зеркально.
    """
    risk = abs(entry - stop)
    if not risk:
        return 0.0
    move = (exit_price - entry) if direction == "long" else (entry - exit_price)
    return move / risk
