"""ICT — свип ликвидности и разрыв (FVG) на пятнадцатиминутках. Стратегия №6.

Решение владельца 18.09.2026: поставить в бой вместо стратегии №4 (тренд по
недельному каналу, снята 17.09.2026) и назвать подписку «ICT».

ПРАВИЛА — ровно тот вариант, что мерился (`ict.py --m15` в лаборатории, строка
«вход по рынку, цель >= 1R»); для лонга, шорт зеркально:
  • решение — по последней ЗАКРЫТОЙ пятнадцатиминутке;
  • ПУЛ ЛИКВИДНОСТИ снизу: свинговый минимум (фрактал окна ICT_POOL_W), пул равных
    минимумов (в пределах ICT_EQ_ATR друг от друга — цена пула ДАЛЬНИЙ из них, как
    в _pools ложного пробоя) или минимум предыдущего дня;
  • СВИП: свеча уходит минимумом под пул, а до неё цена закрывалась над ним;
  • DISPLACEMENT: в течение ICT_SWEEP_BARS свечей после свипа — бычья свеча телом
    не меньше ICT_DISP_ATR × ATR;
  • FVG: она же оставляет трёхсвечный разрыв шириной не меньше ICT_FVG_ATR × ATR;
  • MSS: закрытие выше последнего свингового максимума, стоявшего ДО свипа;
  • вход — ПО РЫНКУ, закрытием свечи, на которой разрыв стал виден целиком;
  • стоп — за экстремумом манипуляции плюс ICT_STOP_ATR × ATR;
  • цель — противоположная ликвидность не ближе ICT_MIN_TP_R рисков; нет такой —
    ровно ICT_FALLBACK_RR риска.

ATR ВЕЗДЕ БЕРЁТСЯ НА СВЕЧЕ СВИПА, а не на свече решения. Это не мелочь: от него
считаются оба порога и запас стопа, и замер устроен так же. Сверка бота с замером
(`ict_verify.py` в лаборатории) первым делом поймала именно это расхождение.

ЦЕНА РЕШЕНИЯ НАЗВАНА ДО ВКЛЮЧЕНИЯ (замер 17–18.09.2026, `ict.py` / `ict_drift.py`
в лаборатории; 14 инструментов, M15 за 100 дней и H1 за 833 дня):
  • брутто −0.035 R на сделку, нетто −0.160 R;
  • на M15 сделка стоит 0.125 R против 0.056 R у той же модели на часовике: стоп
    узкий (1.03% цены), а комиссия берётся в процентах цены — девятое подряд
    воспроизведение закона «издержки в R»;
  • сам свип ликвидности неотличим от СЛУЧАЙНОЙ линии: 25959 реальных событий
    против 30683 плацебо, разница сноса −0.006 ±0.023. Разрыв БЕЗ свипа ведёт
    себя не хуже;
  • ни одно требование модели не несёт нагрузки: без слома структуры +0.020,
    с ним +0.020; без порога импульса столько же (H1, полная выборка).
Прибыльной стратегия по замеру не является. Решение продуктовое.

ЧАСТОТА. 2480 событий в месяц на 14 инструментов после дедупа 90 минут — около
6 в день на инструмент. Крутится одной константой ICT_DEDUP_MIN.

Функции чистые: ни сети, ни базы. Ведёт стратегию scheduler.monitor_ict.
"""

import numpy as np
import pandas as pd

import config


def atr_series(df: pd.DataFrame, period: int | None = None) -> np.ndarray:
    """ATR на каждом баре — векторная форма pattern_detector._atr.

    Зачем своя: детектору ATR нужен на десятках баров за вызов (на каждом пивоте при
    сборе пулов и на каждом кандидате в свип), и звать поштучный помощник значило бы
    пересчитывать одно и то же сотни раз на каждом круге планировщика. Совпадение с
    общим помощником проверяется тестом test_ict_atr_matches_pattern_detector —
    разойдутся, и пороги детектора поедут молча.
    """
    period = config.ATR_PERIOD if period is None else period
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    prev = np.empty(len(close))
    prev[0] = np.nan
    prev[1:] = close[:-1]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    out = np.full(len(tr), np.nan)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    for i in range(len(tr)):
        start = max(0, i - period + 1)
        out[i] = (csum[i + 1] - csum[start]) / (i + 1 - start)
    return out


