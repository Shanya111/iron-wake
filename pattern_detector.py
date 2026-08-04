"""Обнаружение паттернов ложного пробоя на H1 (стратегия №2).

Spring (пружина): цена пробивает поддержку вниз, но свеча закрывается обратно выше
уровня на повышенном объёме → ложный пробой → сигнал в ЛОНГ.
Upthrust — зеркало по сопротивлению (пробой вверх, закрытие ниже) → сигнал в ШОРТ.

Работаем по последней ЗАКРЫТОЙ свече H1 (df.iloc[-2]); df.iloc[-1] обычно ещё
формируется и для оценки «закрылась обратно» не годится.
"""

import pandas as pd

import config
import trading_week


def detect_spring(df: pd.DataFrame, levels: list[dict], trend: str,
                  settings: dict | None = None) -> dict | None:
    """Бычий Spring. Фильтр тренда: при нисходящем тренде ('down') не сигналим.
    settings — персональные пороги подписчика (None → общие из config)."""
    return _detect(df, levels, trend, side="long", settings=settings)


def detect_upthrust(df: pd.DataFrame, levels: list[dict], trend: str,
                    settings: dict | None = None) -> dict | None:
    """Медвежий Upthrust — зеркало Spring. При восходящем тренде ('up') не сигналим.
    settings — персональные пороги подписчика (None → общие из config)."""
    return _detect(df, levels, trend, side="short", settings=settings)


def _avg_volume(df: pd.DataFrame, end_pos: int) -> float:
    """Средний объём VOL_LOOKBACK свечей перед свечой с индексом end_pos."""
    start = max(0, end_pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:end_pos]
    return float(window.mean()) if len(window) else 0.0


def _atr(df: pd.DataFrame, pos: int, period: int) -> float:
    """ATR (средний истинный диапазон) на свече `pos` — мера волатильности.

    Истинный диапазон свечи — наибольшее из: её размах, расстояние от её максимума
    до вчерашнего закрытия и от минимума до него же (последние два ловят гэпы).
    ATR — среднее такого диапазона за `period` свечей.

    Нужен стопу: запас за экстремум свечи пробоя задаётся в долях ATR, а не в
    процентах от цены. По бэктесту типичный свип у биткоина 0.32% от цены, а у
    золота 0.10% — одним процентом им обоим не угодить, в долях ATR они втрое ближе.
    """
    start = max(0, pos - period + 1)
    window = df.iloc[start:pos + 1]
    prev_close = df["close"].shift(1).iloc[start:pos + 1]
    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - prev_close).abs(),
        (window["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.mean())


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


def _detect(df: pd.DataFrame, levels: list[dict], trend: str, side: str,
            settings: dict | None = None) -> dict | None:
    # Действующие пороги: личные значения подписчика поверх общих (None → общие).
    s = settings or {}
    vol_mult = s.get("VOL_MULT", config.get("VOL_MULT"))
    break_pct = s.get("BREAK_PCT", config.get("BREAK_PCT"))
    min_rr = s.get("MIN_RR", config.get("MIN_RR"))
    # Запас стопа за экстремум свечи пробоя — см. config.STOP_MODE. По умолчанию
    # он в долях ATR (подстраивается под волатильность), запасной вариант — процент
    # от цены. Настройки могут переопределить: STOP_ABS — готовый запас числом,
    # STOP_SPREAD — явный процент. Оба нужны бэктесту, чтобы гонять варианты стопа
    # через боевой детектор; явное переопределение всегда главнее режима из config.
    stop_spread = s.get("STOP_SPREAD", config.STOP_SPREAD)
    stop_abs = s.get("STOP_ABS")
    use_atr = stop_abs is None and "STOP_SPREAD" not in s and config.STOP_MODE == "atr"

    if len(df) < config.VOL_LOOKBACK + 3:
        return None
    # Фильтр направления по глобальному тренду.
    if side == "long" and trend == "down":
        return None
    if side == "short" and trend == "up":
        return None

    pos = len(df) - 2  # последняя закрытая свеча
    # Недельное окно: в пятницу и на выходных входов не даём вообще, и не даём,
    # если до закрытия недели осталось меньше MIN_HOURS_BEFORE_CLOSE — сделка не
    # успеет отработать, а через выходные мы ничего не переносим (trading_week).
    # WEEK_FILTER=False отключает проверку — нужно бэктесту, чтобы сравнить с «как было».
    if s.get("WEEK_FILTER", True):
        allowed, _ = trading_week.is_entry_allowed(df.index[pos])
        if not allowed:
            return None
    candle = df.iloc[pos]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])
    vol = float(candle["volume"])

    # Условие №3: аномальный объём на свече пробоя.
    avg_vol = _avg_volume(df, pos)
    if avg_vol <= 0 or vol < avg_vol * vol_mult:
        return None

    # ATR считаем только здесь, когда свеча уже прошла фильтр объёма — на каждом
    # баре подряд это была бы лишняя работа.
    atr = _atr(df, pos, config.STOP_ATR_PERIOD)
    # Запас стопа в долях ATR (если включён режим "atr"). Не на чем посчитать
    # (плоские свечи, короткая история) — молча падаем на процент.
    if use_atr and atr > 0:
        stop_abs = atr * config.STOP_ATR_MULT

    # Фильтр цены входа: риск (закрытие → экстремум свечи пробоя + запас) не должен
    # превышать MAX_RISK_ATR × ATR. Смысл не статистический, а механический: если
    # свеча пробоя аномально большая, вход по её закрытию оказывается далеко от
    # уровня — в конце размашистого бара. Пружина торгуется ОТ уровня, а не вдогонку,
    # и такие «догоняющие» входы в бэктесте суммарно теряют (−15.4R на 63 сделках,
    # минус на обеих половинах истории). Риск от уровня не зависит (вход — закрытие,
    # стоп — экстремум той же свечи), поэтому считаем один раз до перебора уровней.
    max_risk_atr = s.get("MAX_RISK_ATR", config.MAX_RISK_ATR)
    if atr > 0 and max_risk_atr:
        buffer = stop_abs if stop_abs is not None else (l if side == "long" else h) * stop_spread
        risk_now = (c - l + buffer) if side == "long" else (h - c + buffer)
        if risk_now > atr * max_risk_atr:
            return None

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
        if side == "long":
            entry = c
            stop = l - (stop_abs if stop_abs is not None else l * stop_spread)
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
            stop = h + (stop_abs if stop_abs is not None else h * stop_spread)
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


