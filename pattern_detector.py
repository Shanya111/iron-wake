"""Обнаружение паттернов ложного пробоя на H1 (стратегия №2).

Spring (пружина): цена пробивает поддержку вниз, но свеча закрывается обратно выше
уровня на повышенном объёме → ложный пробой → сигнал в ЛОНГ.
Upthrust — зеркало по сопротивлению (пробой вверх, закрытие ниже) → сигнал в ШОРТ.

Работаем по последней ЗАКРЫТОЙ свече H1 (df.iloc[-2]); df.iloc[-1] обычно ещё
формируется и для оценки «закрылась обратно» не годится.

── Вход ЛИМИТНОЙ заявкой, а не по рынку ─────────────────────────────────────────
Раньше сигнал говорил «вход = закрытие свечи пробоя». Это рыночная заявка, то есть
роль ТЕЙКЕРА: на BingX-фьючерсах она стоит 0.05% против 0.02% у мейкера, плюс
половина спреда. При среднем риске сделки ~0.5% от цены разница между тейкером и
мейкером — это десятые доли R на каждой сделке, то есть примерно вся разница между
плюсом и минусом на счёте.

Поэтому детектор отдаёт ЦЕНУ ЗАЯВКИ: точку между закрытием свечи и пробитым уровнем
(доля пути задаётся config.ENTRY_PULLBACK; 1.0 = ровно на уровне). Заявка ждёт
config.ENTRY_WAIT_BARS часов; не исполнилась — сделки не было вовсе.

Цена этого решения — НЕИСПОЛНЕННЫЕ ЗАЯВКИ, и они не случайные: если цена сразу пошла
к цели и не откатилась, заявка не сработала. То есть теряются в первую очередь
выигрышные сделки (адверс-селекция). Поэтому ENTRY_PULLBACK — не «просто поставь
лимитку», а измеренный компромисс; таблица замера — в config.ENTRY_PULLBACK.
"""

import pandas as pd

import config


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


def limit_price(side: str, close: float, level: float, stop: float,
                frac: float | None = None) -> float:
    """Цена лимитной заявки: доля пути от закрытия свечи пробоя обратно к уровню.

    frac=0 — заявка ровно по закрытию (прежнее поведение, фактически вход по рынку);
    frac=0.5 — на полпути к уровню; frac=1.0 — ровно на пробитом уровне.
    Чем ближе к уровню, тем лучше вход при том же стопе и цели (больше R:R), но тем
    реже заявка вообще исполняется.

    Заявка никогда не заходит за стоп: там от сделки не осталось бы риска, а значит
    и смысла. Такое возможно только при испорченных данных, но проверка дешёвая.
    """
    frac = config.ENTRY_PULLBACK if frac is None else frac
    if not frac:
        return close
    price = (close - frac * (close - level) if side == "long"
             else close + frac * (level - close))
    if (price <= stop) if side == "long" else (price >= stop):
        return close
    return price


def _detect(df: pd.DataFrame, levels: list[dict], trend: str, side: str,
            settings: dict | None = None) -> dict | None:
    # Действующие пороги: личные значения подписчика поверх общих (None → общие).
    s = settings or {}
    vol_mult = s.get("VOL_MULT", config.get("VOL_MULT"))
    break_pct = s.get("BREAK_PCT", config.get("BREAK_PCT"))
    min_rr = s.get("MIN_RR", config.get("MIN_RR"))

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
    if avg_vol <= 0 or vol < avg_vol * vol_mult:
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
        #
        # ВАЖНО: отбор (и MIN_RR, и запасная цель) считается ОТ ЗАКРЫТИЯ свечи, а не
        # от цены лимитной заявки. Так набор сигналов не зависит от ENTRY_PULLBACK —
        # заявка меняет только цену входа, а какие сетапы вообще берём, решает та же
        # логика, что и раньше. Иначе замер «что даёт лимитный вход» смешал бы эффект
        # входа с эффектом отбора, и сравнивать было бы нечего.
        if side == "long":
            stop = l * (1 - config.STOP_SPREAD)
            risk = c - stop
            target = _nearest(levels, "resistance", c, above=True)
            if target is None:
                tp = c + risk * min_rr
            elif (target - c) >= risk * min_rr:
                tp = target
            else:
                continue
        else:
            stop = h * (1 + config.STOP_SPREAD)
            risk = stop - c
            target = _nearest(levels, "support", c, above=False)
            if target is None:
                tp = c - risk * min_rr
            elif (c - target) >= risk * min_rr:
                tp = target
            else:
                continue

        entry = limit_price(side, c, price, stop, s.get("ENTRY_PULLBACK"))
        return {
            "pattern": "spring" if side == "long" else "upthrust",
            "direction": "long" if side == "long" else "short",
            "level_price": price,
            "priority": priority,
            # entry_price — цена ЛИМИТНОЙ ЗАЯВКИ (её и ставит пользователь),
            # signal_price — закрытие свечи пробоя, от которого считался отбор.
            "entry_price": entry,
            "signal_price": c,
            "stop_loss": stop,
            "take_profit": tp,
            "bar_time": str(df.index[pos]),
        }
    return None


