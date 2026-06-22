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
        if side == "long":
            entry = c
            stop = l * (1 - config.STOP_SPREAD)
            tp = _nearest(levels, "resistance", entry, above=True)
            if tp is None:
                tp = entry + (entry - stop) * 2    # запасной тейк 2R
        else:
            entry = c
            stop = h * (1 + config.STOP_SPREAD)
            tp = _nearest(levels, "support", entry, above=False)
            if tp is None:
                tp = entry - (stop - entry) * 2

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