def _swings(df: pd.DataFrame, w: int) -> tuple[list[int], list[int]]:
    """Позиции свинговых максимумов и минимумов (фрактал окна w).

    Свинг подтверждается только через w свечей справа, поэтому в момент решения
    свинга последних w баров ещё не существует. Соблюдает это вызывающий: везде, где
    берётся уровень, стоит проверка «подтверждён к нужной свече».
    """
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    highs, lows = [], []
    for p in range(w, len(df) - w):
        if all(high[p] > high[p - k] and high[p] >= high[p + k] for k in range(1, w + 1)):
            highs.append(p)
        if all(low[p] < low[p - k] and low[p] <= low[p + k] for k in range(1, w + 1)):
            lows.append(p)
    return highs, lows


def _pool_table(df: pd.DataFrame, pivots: list[int], atr: np.ndarray,
                side: str) -> list[tuple[float, int, int]]:
    """Пулы ликвидности одной стороны: (цена, бар подтверждения, бар устаревания).

    Пул равных экстремумов отдаётся ДАЛЬНЕЙ ценой: пробивать надо весь пул, а не его
    ближний край — правило _pools ложного пробоя. Сравнение идёт с несколькими
    последними пивотами, а не с одним: равными бывают и экстремумы через свечу.
    Уровень предыдущего дня добавляется здесь же и живёт сутки.
    """
    src = (df["high"] if side == "high" else df["low"]).to_numpy(float)
    w = config.ICT_POOL_W
    bar_min = config.ICT_TF_MINUTES
    age_bars = int(config.ICT_POOL_AGE_H * 60 / bar_min)
    day_bars = int(24 * 60 / bar_min)
    out: list[tuple[float, int, int]] = []
    for p in pivots:
        price = float(src[p])
        a = atr[p]
        if a > 0 and not np.isnan(a):
            for q in range(len(out) - 1, max(-1, len(out) - 12), -1):
                if p - out[q][1] > age_bars:
                    break
                if abs(out[q][0] - price) <= config.ICT_EQ_ATR * a:
                    price = (max(price, out[q][0]) if side == "high"
                             else min(price, out[q][0]))
                    break
        out.append((price, p + w, p + w + age_bars))
    days = df.index.floor("D").to_numpy()
    starts = np.flatnonzero(np.concatenate([[True], days[1:] != days[:-1]]))
    for a_i, b_i in zip(starts, list(starts[1:]) + [len(df)]):
        if b_i >= len(df):
            break
        prev = src[a_i:b_i]
        price = float(prev.max()) if side == "high" else float(prev.min())
        out.append((price, int(b_i), int(b_i) + day_bars))
    return out


def _live_pools(pools: list[tuple[float, int, int]], at: int) -> list[float]:
    """Цены пулов, известных к свече `at` и ещё не устаревших."""
    return [p for p, conf, exp in pools if conf <= at and exp >= at]


def _gap(df: pd.DataFrame, k: int, side: str) -> float:
    """Ширина трёхсвечного разрыва вокруг импульсной свечи k (0 и меньше — нет разрыва)."""
    if side == "long":
        return float(df["low"].iloc[k + 1]) - float(df["high"].iloc[k - 1])
    return float(df["low"].iloc[k - 1]) - float(df["high"].iloc[k + 1])


def _displacement(df: pd.DataFrame, k: int, side: str) -> float:
    """Тело импульсной свечи в сторону сделки (отрицательное — свеча против)."""
    body = float(df["close"].iloc[k]) - float(df["open"].iloc[k])
    return body if side == "long" else -body


def _mss(df: pd.DataFrame, swings: list[int], i: int, pos: int, side: str) -> bool:
    """Слом структуры: закрытие за последним свингом, стоявшим ДО свипа."""
    before = [p for p in swings if p < i - config.ICT_STRUCT_W]
    if not before:
        return False
    last = before[-1]
    if side == "long":
        return float(df["close"].iloc[i:pos + 1].max()) > float(df["high"].iloc[last])
    return float(df["close"].iloc[i:pos + 1].min()) < float(df["low"].iloc[last])


def _target(pools: list[float], entry: float, risk: float, side: str) -> float:
    """Противоположная ликвидность не ближе ICT_MIN_TP_R рисков; нет — 2 риска."""
    gap = config.ICT_MIN_TP_R * risk
    if side == "long":
        ahead = sorted(p for p in pools if p - entry >= gap)
        return ahead[0] if ahead else entry + config.ICT_FALLBACK_RR * risk
    ahead = sorted((p for p in pools if entry - p >= gap), reverse=True)
    return ahead[0] if ahead else entry - config.ICT_FALLBACK_RR * risk