def evaluate_fill(signal: dict, df: pd.DataFrame, wait_bars: int | None = None,
                  optimistic: bool = False) -> dict:
    """Исполнилась ли лимитная заявка. {'status', 'fill_time', 'stopped_at_fill'}.

    status:
      • 'waiting_fill'     — время ещё есть, цена до заявки не дошла, ждём;
      • 'filled'           — заявка исполнилась (в fill_time — момент исполнения);
      • 'expired_unfilled' — сделки не будет: либо цена ушла к цели без нас, либо
                             за wait_bars часов до заявки так и не дошла.

    Порядок событий внутри часовой свечи неизвестен, поэтому есть две границы:
      • optimistic=False (по умолчанию) — если свеча накрыла и нашу цену, и цель,
        считаем, что цель была раньше, и заявка НЕ исполнилась. Это осторожная
        сторона: она не даёт приписать себе сделку, которой могло не быть;
      • optimistic=True — считаем, что заявка успела исполниться.

    stopped_at_fill=True означает, что та же свеча дошла и до стопа. Здесь порядок
    как раз однозначен: стоп лежит ДАЛЬШЕ нашей цены, значит сначала вход, потом
    стоп — сделка открылась и тут же закрылась в минус.
    """
    wait_bars = config.ENTRY_WAIT_BARS if wait_bars is None else wait_bars
    bar_time = signal.get("bar_time")
    if not bar_time:
        return {"status": "waiting_fill", "fill_time": None, "stopped_at_fill": False}

    limit = signal["entry_price"]
    stop, tp = signal["stop_loss"], signal["take_profit"]
    long = signal["direction"] == "long"
    # Свеча пробоя старше всего окна свечей — так бывает, если бот лежал дольше, чем
    # H1_LIMIT часов. Тогда первые свечи окна это НЕ те, что шли за пробоем, и судить
    # по ним об исполнении нельзя. Заявка к этому моменту всё равно давно протухла:
    # снимаем её, а не гадаем.
    if len(df) and pd.Timestamp(bar_time) < df.index[0]:
        return {"status": "expired_unfilled", "fill_time": None,
                "stopped_at_fill": False}
    after = df[df.index > pd.Timestamp(bar_time)]

    for n, (ts, c) in enumerate(after.iloc[:wait_bars].iterrows(), start=1):
        hi, lo = float(c["high"]), float(c["low"])
        hit_limit = lo <= limit if long else hi >= limit
        hit_tp = hi >= tp if long else lo <= tp
        hit_stop = lo <= stop if long else hi >= stop
        if hit_tp and not (hit_limit and optimistic):
            return {"status": "expired_unfilled", "fill_time": None,
                    "stopped_at_fill": False}
        if hit_limit:
            return {"status": "filled", "fill_time": str(ts),
                    "stopped_at_fill": bool(hit_stop)}
    if len(after) >= wait_bars:
        return {"status": "expired_unfilled", "fill_time": None,
                "stopped_at_fill": False}
    return {"status": "waiting_fill", "fill_time": None, "stopped_at_fill": False}


def evaluate_signal(signal: dict, df: pd.DataFrame) -> str:
    """Исход ОТКРЫТОЙ сделки по свечам, появившимся ПОСЛЕ входа.

    Точка отсчёта — момент входа: `fill_time` (когда исполнилась лимитная заявка),
    а если его нет — `bar_time` (свеча пробоя). Второй случай — это старые сигналы
    рыночного входа и журнал сделок, где вход считается моментом записи.

    Возвращает:
      • 'hit_tp'  — цена дошла до цели (плюс);
      • 'hit_sl'  — цена дошла до стопа (минус);
      • 'expired' — за SIGNAL_EXPIRE_HOURS не дошла никуда (исход неизвестен);
      • 'pending' — пока рано, ждём дальше.

    Внутри одной свечи порядок касаний неизвестен, поэтому при двусмысленности
    (свеча накрыла и стоп, и цель) считаем консервативно — сначала стоп.
    Если якоря нет вовсе (совсем старый сигнал) — не трогаем его ('pending'):
    без точки отсчёта исход не определить честно.
    """
    anchor = signal.get("fill_time") or signal.get("bar_time")
    if not anchor:
        return "pending"
    after = df[df.index > pd.Timestamp(anchor)]
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

    age_hours = (after.index[-1] - pd.Timestamp(anchor)).total_seconds() / 3600
    if age_hours >= config.SIGNAL_EXPIRE_HOURS:
        return "expired"
    return "pending"
