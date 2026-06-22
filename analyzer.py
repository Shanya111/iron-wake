"""Контекстный анализ (стратегия №1, чистый Python): тренд, уровни, зоны ликвидности.

Все функции принимают DataFrame со столбцами open/high/low/close/volume (как отдаёт
data_fetcher.get_candles) и НЕ делают сетевых запросов — только расчёты. Логика
детерминированная и тестируемая (см. tests/).
"""

import pandas as pd

import config


def get_trend(df: pd.DataFrame) -> str:
    """Глобальный тренд по EMA: 'up' / 'down' / 'sideways'.

    Сравниваем последнюю цену с EMA(EMA_PERIOD) и смотрим наклон EMA. Если цена выше
    EMA более чем на TREND_BAND и EMA растёт → 'up'; зеркально → 'down'; иначе
    (плоско или разнонаправленно) → 'sideways'.
    """
    close = df["close"]
    if len(close) < config.EMA_PERIOD:
        return "sideways"
    ema = close.ewm(span=config.EMA_PERIOD, adjust=False).mean()
    last_close = float(close.iloc[-1])
    last_ema = float(ema.iloc[-1])
    prev_ema = float(ema.iloc[-min(len(ema), config.EMA_PERIOD)])
    diff = (last_close - last_ema) / last_ema if last_ema else 0.0
    rising = last_ema >= prev_ema
    if diff > config.TREND_BAND and rising:
        return "up"
    if diff < -config.TREND_BAND and not rising:
        return "down"
    return "sideways"


def find_levels(df: pd.DataFrame, window: int, timeframe: str) -> list[dict]:
    """Локальные пики (resistance) и впадины (support) — фракталы шириной `window`.

    Пик: High свечи строго больше High всех `window` соседей слева и справа.
    Впадина: симметрично по Low. Сила пока 'weak' — её проставит prioritize_levels.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    levels: list[dict] = []
    for i in range(window, n - window):
        left, right = slice(i - window, i), slice(i + 1, i + 1 + window)
        if highs[i] > highs[left].max() and highs[i] > highs[right].max():
            levels.append({
                "price": float(highs[i]), "type": "resistance",
                "strength": "weak", "is_liquidity": 0, "timeframe": timeframe,
            })
        if lows[i] < lows[left].min() and lows[i] < lows[right].min():
            levels.append({
                "price": float(lows[i]), "type": "support",
                "strength": "weak", "is_liquidity": 0, "timeframe": timeframe,
            })
    return levels


def find_liquidity_zones(df: pd.DataFrame, mult: float | None = None) -> list[dict]:
    """Зоны скопления объёма (Smart Money): свечи, где volume > среднего × mult.

    Возвращает ценовые уровни (середина свечи) с объёмом — это зоны ликвидности,
    куда рынок может тянуться. mult по умолчанию из настроек (/settings).
    """
    if mult is None:
        mult = config.get("LIQUIDITY_MULT")
    vol = df["volume"]
    if vol.empty or float(vol.mean()) == 0:
        return []
    avg = float(vol.mean())
    zones = []
    for i in range(len(df)):
        if float(vol.iloc[i]) > avg * mult:
            mid = float((df["high"].iloc[i] + df["low"].iloc[i]) / 2)
            zones.append({"price": mid, "volume": float(vol.iloc[i])})
    return zones


def prioritize_levels(
    global_levels: list[dict], local_levels: list[dict], tol: float | None = None
) -> list[dict]:
    """Совмещает глобальные (D1) и локальные (H1) уровни и проставляет силу.

    Локальный уровень H1, совпавший с глобальным D1 того же типа в пределах tol
    (0.1%), помечается 'strong'; иначе 'weak'. Сами глобальные уровни добавляются
    как контекст и всегда 'strong'. Возвращает единый список уровней.
    """
    if tol is None:
        tol = config.LEVEL_MATCH_TOL
    result: list[dict] = []
    for lvl in local_levels:
        strong = any(
            g["type"] == lvl["type"]
            and abs(g["price"] - lvl["price"]) <= lvl["price"] * tol
            for g in global_levels
        )
        out = dict(lvl)
        out["strength"] = "strong" if strong else "weak"
        result.append(out)
    for g in global_levels:
        out = dict(g)
        out["strength"] = "strong"
        result.append(out)
    return result
