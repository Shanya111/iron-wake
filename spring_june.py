"""Ложный пробой в редакции 23 июня 2026 — детектор первого движка (стратегия №2).

Правила — ровно детектор коммита c855660 (22 июня); в коммите 5982fa4 (23 июня) он не
менялся. Вернул их в бой владелец 15 сентября 2026 (config.SPRING_ENGINE = "june23"):

  • решение — по последней ЗАКРЫТОЙ часовой свече (df.iloc[-2]);
  • тренд дневки: лонг не берётся в нисходящем, шорт — в восходящем, в боковике обе;
  • объём этой свечи ≥ среднего за VOL_LOOKBACK (20) предыдущих × VOL_MULT (1.5);
  • свеча проколола уровень глубже BREAK_PCT (0.05%) цены уровня и закрылась обратно
    за него; уровни перебираются по порядку, берётся первый подошедший;
  • вход — закрытие свечи; стоп — за её фитилём плюс STOP_SPREAD (0.1%) цены;
  • цель — ближайший встречный уровень, какой есть; нет его — FALLBACK_RR (2) риска.

Чего здесь НЕТ по сравнению с правилами сентября (pattern_detector): силы отбоя,
свежего пересечения, пулов равных экстремумов, ожидания возврата до трёх часов, стопа
за парой свечей, порогов в долях ATR, минимальной цели и фильтров строгости.
Сентябрьский движок остаётся в pattern_detector нетронутым, константа возвращает его.

Функции — те же, что у pattern_detector, и с теми же сигнатурами (detect_spring,
detect_upthrust, explain): планировщик и /analyze выбирают МОДУЛЬ
(scheduler.spring_rules), а не ветку кода. Трекинг исхода общий —
pattern_detector.evaluate_signal.

Замер этих правил на истории BingX — «Замер 21 августа 2026 — ПЕРВЫЙ ДВИЖОК» в
CLAUDE.md: около 500 сигналов в месяц, винрейт 57%, брутто −0.057 R, нетто −0.254 R.
"""

import pandas as pd

import config
import pattern_detector

RULES = "june23"  # метка в разборе explain: по ней отчёт /analyze знает, чьи правила печатать


def detect_spring(df: pd.DataFrame, levels: list[dict], trend: str,
                  settings: dict | None = None) -> dict | None:
    """Лонг. settings принимается ради одинаковой сигнатуры с pattern_detector и не
    используется: фильтров строгости у движка 23 июня не было."""
    return _detect(df, levels, trend, side="long")


def detect_upthrust(df: pd.DataFrame, levels: list[dict], trend: str,
                    settings: dict | None = None) -> dict | None:
    """Шорт — зеркало Spring. settings не используется, как и у detect_spring."""
    return _detect(df, levels, trend, side="short")


def _avg_volume(df: pd.DataFrame, end_pos: int) -> float:
    """Средний объём VOL_LOOKBACK свечей перед свечой с индексом end_pos."""
    start = max(0, end_pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:end_pos]
    return float(window.mean()) if len(window) else 0.0


# Три помощника ниже общие для _detect и explain: разъедутся — и /analyze начнёт
# называть пробой, стоп или цель не так, как их считает движок.

def _broke_and_returned(side: str, h: float, l: float, c: float,
                        price: float) -> tuple[bool, bool]:
    """(проколола ли свеча уровень глубже BREAK_PCT его цены, закрылась ли обратно)."""
    if side == "long":
        return l < price * (1 - config.BREAK_PCT), c > price
    return h > price * (1 + config.BREAK_PCT), c < price


def _stop(side: str, h: float, l: float) -> float:
    """Стоп за фитилём свечи плюс STOP_SPREAD цены."""
    return l * (1 - config.STOP_SPREAD) if side == "long" else h * (1 + config.STOP_SPREAD)


