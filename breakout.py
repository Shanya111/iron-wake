"""Пробой сильного уровня (стратегия №5) — В СЛЕЖКЕ, сигналы никому не шлются.

Задание владельца 17.09.2026: «свеча закрылась выше сильного уровня — сигнал на
покупку с расчётом риск-прибыль». Замер (`june_break.py`, 13 монет + GOLD + BRENT,
27 мес.) показал 5 пунктов критерия из 6, и владелец решил сначала ПОСМОТРЕТЬ на
живых данных: бот считает сигналы и ведёт их исходы, но не рассылает никому
(config.BREAKOUT_SIGNALS = False). Сводка — команда /breakout.

ПРАВИЛА (ровно те, что мерились):
  • решение — по последней ЗАКРЫТОЙ часовой свече;
  • уровень обязан быть СИЛЬНЫМ (часовой совпал с дневным, либо сам дневной) — на
    слабых уровнях эффекта нет вовсе: брутто -0.006 на 64 тыс. сделок против +0.070;
  • пробой СВЕЖИЙ: свеча закрылась за уровнем, а предыдущая закрывалась по другую
    его сторону. Без этого требования «пробоем» становится любая свеча выше уровня —
    грабли momentum.py (21.08.2026), пойманные по среднему риску 6.59% от цены;
  • из пробитых берётся САМЫЙ ДАЛЬНИЙ — для лонга верхнее сопротивление, для шорта
    нижняя поддержка (тот же принцип, что у ложного пробоя);
  • вход — закрытие свечи, стоп — за её фитилём плюс BREAKOUT_STOP_ATR × ATR;
  • цель — ближайший встречный уровень НЕ БЛИЖЕ BREAKOUT_MIN_TP_R рисков, нет
    такого — ровно BREAKOUT_MIN_TP_R риска.

ПОЧЕМУ ЦЕЛЬ ДАЛЬНЯЯ, А НЕ КАК У ЛОЖНОГО ПРОБОЯ. Пробойная сделка живёт редким
длинным хвостом, и близкая цель его отрезает. Замер по целям (брутто, горизонт 120 ч):
1 риск +0.086, 2 риска +0.135, 3 риска +0.107 — оптимум ВНУТРИ сетки, а не на краю.
Это же предсказывал `trend_geom.py` 16.09.2026 на стратегии №4.

ЧЕГО ЗАМЕР НЕ ПОКАЗАЛ И ЧТО ИМЕННО ПРОВЕРЯЕТ СЛЕЖКА: на свежих данных (после
01.02.2026) пробой в МИНУСЕ (-0.054 при +0.135 на всей истории), а зеркальный ему
возврат — наоборот в плюсе. Так выглядит смена режима рынка, а не преимущество.
Поэтому в базу пишутся ещё и признаки момента (объём свечи к среднему, направление
6-часового тренда): через месяц по ним считается, что дали бы фильтры, без нового
прогона истории.

Фильтров объёма и направления в правилах НЕТ намеренно. Объём ×1.5 давал брутто
выше (+0.120), но втрое меньше сделок и ХУЖЕ экзамен; направление не меняло ничего
(+0.076 против +0.070 при ошибке 0.024). Оба признака пишутся в базу — этого хватит.

Функции чистые: ни сети, ни базы. Ведёт стратегию scheduler.monitor_breakout.
"""

import pandas as pd

import config
import pattern_detector


def _strong(levels: list[dict], kind: str) -> list[dict]:
    """Сильные уровни нужного типа. Сила — из analyzer.prioritize_levels."""
    return [lvl for lvl in levels
            if lvl["type"] == kind and lvl.get("strength") == "strong"]


def _crossed(side: str, close_now: float, close_prev: float, price: float) -> bool:
    """Свежий пробой: закрылись за уровнем, а предыдущая свеча была по другую сторону."""
    if side == "long":
        return close_now > price and close_prev <= price
    return close_now < price and close_prev >= price


