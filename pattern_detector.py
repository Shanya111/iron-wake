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
from instruments import is_fx


def detect_spring(df: pd.DataFrame, levels: list[dict], trend: str,
                  settings: dict | None = None, code: str | None = None) -> dict | None:
    """Бычий Spring. Фильтр тренда: при нисходящем тренде ('down') не сигналим.
    settings — персональные пороги подписчика (None → общие из config).
    code — код инструмента: у валютных пар свои пороги (см. _detect)."""
    return _detect(df, levels, trend, side="long", settings=settings, code=code)


def detect_upthrust(df: pd.DataFrame, levels: list[dict], trend: str,
                    settings: dict | None = None, code: str | None = None) -> dict | None:
    """Медвежий Upthrust — зеркало Spring. При восходящем тренде ('up') не сигналим.
    settings — персональные пороги подписчика (None → общие из config).
    code — код инструмента: у валютных пар свои пороги (см. _detect)."""
    return _detect(df, levels, trend, side="short", settings=settings, code=code)


def _avg_volume(df: pd.DataFrame, end_pos: int) -> float:
    """Средний объём VOL_LOOKBACK свечей перед свечой с индексом end_pos."""
    start = max(0, end_pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:end_pos]
    return float(window.mean()) if len(window) else 0.0


def _atr(df: pd.DataFrame, pos: int, period: int | None = None) -> float:
    """ATR (средний истинный диапазон) на свече `pos` — мера волатильности.

    Истинный диапазон свечи — наибольшее из трёх: её собственный размах, расстояние
    от её максимума до предыдущего закрытия и от минимума до него же (последние два
    ловят гэпы). ATR — среднее такого диапазона за `period` свечей.

    Зачем он вообще: пороги вроде «вход не дальше 0.1% от уровня» у биткоина и у
    золота означают совершенно разное, потому что у них разная волатильность. В долях
    ATR один и тот же порог значит одно и то же на любом инструменте.
    """
    period = config.ATR_PERIOD if period is None else period
    start = max(0, pos - period + 1)
    window = df.iloc[start:pos + 1]
    prev_close = df["close"].shift(1).iloc[start:pos + 1]
    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - prev_close).abs(),
        (window["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.mean())


def _stop_buffer(extreme: float, atr: float, fx: bool) -> float:
    """Запас стопа за экстремум свечи пробоя, в абсолютных единицах цены.

    У крипты это процент от цены (config.STOP_SPREAD), у валютных пар — доля ATR
    (config.FX_STOP_ATR): 0.1% цены на форексе даёт 0.93-1.49 ATR, то есть стоп
    выходил бы шире собственной волатильности инструмента, а риск сделки всегда
    превышал бы фильтры строгости.

    Функция общая для _detect и explain намеренно: разъедутся — и /analyze начнёт
    показывать не тот риск, по которому движок принимает решение."""
    if fx:
        return atr * config.FX_STOP_ATR
    return extreme * config.STOP_SPREAD


def _break_depth(price: float, atr: float, fx: bool) -> float:
    """Требуемая глубина прокола уровня, в абсолютных единицах цены.

    У крипты — процент от уровня (config.BREAK_PCT), у валютных пар — доля ATR
    (config.FX_BREAK_ATR). В процентах цены форексной свече пришлось бы проколоть
    уровень на 0.42-0.68 СОБСТВЕННОГО размаха и в тот же час закрыться обратно;
    отсюда 12 сигналов за год на четырёх парах против 207 с порогом в ATR.

    Общая для _detect и explain по той же причине, что и _stop_buffer."""
    if fx:
        return atr * config.FX_BREAK_ATR
    return price * config.BREAK_PCT


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
            settings: dict | None = None, code: str | None = None) -> dict | None:
    # Пороги отбора (объём, глубина пробоя, R:R) — обычные константы: с 20 августа
    # 2026 пользователь их не крутит. Из settings приходят только два ATR-фильтра
    # строгости отбора — личные значения подписчика поверх общих (см. config._DEFAULTS).
    s = settings or {}
    vol_mult = config.VOL_MULT
    break_pct = config.BREAK_PCT
    min_rr = config.MIN_RR
    max_entry_dist = s.get("MAX_ENTRY_DIST_ATR", config.get("MAX_ENTRY_DIST_ATR"))
    max_risk_atr = s.get("MAX_RISK_ATR", config.get("MAX_RISK_ATR"))
    # ВАЛЮТНЫЕ ПАРЫ идут по своим порогам: глубина пробоя и запас стопа — в долях
    # ATR, а не в процентах цены (в процентах они на форексе не срабатывают вовсе,
    # см. config.FX_BREAK_ATR). ATR-фильтры строгости к ним НЕ применяются: они
    # настроены на крипту и забраковали бы форекс-сигнал механически, ещё до всякой
    # оценки качества — у форекса риск сделки почти всегда больше 0.75 ATR.
    # На крипту это ничего не меняет: там фильтры работают как работали.
    fx = is_fx(code)
    if fx and config.FX_IGNORE_ATR_FILTERS:
        max_entry_dist = max_risk_atr = 0

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

    # ATR считаем ЗДЕСЬ, а не выше: свеча уже прошла фильтр объёма, и таких свечей
    # единицы. Считать ATR на каждом баре подряд — лишняя работа.
    # Форексу ATR нужен ВСЕГДА: в нём заданы и глубина пробоя, и запас стопа.
    atr = _atr(df, pos) if (fx or max_entry_dist or max_risk_atr) else 0.0
    # Без ATR у валютной пары считать нечем. Сигнал не выдаём и НЕ падаем обратно на
    # проценты цены: они на форексе не работают, и это была бы тихая подмена порогов
    # на те, что заведомо молчат.
    if fx and atr <= 0:
        return None

    # Фильтр «не входить вдогонку»: риск сделки (закрытие → экстремум свечи пробоя +
    # запас стопа) не больше max_risk_atr × ATR. Свеча пробоя может быть аномально
    # большой — тогда вход по её закрытию оказывается в конце размашистого бара, а
    # пружина торгуется ОТ уровня, а не вдогонку. Риск от УРОВНЯ не зависит (вход —
    # закрытие, стоп — экстремум той же свечи), поэтому считаем один раз до перебора.
    if atr > 0 and max_risk_atr:
        buffer = _stop_buffer(l if side == "long" else h, atr, fx)
        risk_now = (c - l + buffer) if side == "long" else (h - c + buffer)
        if risk_now > atr * max_risk_atr:
            return None

    level_type = "support" if side == "long" else "resistance"
    relevant = [lvl for lvl in levels if lvl["type"] == level_type]

    for lvl in relevant:
        price = lvl["price"]
        depth = _break_depth(price, atr, fx)
        if side == "long":
            broke = l < price - depth              # пробили поддержку вниз
            returned = c > price                   # закрылись обратно выше
        else:
            broke = h > price + depth              # пробили сопротивление вверх
            returned = c < price                   # закрылись обратно ниже
        if not (broke and returned):
            continue
        # Фильтр «вход у уровня»: закрылись слишком далеко от уровня — это вход
        # вдогонку, а не от уровня. Здесь именно continue, а не return: расстояние
        # у каждого уровня своё, и следующий в списке может оказаться ближе.
        # Считаем от ЗАКРЫТИЯ свечи (c), а не от цены лимитной заявки — по той же
        # причине, что и MIN_RR ниже: набор сигналов не должен зависеть от того,
        # куда мы поставили заявку.
        if atr > 0 and max_entry_dist and abs(c - price) > atr * max_entry_dist:
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
            stop = l - _stop_buffer(l, atr, fx)
            risk = c - stop
            target = _nearest(levels, "resistance", c, above=True)
            if target is None:
                tp = c + risk * min_rr
            elif (target - c) >= risk * min_rr:
                tp = target
            else:
                continue
        else:
            stop = h + _stop_buffer(h, atr, fx)
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


def explain(df: pd.DataFrame, levels: list[dict], trend: str,
            settings: dict | None = None, code: str | None = None) -> dict:
    """Что движок видит на последней закрытой свече и чего ему не хватает до сигнала.

    Это «рентген» детектора для команды /analyze: те же пять условий, в том же
    порядке, по тем же числам — но вместо «сигнал / не сигнал» возвращается разбор,
    на каком именно условии всё встало. Сетевых запросов нет, состояние не меняется.

    Держать разбор рядом с _detect обязательно: разъедутся — и /analyze начнёт врать
    про то, чего движок на самом деле ждёт. Любая правка условий в _detect должна
    отражаться здесь.

    Возвращает dict; ключ 'sides' содержит по разбору на лонг и на шорт.
    """
    s = settings or {}
    max_entry_dist = s.get("MAX_ENTRY_DIST_ATR", config.get("MAX_ENTRY_DIST_ATR"))
    max_risk_atr = s.get("MAX_RISK_ATR", config.get("MAX_RISK_ATR"))
    # Те же правила для валютных пар, что и в _detect (см. там). Держать в согласии
    # обязательно: иначе /analyze покажет пороги и блокеры, которых у движка нет.
    fx = is_fx(code)
    if fx and config.FX_IGNORE_ATR_FILTERS:
        max_entry_dist = max_risk_atr = 0

    if len(df) < config.VOL_LOOKBACK + 3:
        return {"enough_history": False}

    pos = len(df) - 2  # последняя ЗАКРЫТАЯ свеча — по ней и работает детектор
    candle = df.iloc[pos]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])
    vol = float(candle["volume"])
    avg_vol = _avg_volume(df, pos)
    vol_ratio = (vol / avg_vol) if avg_vol > 0 else 0.0
    atr = _atr(df, pos)

    def near(level_type: str, above: bool, limit: int = 3) -> list[dict]:
        """Ближайшие уровни нужного типа с расстоянием от закрытия — в ATR и в %.

        Близкие уровни СЛИВАЕМ (в пределах 0.15% друг от друга): фрактал на H1 часто
        отмечает одну и ту же вершину тремя соседними барами, и без склейки отчёт
        печатает «1.3520, 1.3520, 1.3520» — три строки об одном уровне. При склейке
        сильный побеждает: если хоть один из совпавших помечен strong, таким и
        остаётся весь уровень.
        """
        items = [lvl for lvl in levels if lvl["type"] == level_type
                 and (lvl["price"] > c if above else lvl["price"] < c)]
        items.sort(key=lambda x: abs(x["price"] - c))
        out: list[dict] = []
        for lvl in items:
            dist = abs(lvl["price"] - c)
            same = next((k for k in out
                         if abs(k["price"] - lvl["price"]) <= lvl["price"] * 0.0015), None)
            if same is not None:
                if lvl.get("strength") == "strong":
                    same["strength"] = "strong"
                    same["timeframe"] = lvl.get("timeframe", same["timeframe"])
                continue
            out.append({
                "price": lvl["price"],
                "strength": lvl.get("strength", "weak"),
                "timeframe": lvl.get("timeframe", ""),
                "dist": dist,
                "dist_atr": (dist / atr) if atr > 0 else None,
                "dist_pct": dist / c if c else None,
            })
            if len(out) >= limit:
                break
        return out

    sides: dict[str, dict] = {}
    for side in ("long", "short"):
        blockers: list[str] = []
        trend_ok = not (side == "long" and trend == "down") and \
                   not (side == "short" and trend == "up")
        if not trend_ok:
            blockers.append("тренд дневки против сделки")
        vol_ok = avg_vol > 0 and vol >= avg_vol * config.VOL_MULT
        if not vol_ok:
            blockers.append(f"объём {vol_ratio:.1f}× среднего — порог ×{config.VOL_MULT:g}")

        # Условие «не входить вдогонку» (если включено) — от уровня не зависит.
        risk_now = None
        risk_atr_now = None
        if atr > 0:
            buffer = _stop_buffer(l if side == "long" else h, atr, fx)
            risk_now = (c - l + buffer) if side == "long" else (h - c + buffer)
            risk_atr_now = risk_now / atr
            if max_risk_atr and risk_atr_now > max_risk_atr:
                blockers.append(
                    f"риск сделки {risk_atr_now:.2f} ATR — фильтр «вдогонку» пускает "
                    f"до {max_risk_atr:g}")

        # Перебор уровней ровно как в _detect: ищем ложный пробой.
        level_type = "support" if side == "long" else "resistance"
        relevant = [lvl for lvl in levels if lvl["type"] == level_type]
        far_from_level = closed_wrong = False
        broken = None
        rr = None
        target = None
        for lvl in relevant:
            price = lvl["price"]
            depth = _break_depth(price, atr, fx)
            if side == "long":
                broke, returned = l < price - depth, c > price
            else:
                broke, returned = h > price + depth, c < price
            if not broke:
                continue
            if not returned:
                closed_wrong = True
                continue
            if atr > 0 and max_entry_dist and abs(c - price) > atr * max_entry_dist:
                far_from_level = True
                continue
            broken = {"price": price, "strength": lvl.get("strength", "weak"),
                      "dist_atr": (abs(c - price) / atr) if atr > 0 else None}
            break

        # break_note — отдельная строка ИМЕННО про пробой. Держим её полем, а не
        # выуживаем из списка блокеров: в blockers лежат и тренд, и объём, и фильтры,
        # и «первый попавшийся» оттуда — не обязательно про пробой.
        break_note = None
        if broken is None:
            if far_from_level:
                break_note = ("прокол есть, но закрылись далеко от уровня "
                              f"(фильтр «вход у уровня» пускает до {max_entry_dist:g} ATR)")
            elif closed_wrong:
                break_note = ("уровень проколот, но цена НЕ вернулась за него — "
                              "пробой не ложный")
            else:
                break_note = "свеча не заходила за уровень"
            blockers.append(break_note)

        # Какой R:R вышел бы при входе прямо сейчас (вход — закрытие, как в отборе).
        if risk_now and risk_now > 0:
            opposite = "resistance" if side == "long" else "support"
            target = _nearest(levels, opposite, c, above=(side == "long"))
            if target is not None:
                rr = (abs(target - c)) / risk_now
                if rr < config.MIN_RR:
                    blockers.append(f"до ближайшей цели всего 1:{rr:.1f} — "
                                    f"порог 1:{config.MIN_RR:g}")
            else:
                # Уровня впереди нет — цель ставится на MIN_RR × риск, места хватает
                # по определению. Это не нехватка, а штатная запасная цель.
                rr = config.MIN_RR

        sides[side] = {
            "trend_ok": trend_ok,
            "vol_ok": vol_ok,
            "broken_level": broken,
            "break_note": break_note,
            "risk": risk_now,
            "risk_atr": risk_atr_now,
            "target": target,
            "rr": rr,
            "blockers": blockers,
            "ready": not blockers,
        }

    return {
        "enough_history": True,
        "bar_time": str(df.index[pos]),
        "close": c, "high": h, "low": l,
        "volume": vol, "avg_volume": avg_vol, "vol_ratio": vol_ratio,
        "vol_mult": config.VOL_MULT,
        "atr": atr, "atr_pct": (atr / c) if c else 0.0,
        "trend": trend,
        "resistances": near("resistance", above=True),
        "supports": near("support", above=False),
        "filters": {"MAX_ENTRY_DIST_ATR": max_entry_dist, "MAX_RISK_ATR": max_risk_atr},
        # fx=True — разбор считался по валютным порогам (доли ATR), а ATR-фильтры
        # строгости к инструменту не применялись. Отчёт /analyze это показывает.
        "fx": fx,
        "sides": sides,
    }


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