def _target_level(levels: list[dict], side: str, c: float) -> float | None:
    """Ближайший встречный уровень, какой есть (без минимального расстояния)."""
    if side == "long":
        prices = [x["price"] for x in levels if x["type"] == "resistance" and x["price"] > c]
        return min(prices) if prices else None
    prices = [x["price"] for x in levels if x["type"] == "support" and x["price"] < c]
    return max(prices) if prices else None


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

    # Аномальный объём на свече пробоя.
    avg_vol = _avg_volume(df, pos)
    if avg_vol <= 0 or vol < avg_vol * config.VOL_MULT:
        return None

    level_type = "support" if side == "long" else "resistance"
    for lvl in levels:
        if lvl["type"] != level_type:
            continue
        broke, returned = _broke_and_returned(side, h, l, c, lvl["price"])
        if not (broke and returned):
            continue

        stop = _stop(side, h, l)
        target = _target_level(levels, side, c)
        if side == "long":
            tp = target if target is not None else c + (c - stop) * config.FALLBACK_RR
        else:
            tp = target if target is not None else c - (stop - c) * config.FALLBACK_RR
        return {
            "pattern": "spring" if side == "long" else "upthrust",
            "direction": side,
            "level_price": lvl["price"],
            "priority": "high" if lvl.get("strength") == "strong" else "normal",
            # Вход — закрытие свечи. Поле entry_price и signal_price совпадают: так
            # сигнал читается планировщиком и трекингом наравне с сентябрьским.
            "entry_price": c,
            "signal_price": c,
            "stop_loss": stop,
            "take_profit": tp,
            "bar_time": str(df.index[pos]),
            "sweep_bar_time": str(df.index[pos]),
        }
    return None


def explain(df: pd.DataFrame, levels: list[dict], trend: str,
            settings: dict | None = None) -> dict:
    """Разбор для /analyze по правилам 23 июня — в том же виде, что pattern_detector.explain.

    Общую часть отчёта (цена, ATR, объём, ближайшие уровни со склейкой) берём у
    сентябрьского разбора: это описание рынка, а не правила. Условия по сторонам —
    тренд, объём, пробой, цель — считаются здесь, теми же помощниками, что в _detect.
    """
    base = pattern_detector.explain(df, levels, trend,
                                    {"MAX_ENTRY_DIST_ATR": 0, "MAX_RISK_ATR": 0})
    if not base.get("enough_history"):
        return base

    h, l, c, atr = base["high"], base["low"], base["close"], base["atr"]
    vol_ok = base["avg_volume"] > 0 and base["volume"] >= base["avg_volume"] * config.VOL_MULT

    sides: dict[str, dict] = {}
    for side in ("long", "short"):
        blockers: list[str] = []
        trend_ok = not (side == "long" and trend == "down") and \
                   not (side == "short" and trend == "up")
        if not trend_ok:
            blockers.append("тренд дневки против сделки")
        if not vol_ok:
            blockers.append(f"объём {base['vol_ratio']:.1f}× среднего — порог ×{config.VOL_MULT:g}")

        level_type = "support" if side == "long" else "resistance"
        broken, closed_wrong = None, False
        for lvl in levels:
            if lvl["type"] != level_type:
                continue
            broke, returned = _broke_and_returned(side, h, l, c, lvl["price"])
            if not broke:
                continue
            if not returned:
                closed_wrong = True
                continue
            broken = {"price": lvl["price"], "strength": lvl.get("strength", "weak"),
                      "dist_atr": abs(c - lvl["price"]) / atr if atr > 0 else None}
            break
        break_note = None
        if broken is None:
            break_note = ("уровень проколот, но цена НЕ вернулась за него — пробой не ложный"
                          if closed_wrong else "свеча не заходила за уровень")
            blockers.append(break_note)

        # Профит/риск при входе прямо сейчас — справка, как и в сентябрьском отчёте.
        stop = _stop(side, h, l)
        risk = (c - stop) if side == "long" else (stop - c)
        target = _target_level(levels, side, c)
        rr = risk_atr = None
        if risk > 0 and atr > 0:
            risk_atr = risk / atr
            rr = abs(target - c) / risk if target is not None else config.FALLBACK_RR

        sides[side] = {
            "trend_ok": trend_ok,
            "vol_ok": vol_ok,
            "vol_ratio": base["vol_ratio"],
            "sweep_offset": 0,          # прокол и возврат — всегда одна свеча
            "broken_level": broken,
            "break_note": break_note,
            "risk": risk,
            "risk_atr": risk_atr,
            "target": target,
            "rr": rr,
            "blockers": blockers,
            "ready": not blockers,
        }

    return {**base, "rules": RULES, "pools": [],
            "filters": {"MAX_ENTRY_DIST_ATR": 0, "MAX_RISK_ATR": 0}, "sides": sides}
