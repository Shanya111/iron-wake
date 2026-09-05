"""Обнаружение паттернов ложного пробоя на H1 (стратегия №2).

Spring (пружина): цена пробивает поддержку вниз, но свеча закрывается обратно выше
уровня на повышенном объёме → ложный пробой → сигнал в ЛОНГ.
Upthrust — зеркало по сопротивлению (пробой вверх, закрытие ниже) → сигнал в ШОРТ.

Работаем по последней ЗАКРЫТОЙ свече H1 (df.iloc[-2]); df.iloc[-1] обычно ещё
формируется и для оценки «закрылась обратно» не годится.

── Все пороги детектора — В ДОЛЯХ ATR (2 сентября 2026) ─────────────────────────
Глубина пробоя и запас стопа заданы долей ATR, а не процентом цены. «0.05% цены» у
биткоина и у золота значат разное: у золота волатильность в процентах ниже, и прежний
порог требовал от него прокола в 2.6 раза глубже в его же ATR. Значения подобраны по
эквиваленту прежних процентов — см. config.BREAK_ATR.

Отсюда следствие для кода: ATR нужен ВСЕГДА, а не только когда включён хоть один
фильтр строгости. Нет ATR (плоские свечи) — сигнала нет.

── Стоп за ДАЛЬНИМ из двух часовых экстремумов (2 сентября 2026) ────────────────
Стоп уходит не за экстремум одной только свечи свипа, а за дальний из неё и предыдущей
(config.STOP_STRUCT_BARS), плюс запас в долях ATR. Сетап — это пара свечей: свип выносит
стопы фитилём, структура образована соседним баром. Стоп внутри пары сносится
собственным шумом сетапа.

── СИЛА ОТБОЯ: закрытие в верхних 60% размаха свечи (2 сентября 2026) ───────────
Мало вернуться за уровень — вернуться надо решительно. Свеча, еле переползшая уровень
обратно, и свеча, выкупившая прокол целиком, до этой правки были для движка одинаковы;
теперь закрытие должно лежать не ниже config.MIN_CLOSE_POS размаха свечи (для лонга).

Это ЕДИНСТВЕННЫЙ рычаг проекта, улучшивший брутто и нетто одновременно (−0.050 → −0.030
и −0.136 → −0.084), ценой трети сигналов. Замер, дуга доза-эффект и проверка значимости —
в config.MIN_CLOSE_POS. Продолжения у него нет: displacement (тот же импульс, но в долях
ATR) и BOS измерены и отклонены.

── Фильтра R:R больше НЕТ (2 сентября 2026) ─────────────────────────────────────
Цель — ближайший встречный уровень, какой есть. Прежний порог 1:2 отбрасывал сигнал,
если до уровня было мало места; он срезал около 80% сетапов, и снят по решению
владельца. config.FALLBACK_RR осталась, но теперь она делает ровно одно — ставит цель,
когда впереди нет ни одного уровня. Цена решения записана там же.

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


def _stop_extreme(df: pd.DataFrame, pos: int, side: str, bars: int | None = None) -> float:
    """Экстремум, ЗА который уходит стоп: дальний из свечи свипа и `bars` предыдущих.

    Для лонга это минимум окна, для шорта — максимум. При bars=0 возвращается экстремум
    самой свечи свипа, то есть прежнее поведение.

    Смысл в том, что сетап — это не одна свеча. Свип выносит стопы фитилём, а сама
    структура (уровень, с которого пошло движение) обычно образована парой соседних
    баров. Стоп внутри этой пары стоит там, где его снесёт собственный шум сетапа.

    Общая для _detect и explain намеренно, как _stop_buffer и _break_depth: разъедутся —
    и /analyze покажет не тот риск, по которому движок принимает решение.
    """
    bars = config.STOP_STRUCT_BARS if bars is None else bars
    window = df.iloc[max(0, pos - bars):pos + 1]
    return float(window["low"].min()) if side == "long" else float(window["high"].max())


def _stop_buffer(atr: float) -> float:
    """Запас стопа за экстремум свечи пробоя, в абсолютных единицах цены.

    Доля ATR (config.STOP_ATR) — одна и та же на всех инструментах движка. От самого
    экстремума величина запаса больше не зависит: в этом и смысл перехода с процентов
    цены на ATR.

    Функция общая для _detect и explain намеренно: разъедутся — и /analyze начнёт
    показывать не тот риск, по которому движок принимает решение."""
    return atr * config.STOP_ATR


def _break_depth(atr: float) -> float:
    """Требуемая глубина прокола уровня, в абсолютных единицах цены.

    Доля ATR (config.BREAK_ATR). От цены самого уровня не зависит — раньше зависела,
    и из-за этого один и тот же порог значил разное на разных инструментах.

    Общая для _detect и explain по той же причине, что и _stop_buffer."""
    return atr * config.BREAK_ATR


def _target_gap(atr: float) -> float:
    """Минимальное расстояние от закрытия до цели, в абсолютных единицах цены.

    Доля ATR (config.MIN_TARGET_ATR). Уровни ближе этого при выборе цели
    пропускаются — иначе цель встаёт вплотную и сделка идёт за копейками при
    полном риске (см. комментарий к константе).

    Общая для _detect и explain по той же причине, что и _stop_buffer: разъедутся —
    и /analyze покажет один R:R, а движок посчитает по другому уровню."""
    return atr * config.MIN_TARGET_ATR


def _close_pos(h: float, l: float, c: float, side: str) -> float | None:
    """Сила отбоя: доля размаха свечи, отделяющая закрытие от НУЖНОГО края.

    Для лонга это (close − low) / размах: 1.0 — закрылись ровно на максимуме свечи,
    то есть прокол выкупили целиком; 0.1 — еле переползли обратно. Для шорта зеркально.

    None — у свечи нет размаха (high == low). Тогда судить не по чему, и вызывающий
    обязан считать условие непройденным: молча пропустить значило бы выдать сигнал
    там, где движок не смог проверить своё же условие.

    Общая для _detect и explain намеренно, как _stop_buffer и _break_depth: разъедутся —
    и /analyze начнёт обещать сигнал, которого движок не даст."""
    rng = h - l
    if rng <= 0:
        return None
    return (c - l) / rng if side == "long" else (h - c) / rng


def _pools(df: pd.DataFrame, pos: int, atr: float, side: str) -> list[dict]:
    """Пулы ликвидности — РАВНЫЕ экстремумы перед свечой свипа, видимые сразу.

    Возвращает уровни в том же формате, что и analyzer.find_levels, чтобы их можно
    было просто добавить к списку уровней: цена, тип, сила, таймфрейм.

    ЗАЧЕМ ОТДЕЛЬНАЯ СУЩНОСТЬ, а не фрактал покороче. Уровни движка — фракталы с
    окном 3, и такой уровень попадает в базу через ТРИ свечи после того, как
    сформировался. Свип случается раньше, и свипать в этот момент нечего: у золота
    2 сентября двойное дно 4293.5 стало уровнем только к 05:00, а вынесли его в
    03:00. Пул из равных минимумов правого окна не требует вовсе.

    И фрактал с окном 1 тут не помог бы: минимум 02:00 не был локальным, потому что
    следующая свеча ушла ниже. Равные края — это про то, где стоят чужие стопы, а не
    про форму графика.

    Цена пула — САМЫЙ ДАЛЬНИЙ из равных краёв (минимум группы для лонга): пробивать
    надо весь пул, а не его ближний край. Сила 'weak': пул не проверялся дневкой,
    и выдавать его за сильный уровень было бы враньём в приоритете сигнала.

    Общая для _detect и explain намеренно, как _stop_buffer и _break_depth.
    """
    if atr <= 0 or pos <= 0:
        return []
    col = "low" if side == "long" else "high"
    start = max(0, pos - config.POOL_LOOKBACK)
    vals = [float(v) for v in df[col].iloc[start:pos]]  # свечи строго ДО свипа
    if len(vals) < config.POOL_MIN_TOUCHES:
        return []
    tol = atr * config.EQUAL_EXTREME_ATR
    out: list[dict] = []
    for v in vals:
        group = [x for x in vals if abs(x - v) <= tol]
        if len(group) < config.POOL_MIN_TOUCHES:
            continue
        price = min(group) if side == "long" else max(group)
        if any(abs(price - o["price"]) <= tol for o in out):
            continue
        out.append({"price": price,
                    "type": "support" if side == "long" else "resistance",
                    "strength": "weak", "is_liquidity": 0, "timeframe": "pool"})
    return out


def _fresh_cross(df: pd.DataFrame, pos: int, level: float, side: str) -> bool:
    """Была ли цена ПО НУЖНУЮ СТОРОНУ уровня перед свипом — то есть свип ли это.

    Для лонга: хотя бы одна из config.FRESH_CROSS_BARS свечей перед свечой свипа
    закрылась ВЫШЕ поддержки. Иначе цена уже жила под уровнем, и «прокол с
    возвратом» — это не снятие ликвидности, а выкуп уровня снизу.

    Без этой проверки любой ретест снизу читался как пружина: GOLD 2 сентября
    11:00 (цена пять часов закрывалась под «поддержкой») и BTC 6 июля 15:00 (риск
    вышел 3% цены вместо обычных 0.5%). Подробности и цена — в config.FRESH_CROSS_BARS.

    Общая для _detect и explain по той же причине, что и _stop_buffer.
    """
    start = max(0, pos - config.FRESH_CROSS_BARS)
    closes = df["close"].iloc[start:pos]
    if not len(closes):
        return False
    return bool((closes > level).any()) if side == "long" else bool((closes < level).any())


def _engulfs(prev, cur, side: str) -> bool:
    """Поглотила ли свеча `cur` тело свечи `prev` в сторону сделки.

    Требуется только когда свип и выкуп — РАЗНЫЕ свечи (config.SWEEP_WINDOW): свеча
    А ушла под уровень и закрылась под ним, свеча B выкупила её обратно. Без
    поглощения правило «две свечи» означало бы «любой возврат за уровень в пределах
    пары часов», а это уже не свип.

    Для лонга: открытие не выше тела А, закрытие выше всего тела А.

    Общая для _detect и explain по той же причине, что и _stop_buffer.
    """
    po, pc = float(prev["open"]), float(prev["close"])
    co, cc = float(cur["open"]), float(cur["close"])
    if side == "long":
        return cc > max(po, pc) and co <= min(po, pc)
    return cc < min(po, pc) and co >= max(po, pc)


def _sweep_candidates(df: pd.DataFrame, pos: int, side: str) -> list[int]:
    """Свечи, которые вообще МОГУТ быть свипом: сигнальная и config.SWEEP_WINDOW перед ней.

    Объём здесь не проверяется — это делает _sweep_bars. Разделено на две функции
    потому, что explain обязан назвать объём даже у свечи, которая порог не прошла:
    иначе блокер напечатает число не той свечи.

    Чужая свеча засчитывается кандидатом только если сигнальная её ПОГЛОТИЛА. Без
    этого «две свечи» означало бы «любой возврат за уровень в пределах пары часов»,
    а это уже не свип (см. config.SWEEP_WINDOW).

    Порядок — от поздних к ранним: одна свеча, сделавшая всё сама, по-прежнему
    предпочтительный сетап, двухсвечный разбирается следом.
    """
    out = []
    for i in range(pos, pos - config.SWEEP_WINDOW - 1, -1):
        if i <= 0 or i >= len(df):
            continue
        if i != pos and not _engulfs(df.iloc[i], df.iloc[pos], side):
            continue
        out.append(i)
    return out


def _sweep_bars(df: pd.DataFrame, pos: int, side: str, vol_mult: float) -> list[int]:
    """Кандидаты на свип, прошедшие фильтр объёма.

    Объём проверяется на свече, которая СНИМАЕТ ликвидность, а не на той, которая её
    выкупает: на золоте 2 сентября свип дал ×1.81, а свеча поглощения — ×0.82.
    Прежний движок мерил объём не на той свече и такой сетап терял.
    """
    bars = []
    for i in _sweep_candidates(df, pos, side):
        avg = _avg_volume(df, i)
        if avg > 0 and float(df["volume"].iloc[i]) >= avg * vol_mult:
            bars.append(i)
    return bars


def _nearest(levels: list[dict], level_type: str, ref_price: float, above: bool,
             min_gap: float = 0.0):
    """Ближайший уровень нужного типа выше (above=True) или ниже ref_price.

    min_gap — уровни ближе этого расстояния ПРОПУСКАЮТСЯ, берётся следующий за ними
    (config.MIN_TARGET_ATR, 3 сентября 2026). Так отсекаются вырожденные цели вроде
    «риск 35 ради 2», которыми обернулось снятие фильтра R:R. Отличие от того фильтра
    принципиальное: он отбрасывал СИГНАЛ, этот двигает ЦЕЛЬ — число сигналов не
    меняется. Если дальше нет ни одного уровня, вернётся None, и вызывающий код
    поставит запасную цель на FALLBACK_RR × риск.
    """
    prices = [
        l["price"] for l in levels
        if l["type"] == level_type
        and (l["price"] > ref_price + min_gap if above
             else l["price"] < ref_price - min_gap)
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
    # Пороги отбора (объём, глубина пробоя, запас стопа) — обычные константы: с 20
    # августа 2026 пользователь их не крутит. Из settings приходят только два ATR-фильтра
    # строгости отбора — личные значения подписчика поверх общих (см. config._DEFAULTS).
    s = settings or {}
    vol_mult = config.VOL_MULT
    max_entry_dist = s.get("MAX_ENTRY_DIST_ATR", config.get("MAX_ENTRY_DIST_ATR"))
    max_risk_atr = s.get("MAX_RISK_ATR", config.get("MAX_RISK_ATR"))

    if len(df) < config.VOL_LOOKBACK + 3:
        return None
    # Фильтр направления по глобальному тренду.
    if side == "long" and trend == "down":
        return None
    if side == "short" and trend == "up":
        return None

    # Сигнальная свеча — последняя закрытая. По её закрытию уходит сигнал и по её
    # закрытию считается вход: ждать после неё нечего.
    pos = len(df) - 2
    candle = df.iloc[pos]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])

    # Условие №1: СИЛА ОТБОЯ. Это свойство СИГНАЛЬНОЙ свечи — той, что выкупает
    # прокол: закрытие не ниже верхних config.MIN_CLOSE_POS её размаха (для лонга).
    # Свеча без размаха (_close_pos вернул None) условие НЕ проходит: судить не по чему.
    rebound = _close_pos(h, l, c, side)
    if rebound is None or rebound < config.MIN_CLOSE_POS:
        return None

    # Условие №2: аномальный объём. Это свойство свечи СВИПА, а не свечи выкупа —
    # ликвидность снимают на объёме, выкупают не обязательно (GOLD 2 сентября: свип
    # x1.81, поглощение x0.82). Кандидатов может быть двое: сама сигнальная свеча
    # (сетап в одну свечу, как было всегда) и предыдущая, если сигнальная её
    # ПОГЛОТИЛА — см. config.SWEEP_WINDOW.
    sweeps = _sweep_bars(df, pos, side, vol_mult)
    if not sweeps:
        return None

    # ATR считаем ЗДЕСЬ, а не выше: свеча уже прошла отбой и объём, и таких свечей
    # единицы. Нужен он ВСЕГДА: в долях ATR заданы и глубина пробоя, и запас стопа,
    # и допуск равных экстремумов. Нет ATR (плоские свечи) — сигнала нет, и отката
    # на проценты цены не происходит: это была бы тихая подмена порогов на другие.
    atr = _atr(df, pos)
    if atr <= 0:
        return None

    # Глубина прокола от уровня не зависит (доля ATR) — считаем один раз.
    depth = _break_depth(atr)
    level_type = "support" if side == "long" else "resistance"

    for sw in sweeps:
        sweep = df.iloc[sw]
        sl, sh = float(sweep["low"]), float(sweep["high"])
        # Стоп уходит за экстремум ВСЕГО сетапа: свечи свипа, config.STOP_STRUCT_BARS
        # перед ней и свечи выкупа, если это разные свечи. Экстремум зависит только от
        # свечей, не от уровня, поэтому считаем один раз на кандидата.
        extreme = _stop_extreme(df, pos, side, bars=pos - sw + config.STOP_STRUCT_BARS)
        # Фильтр «не входить вдогонку»: риск сделки (закрытие -> экстремум сетапа +
        # запас стопа) не больше max_risk_atr x ATR. Риск от УРОВНЯ не зависит,
        # поэтому проверяем до перебора уровней; continue, а не return — у другого
        # кандидата на свип свой экстремум и свой риск.
        if max_risk_atr:
            buffer = _stop_buffer(atr)
            risk_now = (c - extreme + buffer) if side == "long" else (extreme - c + buffer)
            if risk_now > atr * max_risk_atr:
                continue

        # Уровни-фракталы из базы ПЛЮС пулы равных экстремумов, посчитанные прямо
        # сейчас. Пул нужен потому, что фрактал подтверждается только через три
        # свечи справа и на момент свипа его в базе ещё нет — см. _pools.
        relevant = [lvl for lvl in levels if lvl["type"] == level_type]
        relevant += _pools(df, sw, atr, side)

        for lvl in relevant:
            price = lvl["price"]
            # Прокол — по экстремуму свечи СВИПА, возврат — по закрытию СИГНАЛЬНОЙ.
            # При сетапе в одну свечу это одна и та же свеча, и правило прежнее.
            if side == "long":
                broke = sl < price - depth              # пробили поддержку вниз
                returned = c > price                    # закрылись обратно выше
            else:
                broke = sh > price + depth              # пробили сопротивление вверх
                returned = c < price                    # закрылись обратно ниже
            if not (broke and returned):
                continue
            # СВИП ОБЯЗАН БЫТЬ СВИПОМ: до прокола цена должна была стоять по нужную
            # сторону уровня. Иначе это выкуп уровня СНИЗУ, а не снятие ликвидности
            # (GOLD 11:00 2 сентября, BTC 15:00 6 июля) — см. config.FRESH_CROSS_BARS.
            if not _fresh_cross(df, sw, price, side):
                continue
            # Фильтр «вход у уровня»: закрылись слишком далеко от уровня — это вход
            # вдогонку, а не от уровня. Здесь именно continue, а не return: расстояние
            # у каждого уровня своё, и следующий в списке может оказаться ближе.
            # Считаем от ЗАКРЫТИЯ свечи (c), а не от цены лимитной заявки — по той же
            # причине, что и цель ниже: набор сигналов не должен зависеть от того, куда
            # мы поставили заявку.
            if max_entry_dist and abs(c - price) > atr * max_entry_dist:
                continue

            priority = "high" if lvl.get("strength") == "strong" else "normal"
            # Цель — ближайший противоположный уровень, НЕ БЛИЖЕ config.MIN_TARGET_ATR
            # (3 сентября 2026). Проверки «а даёт ли он хотя бы 1:2» по-прежнему нет: она
            # срезала около 80% сетапов (см. config.FALLBACK_RR, там же цена решения).
            # Разница между ними в том, что отбраковывалось: тот фильтр выбрасывал СИГНАЛ,
            # а порог расстояния двигает ЦЕЛЬ на следующий уровень — сигналов остаётся
            # столько же. Нет уровня впереди — цель на FALLBACK_RR x риск.
            # Уровень в любом случае лежит по нужную сторону от закрытия (см. _nearest),
            # поэтому вырожденной цели «в ноль или в минус» тут возникнуть не может.
            #
            # ВАЖНО: и риск, и цель считаются ОТ ЗАКРЫТИЯ свечи, а не от цены лимитной
            # заявки. Так набор сигналов не зависит от ENTRY_PULLBACK — заявка меняет только
            # цену входа, а какие сетапы вообще берём, решает та же логика. Иначе замер
            # «что даёт лимитный вход» смешал бы эффект входа с эффектом отбора.
            if side == "long":
                stop = extreme - _stop_buffer(atr)
                risk = c - stop
                target = _nearest(levels, "resistance", c, above=True,
                                  min_gap=_target_gap(atr))
                tp = target if target is not None else c + risk * config.FALLBACK_RR
            else:
                stop = extreme + _stop_buffer(atr)
                risk = stop - c
                target = _nearest(levels, "support", c, above=False,
                                  min_gap=_target_gap(atr))
                tp = target if target is not None else c - risk * config.FALLBACK_RR

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
                # bar_time — СИГНАЛЬНАЯ свеча: по её закрытию вход, от неё считает
                # трекинг. sweep_bar_time — свеча, снявшая ликвидность (совпадает с
                # сигнальной, когда сетап уложился в одну свечу).
                "bar_time": str(df.index[pos]),
                "sweep_bar_time": str(df.index[sw]),
            }
    return None



def explain(df: pd.DataFrame, levels: list[dict], trend: str,
            settings: dict | None = None) -> dict:
    """Что движок видит на последней закрытой свече и чего ему не хватает до сигнала.

    Это «рентген» детектора для команды /analyze: те же условия, в том же порядке,
    по тем же числам — но вместо «сигнал / не сигнал» возвращается разбор, на каком
    именно условии всё встало. Сетевых запросов нет, состояние не меняется.

    Держать разбор рядом с _detect обязательно: разъедутся — и /analyze начнёт врать
    про то, чего движок на самом деле ждёт. Любая правка условий в _detect должна
    отражаться здесь. Порядок перебора (сначала свечи свипа, внутри — уровни) тоже
    повторён специально: при включённом фильтре «вдогонку» от него зависит вердикт.

    Возвращает dict; ключ 'sides' содержит по разбору на лонг и на шорт.
    """
    s = settings or {}
    max_entry_dist = s.get("MAX_ENTRY_DIST_ATR", config.get("MAX_ENTRY_DIST_ATR"))
    max_risk_atr = s.get("MAX_RISK_ATR", config.get("MAX_RISK_ATR"))

    if len(df) < config.VOL_LOOKBACK + 3:
        return {"enough_history": False}

    pos = len(df) - 2  # последняя ЗАКРЫТАЯ свеча — по ней и работает детектор
    candle = df.iloc[pos]
    h, l, c = float(candle["high"]), float(candle["low"]), float(candle["close"])
    vol = float(candle["volume"])
    avg_vol = _avg_volume(df, pos)
    vol_ratio = (vol / avg_vol) if avg_vol > 0 else 0.0
    atr = _atr(df, pos)

    # Пулы ликвидности движок теперь свипает наравне с уровнями, поэтому человеку их
    # надо показать. Но в СПИСОК УРОВНЕЙ они не идут: там лежит то, что видно на
    # графике и на что вешаются алерты, а пул живёт config.POOL_LOOKBACK свечей и
    # пересчитывается каждый раз. Отдаём отдельным ключом, отчёт печатает строкой.
    pools = (_pools(df, pos, atr, "long") + _pools(df, pos, atr, "short")
             if atr > 0 else [])
    shown = levels

    def near(level_type: str, above: bool, limit: int = 3) -> list[dict]:
        """Ближайшие уровни нужного типа с расстоянием от закрытия — в ATR и в %.

        Близкие уровни СЛИВАЕМ (в пределах 0.15% друг от друга): фрактал на H1 часто
        отмечает одну и ту же вершину тремя соседними барами, и без склейки отчёт
        печатает «1.3520, 1.3520, 1.3520» — три строки об одном уровне. При склейке
        сильный побеждает: если хоть один из совпавших помечен strong, таким и
        остаётся весь уровень.
        """
        items = [lvl for lvl in shown if lvl["type"] == level_type
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

        # Сила отбоя — свойство СИГНАЛЬНОЙ свечи, то же условие и в том же месте,
        # что в _detect.
        rebound = _close_pos(h, l, c, side)
        rebound_ok = rebound is not None and rebound >= config.MIN_CLOSE_POS
        if not rebound_ok:
            blockers.append(
                "свеча без размаха — силу отбоя не измерить" if rebound is None else
                f"отбой {rebound:.2f} размаха свечи — порог {config.MIN_CLOSE_POS:g} "
                "(прокол выкупили вяло)")

        # Объём — свойство свечи СВИПА. Кандидатов может быть двое: сама сигнальная
        # свеча и предыдущая, если сигнальная её поглотила (config.SWEEP_WINDOW).
        cands = _sweep_candidates(df, pos, side)
        ratios = {}
        for i in cands:
            a = _avg_volume(df, i)
            ratios[i] = (float(df["volume"].iloc[i]) / a) if a > 0 else 0.0
        sweeps = [i for i in cands if ratios[i] >= config.VOL_MULT]
        vol_ok = bool(sweeps)
        # Показываем ту свечу, по которой движок судил: прошедшую объём, а если ни
        # одна не прошла — лучшую из кандидатов (иначе блокер назвал бы не то число).
        vol_bar = sweeps[0] if sweeps else (
            max(ratios, key=ratios.get) if ratios else pos)
        side_vol_ratio = ratios.get(vol_bar, vol_ratio)
        if not vol_ok:
            blockers.append(
                f"объём {side_vol_ratio:.1f}× среднего — порог ×{config.VOL_MULT:g}")

        # Нет ATR — в _detect это ранний выход, значит и здесь обязан быть блокер:
        # иначе отчёт скажет «условия сложились» там, где движок молчит.
        if atr <= 0:
            blockers.append("ATR нулевой (свечи без размаха) — пороги не посчитать")

        # Перебор ровно как в _detect: снаружи свечи свипа, внутри уровни. Порядок
        # важен: фильтр «вдогонку» отбраковывает КАНДИДАТА целиком, и у следующего
        # кандидата риск свой.
        level_type = "support" if side == "long" else "resistance"
        far_from_level = closed_wrong = not_a_sweep = False
        risk_blocked = False
        broken = None
        sweep_used = vol_bar
        risk_now = risk_atr_now = None
        rr = None
        target = None
        if atr > 0:
            depth = _break_depth(atr)   # от уровня не зависит — как и в _detect
            buffer = _stop_buffer(atr)

            def risk_of(sw: int) -> float:
                extreme = _stop_extreme(df, pos, side,
                                        bars=pos - sw + config.STOP_STRUCT_BARS)
                return ((c - extreme + buffer) if side == "long"
                        else (extreme - c + buffer))

            def scan(sw: int):
                """Первый уровень, который свеча `sw` действительно свипнула."""
                nonlocal far_from_level, closed_wrong, not_a_sweep
                sl, sh = float(df["low"].iloc[sw]), float(df["high"].iloc[sw])
                relevant = [lvl for lvl in levels if lvl["type"] == level_type]
                relevant += _pools(df, sw, atr, side)
                for lvl in relevant:
                    price = lvl["price"]
                    if side == "long":
                        broke, returned = sl < price - depth, c > price
                    else:
                        broke, returned = sh > price + depth, c < price
                    if not broke:
                        continue
                    if not returned:
                        closed_wrong = True
                        continue
                    if not _fresh_cross(df, sw, price, side):
                        not_a_sweep = True
                        continue
                    if max_entry_dist and abs(c - price) > atr * max_entry_dist:
                        far_from_level = True
                        continue  # у следующего уровня расстояние своё, оно может подойти
                    return {"price": price, "strength": lvl.get("strength", "weak"),
                            "dist_atr": abs(c - price) / atr}
                return None

            order = sweeps or cands or [pos]
            # Фаза 1 — ровно как в _detect: кандидат, отбракованный фильтром
            # «вдогонку», пропускается целиком, у следующего риск свой.
            for sw in order:
                r = risk_of(sw)
                if max_risk_atr and r / atr > max_risk_atr:
                    risk_blocked = True
                    continue
                hit = scan(sw)
                if hit is not None:
                    broken, sweep_used = hit, sw
                    risk_now, risk_atr_now = r, r / atr
                    break
            # Фаза 2 — сигнала нет, и надо объяснить ЧЕЛОВЕКУ почему. Числа берём с
            # предпочтительного кандидата, а пробой ищем БЕЗ фильтра риска: фильтр
            # снимает сигнал, но не отменяет того, что прокол состоялся. Иначе в
            # пункте «Ложный пробой» печатался бы текст про «вдогонку» — отдельный
            # блокер подменял бы причину (тест
            # test_explain_break_note_is_about_breakout_only).
            if broken is None:
                sweep_used = order[0]
                risk_now = risk_of(sweep_used)
                risk_atr_now = risk_now / atr
                if max_risk_atr and risk_atr_now > max_risk_atr:
                    blockers.append(
                        f"риск сделки {risk_atr_now:.2f} ATR — фильтр «вдогонку» "
                        f"пускает до {max_risk_atr:g}")
                broken = scan(sweep_used)

        # break_note — отдельная строка ИМЕННО про пробой. Держим её полем, а не
        # выуживаем из списка блокеров: в blockers лежат и тренд, и объём, и фильтры,
        # и «первый попавшийся» оттуда — не обязательно про пробой.
        break_note = None
        if broken is None and atr > 0:
            if far_from_level:
                break_note = ("прокол есть, но закрылись далеко от уровня "
                              f"(фильтр «вход у уровня» пускает до {max_entry_dist:g} ATR)")
            elif not_a_sweep:
                where = "под ним" if side == "long" else "над ним"
                break_note = ("уровень выкуплен, но это не свип — до прокола цена "
                              f"уже стояла {where}")
            elif closed_wrong:
                break_note = ("уровень проколот, но цена НЕ вернулась за него — "
                              "пробой не ложный")
            elif risk_blocked:
                break_note = "свечу свипа отбраковал фильтр «вдогонку»"
            else:
                break_note = "свеча не заходила за уровень"
            blockers.append(break_note)

        # Какой R:R вышел бы при входе прямо сейчас (вход — закрытие, как в отборе).
        # Это СПРАВКА, а не условие: порога 1:2 больше нет, сигнал берётся с любым
        # встречным уровнем (см. config.FALLBACK_RR). Блокером тут ничего не станет.
        # Но уровни ближе _target_gap пропускаются ровно так же, как в _detect, —
        # иначе отчёт назовёт целью уровень, который движок целью не считает.
        if risk_now and risk_now > 0:
            opposite = "resistance" if side == "long" else "support"
            target = _nearest(levels, opposite, c, above=(side == "long"),
                              min_gap=_target_gap(atr))
            if target is not None:
                rr = (abs(target - c)) / risk_now
            else:
                # Уровня впереди нет — цель ставится на FALLBACK_RR × риск.
                rr = config.FALLBACK_RR

        sides[side] = {
            "trend_ok": trend_ok,
            "vol_ok": vol_ok,
            "vol_ratio": side_vol_ratio,
            # 0 — свип на самой сигнальной свече (сетап в одну свечу), 1 — свип был
            # на предыдущей, а сигнальная её поглотила.
            "sweep_offset": pos - sweep_used,
            "rebound": rebound,
            "rebound_ok": rebound_ok,
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
        "pools": [pl["price"] for pl in pools],
        "filters": {"MAX_ENTRY_DIST_ATR": max_entry_dist, "MAX_RISK_ATR": max_risk_atr},
        "sides": sides,
    }



def evaluate_fill(signal: dict, df: pd.DataFrame, wait_bars: int | None = None,
                  optimistic: bool = False) -> dict:
    """Исполнилась ли лимитная заявка.

    Возвращает {'status', 'fill_time', 'stopped_at_fill', 'reason'}. `reason` заполняется
    только при 'expired_unfilled' и говорит, ПОЧЕМУ сделки не было: 'target' — цена ушла
    к цели, не задев заявку; 'timeout' — за отведённые часы до заявки не дошла; 'stale' —
    свеча пробоя старше всего окна свечей (бот лежал). Причины разные по смыслу, и
    сообщение пользователю обязано их различать.

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
                "stopped_at_fill": False, "reason": "stale"}
    after = df[df.index > pd.Timestamp(bar_time)]

    for n, (ts, c) in enumerate(after.iloc[:wait_bars].iterrows(), start=1):
        hi, lo = float(c["high"]), float(c["low"])
        hit_limit = lo <= limit if long else hi >= limit
        hit_tp = hi >= tp if long else lo <= tp
        hit_stop = lo <= stop if long else hi >= stop
        if hit_tp and not (hit_limit and optimistic):
            return {"status": "expired_unfilled", "fill_time": None,
                    "stopped_at_fill": False, "reason": "target"}
        if hit_limit:
            return {"status": "filled", "fill_time": str(ts),
                    "stopped_at_fill": bool(hit_stop)}
    if len(after) >= wait_bars:
        return {"status": "expired_unfilled", "fill_time": None,
                "stopped_at_fill": False, "reason": "timeout"}
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
