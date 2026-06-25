"""Обнаружение паттернов ложного пробоя на H1 (стратегия №2).

Spring (пружина): цена пробивает поддержку вниз, но свеча закрывается обратно выше
уровня на повышенном объёме → ложный пробой → сигнал в ЛОНГ.
Upthrust — зеркало по сопротивлению (пробой вверх, закрытие ниже) → сигнал в ШОРТ.

Работаем по последней ЗАКРЫТОЙ свече H1 (df.iloc[-2]); df.iloc[-1] обычно ещё
формируется и для оценки «закрылась обратно» не годится.
"""

import pandas as pd

import config


def detect_spring(df: pd.DataFrame, levels: list[dict], trend: str) -> dict | None:
    """Бычий Spring. Фильтр тренда: при нисходящем тренде ('down') не сигналим."""
    return _detect(df, levels, trend, side="long")


def detect_upthrust(df: pd.DataFrame, levels: list[dict], trend: str) -> dict | None:
    """Медвежий Upthrust — зеркало Spring. При восходящем тренде ('up') не сигналим."""
    return _detect(df, levels, trend, side="short")


def _avg_volume(df: pd.DataFrame, end_pos: int) -> float:
    """Средний объём VOL_LOOKBACK свечей перед свечой с индексом end_pos."""
    start = max(0, end_pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:end_pos]
    return float(window.mean()) if len(window) else 0.0


def _nearest(levels: list[dict], level_type: str, ref_price: float, above: bool):
    """Ближайший уровень нужного типа выше (above=True) или ниже ref_price."""
    prices = [
        l["price"] for l in levels
        if l["type"] == level_type
        and (l["price"] > ref_price if above else l["price"] < ref_price)
    ]
    if not prices:
        return None
    return min(prices) if above else max(prices)


def _detect(df: pd.DataFrame, levels: list[dict], trend: str, side: str) -> dict | None:
    if len(df) < config.VOL_LOOKBACK + 3:
        return None
    # Фильтр направления по глобальному тренду.
    if side == "long" and trend == "down":
        return None
    if side == "short" and trend == "up":
        return None

    pos = len(df) - 2  # последняя закрытая свеча
    candle = df.iloc[pos]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])
    vol = float(candle["volume"])

    # Условие №3: аномальный объём на свече пробоя.
    avg_vol = _avg_volume(df, pos)
    if avg_vol <= 0 or vol < avg_vol * config.get("VOL_MULT"):
        return None

    break_pct = config.get("BREAK_PCT")
    level_type = "support" if side == "long" else "resistance"
    relevant = [lvl for lvl in levels if lvl["type"] == level_type]

    for lvl in relevant:
        price = lvl["price"]
        if side == "long":
            broke = l < price * (1 - break_pct)   # пробили поддержку вниз
            returned = c > price                   # закрылись обратно выше
        else:
            broke = h > price * (1 + break_pct)    # пробили сопротивление вверх
            returned = c < price                   # закрылись обратно ниже
        if not (broke and returned):
            continue

        priority = "high" if lvl.get("strength") == "strong" else "normal"
        # Минимальный R:R (прибыль/риск). Цель — ближайший противоположный уровень,
        # но сигнал берём, только если он даёт хотя бы MIN_RR; ближе — сделка
        # невыгодна, пропускаем. Нет уровня впереди → ставим цель ровно на MIN_RR×риск.
        min_rr = config.get("MIN_RR")
        if side == "long":
            entry = c
            stop = l * (1 - config.STOP_SPREAD)
            risk = entry - stop
            target = _nearest(levels, "resistance", entry, above=True)
            if target is None:
                tp = entry + risk * min_rr
            elif (target - entry) >= risk * min_rr:
                tp = target
            else:
                continue
        else:
            entry = c
            stop = h * (1 + config.STOP_SPREAD)
            risk = stop - entry
            target = _nearest(levels, "support", entry, above=False)
            if target is None:
                tp = entry - risk * min_rr
            elif (entry - target) >= risk * min_rr:
                tp = target
            else:
                continue

        return {
            "pattern": "spring" if side == "long" else "upthrust",
            "direction": "long" if side == "long" else "short",
            "level_price": price,
            "priority": priority,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": tp,
            "bar_time": str(df.index[pos]),
        }
    return None


def evaluate_signal(signal: dict, df: pd.DataFrame) -> str:
    """Исход открытого сигнала по свечам, появившимся ПОСЛЕ свечи пробоя.

    Вход в сделку — это закрытие свечи пробоя (signal['bar_time']), поэтому смотрим
    только свечи строго после неё. Возвращает:
      • 'hit_tp'  — цена дошла до цели (плюс);
      • 'hit_sl'  — цена дошла до стопа (минус);
      • 'expired' — за SIGNAL_EXPIRE_HOURS не дошла никуда (исход неизвестен);
      • 'pending' — пока рано, ждём дальше.

    Внутри одной свечи порядок касаний неизвестен, поэтому при двусмысленности
    (свеча накрыла и стоп, и цель) считаем консервативно — сначала стоп.
    Если у сигнала нет якоря bar_time (старый сигнал до 2-й волны) — не трогаем
    его ('pending'): без точки отсчёта исход не определить честно.
    """
    bar_time = signal.get("bar_time")
    if not bar_time:
        return "pending"
    after = df[df.index > pd.Timestamp(bar_time)]
    if after.empty:
        return "pending"

    stop, tp = signal["stop_loss"], signal["take_profit"]
    long = signal["direction"] == "long"
    for _, c in after.iterrows():
        hi, lo = float(c["high"]), float(c["low"])
        if long:
            if lo <= stop:
                return "hit_sl"
            if hi >= tp:
                return "hit_tp"
        else:
            if hi >= stop:
                return "hit_sl"
            if lo <= tp:
                return "hit_tp"

    age_hours = (after.index[-1] - pd.Timestamp(bar_time)).total_seconds() / 3600
    if age_hours >= config.SIGNAL_EXPIRE_HOURS:
        return "expired"
    return "pending"
