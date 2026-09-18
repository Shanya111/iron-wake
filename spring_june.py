"""Ложный пробой в редакции 23 июня 2026 — детектор первого движка (стратегия №2).

Правила — ровно детектор коммита c855660 (22 июня); в коммите 5982fa4 (23 июня) он не
менялся. Вернул их в бой владелец 15 сентября 2026 (config.SPRING_ENGINE = "june23"):

  • решение — по последней ЗАКРЫТОЙ часовой свече (df.iloc[-2]);
  • направление: лонг не берётся в нисходящем, шорт — в восходящем, в боковике обе.
    Тренд с 16.09.2026 считается по 6-часовым свечам (analyzer.engine_trend), а не по
    дневным, как было в июне;
  • объём этой свечи ≥ среднего за VOL_LOOKBACK предыдущих × VOL_MULT (1.5). Окно
    среднего с 17.09.2026 — 12 часов вместо 20 (на качество не влияет, см. config);
  • свеча проколола уровень глубже BREAK_PCT (0.05%) цены уровня и закрылась обратно
    за него. С 17.09.2026 уровень обязан быть СИЛЬНЫМ (config.JUNE_STRONG_ONLY), а из
    проколотых берётся САМЫЙ ГЛУБОКИЙ; в июне брался первый подошедший по списку;
  • вход — закрытие свечи; стоп — за её фитилём плюс JUNE_STOP_ATR (1) ATR. В июне
    запас был долей цены (STOP_SPREAD, 0.1%), он остался запасной меркой без ATR;
  • цель — ближайший встречный уровень не ближе JUNE_MIN_TP_R своего класса
    (крипта 1 риск, валюта/золото/нефть 0.5); нет такого — FALLBACK_RR (2) риска.

Чего здесь НЕТ по сравнению с правилами сентября (pattern_detector): силы отбоя,
свежего пересечения, пулов равных экстремумов, ожидания возврата до трёх часов, стопа
за парой свечей, порогов в долях ATR и фильтров строгости. Минимальная цель, наоборот,
появилась и здесь — своя, в долях старого риска.
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
    """Лонг. Из settings читается ровно один ключ — MIN_TP_R (минимальная цель в долях
    старого риска, кладёт вызывающий по классу инструмента). Фильтров строгости у
    движка 23 июня не было и нет: MAX_ENTRY_DIST_ATR и MAX_RISK_ATR тут не смотрят."""
    return _detect(df, levels, trend, side="long", settings=settings)


def detect_upthrust(df: pd.DataFrame, levels: list[dict], trend: str,
                    settings: dict | None = None) -> dict | None:
    """Шорт — зеркало Spring. Из settings читается тот же MIN_TP_R, что у detect_spring."""
    return _detect(df, levels, trend, side="short", settings=settings)


def _avg_volume(df: pd.DataFrame, end_pos: int) -> float:
    """Средний объём VOL_LOOKBACK свечей перед свечой с индексом end_pos."""
    start = max(0, end_pos - config.VOL_LOOKBACK)
    window = df["volume"].iloc[start:end_pos]
    return float(window.mean()) if len(window) else 0.0


# Помощники ниже общие для _detect и explain: разъедутся — и /analyze начнёт
# называть пробой, уровень, стоп или цель не так, как их считает движок.

def _broke_and_returned(side: str, h: float, l: float, c: float,
                        price: float) -> tuple[bool, bool]:
    """(проколола ли свеча уровень глубже BREAK_PCT его цены, закрылась ли обратно)."""
    if side == "long":
        return l < price * (1 - config.BREAK_PCT), c > price
    return h > price * (1 + config.BREAK_PCT), c < price


def _stop(side: str, h: float, l: float, atr: float) -> float:
    """Стоп за фитилём свечи плюс JUNE_STOP_ATR × ATR (с 17 сентября 2026).

    До этого дня запас считался долей цены (STOP_SPREAD, 0.1%). Замер и цена решения —
    в комментарии к config.JUNE_STOP_ATR. Без ATR (плоские свечи) откатываемся на
    прежнюю мерку: движок не должен замолкать из-за того, что волатильность нулевая.
    """
    if atr and atr > 0:
        return l - config.JUNE_STOP_ATR * atr if side == "long" else h + config.JUNE_STOP_ATR * atr
    return l * (1 - config.STOP_SPREAD) if side == "long" else h * (1 + config.STOP_SPREAD)


def _old_risk(side: str, h: float, l: float, c: float) -> float:
    """Риск ПРЕЖНЕГО движка: закрытие → фитиль плюс STOP_SPREAD цены.

    Живёт отдельно от реального стопа нарочно. Минимальная цель меряется в долях
    именно этого риска (решение владельца 17.09.2026): иначе, расширив стоп до 1 ATR,
    мы бы заодно отодвинули и цель — вышло бы два рычага вместо одного.
    """
    stop = l * (1 - config.STOP_SPREAD) if side == "long" else h * (1 + config.STOP_SPREAD)
    return abs(c - stop)


def _broken_levels(levels: list[dict], side: str, h: float, l: float,
                   c: float) -> list[dict]:
    """Уровни нужного типа, которые свеча проколола и закрылась обратно за них."""
    level_type = "support" if side == "long" else "resistance"
    return [lvl for lvl in levels if lvl["type"] == level_type
            and all(_broke_and_returned(side, h, l, c, lvl["price"]))]


def _pick_level(broken: list[dict], side: str) -> dict | None:
    """Уровень, от которого берётся сигнал: САМЫЙ ГЛУБОКИЙ из проколотых.

    При config.JUNE_STRONG_ONLY среди проколотых сначала остаются только СИЛЬНЫЕ
    (часовой уровень, совпавший с дневным, либо сам дневной) — нет таких, сигнала
    нет вовсе. Из оставшихся берётся самый глубокий вынос: для лонга нижняя
    поддержка, для шорта верхнее сопротивление.

    До 17 сентября 2026 брался ПЕРВЫЙ подошедший по порядку списка. Сам по себе
    выбор уровня ни одной сделки не менял (стоп стоит за фитилём свечи, цель
    ищется от закрытия), но с отбором по силе он стал важен: сильный уровень надо
    искать среди ВСЕХ проколотых, а не только среди первого.
    """
    if config.JUNE_STRONG_ONLY:
        broken = [lvl for lvl in broken if lvl.get("strength") == "strong"]
    if not broken:
        return None
    return (min(broken, key=lambda x: x["price"]) if side == "long"
            else max(broken, key=lambda x: x["price"]))


def _target_level(levels: list[dict], side: str, c: float,
                  min_gap: float = 0.0) -> float | None:
    """Ближайший встречный уровень НЕ БЛИЖЕ min_gap от закрытия.

    min_gap — доля старого риска (config.JUNE_MIN_TP_R по классу инструмента). Уровни
    ближе пропускаются, цель встаёт на следующий за ними; сигнал при этом остаётся —
    двигается только цель, число сигналов не меняется.
    """
    if side == "long":
        prices = [x["price"] for x in levels
                  if x["type"] == "resistance" and x["price"] > c and x["price"] - c >= min_gap]
        return min(prices) if prices else None
    prices = [x["price"] for x in levels
              if x["type"] == "support" and x["price"] < c and c - x["price"] >= min_gap]
    return max(prices) if prices else None


def _min_gap(settings: dict | None, side: str, h: float, l: float, c: float) -> float:
    """Минимальное расстояние до цели в ЦЕНЕ.

    MIN_TP_R кладёт вызывающий по классу инструмента (instruments.asset_class →
    config.JUNE_MIN_TP_R): у крипты 1 риск, у валюты, золота и нефти 0.5.
    Общий помощник для _detect и explain — разъедутся, и /analyze назовёт целью
    уровень, который движок целью не считает.
    """
    k = float((settings or {}).get("MIN_TP_R", 0.0) or 0.0)
    return k * _old_risk(side, h, l, c) if k > 0 else 0.0


def _detect(df: pd.DataFrame, levels: list[dict], trend: str, side: str,
            settings: dict | None = None) -> dict | None:
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

    lvl = _pick_level(_broken_levels(levels, side, h, l, c), side)
    if lvl is not None:
        stop = _stop(side, h, l, pattern_detector._atr(df, pos))
        target = _target_level(levels, side, c, _min_gap(settings, side, h, l, c))
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
        # Прокол был, но свеча осталась за уровнем — это не ложный пробой, а обычный.
        closed_wrong = any(
            _broke_and_returned(side, h, l, c, lvl["price"]) == (True, False)
            for lvl in levels if lvl["type"] == level_type
        )
        all_broken = _broken_levels(levels, side, h, l, c)
        picked = _pick_level(all_broken, side)
        broken = None if picked is None else {
            "price": picked["price"], "strength": picked.get("strength", "weak"),
            "dist_atr": abs(c - picked["price"]) / atr if atr > 0 else None,
        }
        break_note = None
        if broken is None:
            if all_broken:
                # Проколотые уровни есть, но все слабые — именно это движок и
                # отбраковывает с 17 сентября 2026 (config.JUNE_STRONG_ONLY).
                weakest = (min(all_broken, key=lambda x: x["price"]) if side == "long"
                           else max(all_broken, key=lambda x: x["price"]))
                break_note = (f"уровень {weakest['price']:g} проколот и выкуплен, но он "
                              f"СЛАБЫЙ — движок берёт только сильные (совпавшие с дневным)")
            elif closed_wrong:
                break_note = "уровень проколот, но цена НЕ вернулась за него — пробой не ложный"
            else:
                break_note = "свеча не заходила за уровень"
            blockers.append(break_note)

        # Профит/риск при входе прямо сейчас — справка, как и в сентябрьском отчёте.
        stop = _stop(side, h, l, atr)
        risk = (c - stop) if side == "long" else (stop - c)
        target = _target_level(levels, side, c, _min_gap(settings, side, h, l, c))
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

    # min_tp_r отдаём наружу, чтобы отчёт /analyze печатал ТО правило цели, по которому
    # разбор и посчитан: у крипты, валюты и товаров оно разное.
    return {**base, "rules": RULES, "pools": [],
            "min_tp_r": float((settings or {}).get("MIN_TP_R", 0.0) or 0.0),
            "filters": {"MAX_ENTRY_DIST_ATR": 0, "MAX_RISK_ATR": 0}, "sides": sides}