def _setup(df, pos, k, side, atr, pools_dn, pools_up, struct_hi, struct_lo):
    """Сетап одной стороны или None. Кандидаты свипа перебираются от раннего к позднему.

    Порядок важен: сетап принадлежит ПЕРВОМУ свипу, после которого случился этот
    разрыв, — так же перебирает замер. Проверка идёт полностью для каждого кандидата:
    не прошёл слом структуры у раннего свипа — пробуем следующий, а не бросаем сторону.
    """
    own = pools_dn if side == "long" else pools_up
    other = pools_up if side == "long" else pools_dn
    swings = struct_hi if side == "long" else struct_lo
    for i in range(k - config.ICT_SWEEP_BARS, k):
        if i < 2:
            continue
        a = atr[i]                       # ATR свечи СВИПА — так мерилось
        if not (a > 0):
            continue
        if _displacement(df, k, side) < config.ICT_DISP_ATR * a:
            continue
        gap = _gap(df, k, side)
        if gap < config.ICT_FVG_ATR * a:
            continue
        c_prev = float(df["close"].iloc[i - 1])
        near = [p for p in _live_pools(own, i - 1)
                if (p < c_prev if side == "long" else p > c_prev)]
        if not near:
            continue
        pool = max(near) if side == "long" else min(near)
        swept = (float(df["low"].iloc[i]) < pool if side == "long"
                 else float(df["high"].iloc[i]) > pool)
        if not swept:
            continue
        # Разрыв обязан быть ПЕРВЫМ после свипа: был раньше — сетап объявлен на нём.
        if any(_gap(df, j, side) > 0 and _displacement(df, j, side) > 0
               for j in range(i + 1, k)):
            continue
        if not _mss(df, swings, i, pos, side):
            continue
        entry = float(df["close"].iloc[pos])
        if side == "long":
            manip = float(df["low"].iloc[i:pos + 1].min())
            stop = manip - config.ICT_STOP_ATR * a
        else:
            manip = float(df["high"].iloc[i:pos + 1].max())
            stop = manip + config.ICT_STOP_ATR * a
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        return {
            "direction": side,
            "pool_price": pool,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": _target(_live_pools(other, pos), entry, risk, side),
            "bar_time": str(df.index[pos]),
            "sweep_time": str(df.index[i]),
            "gap_atr": gap / a,
            "disp_atr": _displacement(df, k, side) / a,
            "risk_atr": risk / a,
        }
    return None


def detect(df: pd.DataFrame) -> list[dict]:
    """Сигналы ICT по последней закрытой свече — список из 0, 1 или 2 штук.

    Двух сразу не бывает (разрыв не может быть и бычьим, и медвежьим), но список
    возвращается ради симметрии с detect_spring/detect_upthrust и пробоем уровня.
    """
    need = config.ICT_POOL_W + config.ICT_SWEEP_BARS + config.ATR_PERIOD + 6
    if len(df) < need:
        return []
    pos = len(df) - 2                      # последняя закрытая свеча: на ней решаем
    k = pos - 1                            # импульсная свеча: разрыв виден на pos
    atr = atr_series(df)
    pivot_hi, pivot_lo = _swings(df, config.ICT_POOL_W)
    struct_hi, struct_lo = _swings(df, config.ICT_STRUCT_W)
    pools_dn = _pool_table(df, pivot_lo, atr, "low")
    pools_up = _pool_table(df, pivot_hi, atr, "high")

    out = []
    for side in ("long", "short"):
        sig = _setup(df, pos, k, side, atr, pools_dn, pools_up, struct_hi, struct_lo)
        if sig is not None:
            out.append(sig)
    return out


def outcome(signal: dict, df: pd.DataFrame) -> str:
    """Исход открытого сигнала: 'hit_tp' / 'hit_sl' / 'expired' / 'pending'.

    Считает общий помощник pattern_detector.evaluate_signal — тот же, что ведёт
    ложный пробой, пробой уровня и журнал сделок: правило «при двусмысленности
    внутри свечи сначала стоп» обязано быть одним на весь проект. Горизонт 48 часов,
    как в замере. Импорт локальный: pattern_detector тянет тяжёлый analyzer, а
    детектору ICT он не нужен вовсе.
    """
    import pattern_detector
    return pattern_detector.evaluate_signal(
        signal, df, expire_hours=config.ICT_EXPIRE_HOURS)


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