def evaluate_signal(signal: dict, df: pd.DataFrame, week_close: bool = False) -> str:
    """Исход открытого сигнала одной строкой (см. evaluate_signal_detailed)."""
    return evaluate_signal_detailed(signal, df, week_close)["status"]


def evaluate_signal_detailed(signal: dict, df: pd.DataFrame,
                             week_close: bool = False) -> dict:
    """Исход открытого сигнала по свечам, появившимся ПОСЛЕ свечи пробоя.

    Вход в сделку — это закрытие свечи пробоя (signal['bar_time']), поэтому смотрим
    только свечи строго после неё. Возвращает {status, exit_price, exit_time}, где
    status:
      • 'hit_tp'      — цена дошла до цели (плюс);
      • 'hit_sl'      — цена дошла до стопа (минус);
      • 'closed_week' — не дошла никуда до закрытия недели и погашена по рынку
                        (только при week_close=True; цена выхода — в exit_price);
      • 'expired'     — за SIGNAL_EXPIRE_HOURS не дошла никуда (исход неизвестен);
      • 'pending'     — пока рано, ждём дальше.

    week_close=True включает недельное окно: за пятничное закрытие сделку не
    тянем, гасим по последней свече перед ним (см. trading_week). Журнал сделок
    вызывает с week_close=False — там сделки закрывает сам пользователь.

    Внутри одной свечи порядок касаний неизвестен, поэтому при двусмысленности
    (свеча накрыла и стоп, и цель) считаем консервативно — сначала стоп.
    Если у сигнала нет якоря bar_time (старый сигнал до 2-й волны) — не трогаем
    его ('pending'): без точки отсчёта исход не определить честно.
    """
    def result(status, price=None, ts=None):
        return {"status": status, "exit_price": price, "exit_time": ts}

    bar_time = signal.get("bar_time")
    if not bar_time:
        return result("pending")
    after = df[df.index > pd.Timestamp(bar_time)]
    if after.empty:
        return result("pending")

    deadline = trading_week.week_close_after(bar_time) if week_close else None
    if deadline is not None:
        after = after[after.index <= deadline]  # за закрытие недели не заглядываем
        if after.empty:
            return result("pending")

    stop, tp = signal["stop_loss"], signal["take_profit"]
    long = signal["direction"] == "long"
    for ts, c in after.iterrows():
        hi, lo = float(c["high"]), float(c["low"])
        if long:
            if lo <= stop:
                return result("hit_sl", stop, ts)
            if hi >= tp:
                return result("hit_tp", tp, ts)
        else:
            if hi >= stop:
                return result("hit_sl", stop, ts)
            if lo <= tp:
                return result("hit_tp", tp, ts)

    # С недельным окном горизонт сигнала задаёт закрытие недели: `after` обрезан
    # дедлайном, а от понедельника до пятничного закрытия максимум 117ч — меньше
    # SIGNAL_EXPIRE_HOURS (120ч). То есть сигнал понедельника доживает до пятницы
    # и гасится по рынку, а не истекает на середине недели. 'expired' остаётся
    # для вызовов без окна (журнал сделок) и на случай дыр в данных.
    age_hours = (after.index[-1] - pd.Timestamp(bar_time)).total_seconds() / 3600
    if age_hours >= config.SIGNAL_EXPIRE_HOURS:
        return result("expired")
    if deadline is not None and df.index.max() >= deadline:
        return result("closed_week", float(after["close"].iloc[-1]), after.index[-1])
    return result("pending")