def _target(levels: list[dict], side: str, c: float, min_gap: float) -> float | None:
    """Ближайший встречный уровень не ближе min_gap от закрытия (в цене)."""
    if side == "long":
        ahead = [x["price"] for x in levels
                 if x["type"] == "resistance" and x["price"] - c >= min_gap]
        return min(ahead) if ahead else None
    ahead = [x["price"] for x in levels
             if x["type"] == "support" and c - x["price"] >= min_gap]
    return max(ahead) if ahead else None


def detect(df: pd.DataFrame, levels: list[dict], trend: str = "sideways") -> list[dict]:
    """Сигналы пробоя по последней закрытой свече — список из 0, 1 или 2 штук.

    Двух сразу не бывает у нормального рынка (свеча не может закрыться и выше
    сопротивления, и ниже поддержки), но список возвращается ради симметрии с
    detect_spring/detect_upthrust и чтобы вызывающему не гадать.

    `trend` не фильтрует — он пишется в сигнал как признак момента (см. docstring
    модуля): замер разницы между «с фильтром» и «без» не нашёл.
    """
    if len(df) < config.VOL_LOOKBACK + 3:
        return []
    pos = len(df) - 2                       # последняя закрытая свеча
    candle, prev = df.iloc[pos], df.iloc[pos - 1]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])
    c_prev = float(prev["close"])
    atr = pattern_detector._atr(df, pos)
    if atr <= 0:                            # плоские свечи — стоп ставить не от чего
        return []

    start = max(0, pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:pos]
    avg_vol = float(window.mean()) if len(window) else 0.0
    vol_ratio = float(candle["volume"]) / avg_vol if avg_vol > 0 else 0.0

    out = []
    for side, kind in (("long", "resistance"), ("short", "support")):
        broken = [lvl for lvl in _strong(levels, kind)
                  if _crossed(side, c, c_prev, lvl["price"])]
        if not broken:
            continue
        # Самый дальний из пробитых: для лонга верхнее сопротивление, для шорта нижняя
        # поддержка. Свеча прошла их все, и берём тот, чей пробой был самым сильным.
        lvl = (max(broken, key=lambda x: x["price"]) if side == "long"
               else min(broken, key=lambda x: x["price"]))
        stop = (l - config.BREAKOUT_STOP_ATR * atr if side == "long"
                else h + config.BREAKOUT_STOP_ATR * atr)
        risk = abs(c - stop)
        if risk <= 0:
            continue
        gap = config.BREAKOUT_MIN_TP_R * risk
        target = _target(levels, side, c, gap)
        if target is None:
            target = c + gap if side == "long" else c - gap
        out.append({
            "direction": side,
            "level_price": lvl["price"],
            "entry_price": c,
            "stop_loss": stop,
            "take_profit": target,
            "bar_time": str(df.index[pos]),
            "vol_ratio": vol_ratio,
            "trend": trend,
        })
    return out


def outcome(signal: dict, df: pd.DataFrame) -> str:
    """Исход открытого сигнала: 'hit_tp' / 'hit_sl' / 'expired' / 'pending'.

    Считает общий помощник pattern_detector.evaluate_signal — тот же, что ведёт
    ложный пробой и журнал сделок: правило «при двусмысленности внутри свечи сначала
    стоп» обязано быть одним на весь проект. Отличается только срок жизни: у пробоя
    горизонт 120 часов (в замере он лучше 48 на всех вариантах цели).
    """
    return pattern_detector.evaluate_signal(
        signal, df, expire_hours=config.BREAKOUT_EXPIRE_HOURS)


def result_r(signal: dict, status: str) -> float | None:
    """Итог сделки в рисках: цель даёт фактический R:R, стоп — ровно −1."""
    risk = abs(signal["entry_price"] - signal["stop_loss"])
    if risk <= 0:
        return None
    if status == "hit_tp":
        return abs(signal["take_profit"] - signal["entry_price"]) / risk
    if status == "hit_sl":
        return -1.0
    return None
