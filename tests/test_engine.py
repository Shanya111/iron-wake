"""Юнит-тесты аналитики и детектора паттернов на синтетических свечах.

Сетевых запросов нет — данные строятся вручную, поэтому тесты быстрые и
детерминированные. Запуск без pytest:  python tests/test_engine.py
(или, если установлен pytest:  pytest tests/).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import analyzer  # noqa: E402
import config  # noqa: E402
import instruments
import pattern_detector  # noqa: E402


def _df(rows: list[tuple]) -> pd.DataFrame:
    """rows: список (open, high, low, close, volume). Индекс — почасовой UTC."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ── Тренд ────────────────────────────────────────────────────────────────────

def test_trend_up():
    rows = [(p, p + 1, p - 1, p, 100) for p in range(100, 140)]
    assert analyzer.get_trend(_df(rows)) == "up"


def test_trend_down():
    rows = [(p, p + 1, p - 1, p, 100) for p in range(140, 100, -1)]
    assert analyzer.get_trend(_df(rows)) == "down"


def test_trend_sideways():
    rows = [(100, 101, 99, 100, 100) for _ in range(40)]
    assert analyzer.get_trend(_df(rows)) == "sideways"


# ── Уровни ─────────────────────────────────────────────────────────────────

def test_find_levels_pivot():
    highs = [11, 12, 13, 20, 13, 12, 11]
    lows = [9, 8, 7, 6, 7, 8, 9]
    rows = [(highs[i], highs[i], lows[i], (highs[i] + lows[i]) / 2, 100) for i in range(7)]
    levels = analyzer.find_levels(_df(rows), window=2, timeframe="H1")
    found = {(round(l["price"]), l["type"]) for l in levels}
    assert (20, "resistance") in found
    assert (6, "support") in found


def test_prioritize_strong_weak():
    global_levels = [{"price": 100.0, "type": "support", "strength": "weak",
                      "is_liquidity": 0, "timeframe": "D1"}]
    local_levels = [
        {"price": 100.05, "type": "support", "strength": "weak", "is_liquidity": 0, "timeframe": "H1"},
        {"price": 105.0, "type": "support", "strength": "weak", "is_liquidity": 0, "timeframe": "H1"},
    ]
    result = analyzer.prioritize_levels(global_levels, local_levels, tol=0.001)
    by_price = {round(r["price"], 2): r["strength"] for r in result if r["timeframe"] == "H1"}
    assert by_price[100.05] == "strong"   # совпал с D1 в пределах 0.1%
    assert by_price[105.0] == "weak"      # далеко от D1


def test_find_liquidity_zones():
    rows = [(100, 101, 99, 100, 100) for _ in range(10)]
    rows[5] = (100, 101, 99, 100, 1000)   # явный всплеск объёма
    zones = analyzer.find_liquidity_zones(_df(rows), mult=1.5)
    assert len(zones) == 1
    assert abs(zones[0]["price"] - 100.0) < 1e-9


# ── Spring / Upthrust ────────────────────────────────────────────────────────

def _spring_df() -> pd.DataFrame:
    rows = [(100.5, 101.0, 100.2, 100.6, 100.0) for _ in range(25)]
    # Последняя ЗАКРЫТАЯ свеча (индекс -2 = 23): пробой поддержки 100 вниз,
    # закрытие обратно выше 100, объём 300 (всплеск над средним 100).
    rows[23] = (100.4, 100.7, 99.0, 100.5, 300.0)
    rows[24] = (100.5, 100.8, 100.3, 100.6, 50.0)  # текущая формирующаяся свеча
    return _df(rows)


def test_detect_spring():
    df = _spring_df()
    levels = [
        {"price": 100.0, "type": "support", "strength": "strong"},
        {"price": 110.0, "type": "resistance", "strength": "weak"},
    ]
    sig = pattern_detector.detect_spring(df, levels, trend="up")
    assert sig is not None
    assert sig["direction"] == "long"
    assert sig["priority"] == "high"               # пробитый уровень сильный
    # Закрытие свечи пробоя 100.5, пробитый уровень 100.0 → заявка на полпути
    # (config.ENTRY_PULLBACK = 0.5). signal_price — то самое закрытие.
    assert abs(sig["signal_price"] - 100.5) < 1e-9
    expected = 100.5 - config.ENTRY_PULLBACK * (100.5 - 100.0)
    assert abs(sig["entry_price"] - expected) < 1e-9
    assert abs(sig["take_profit"] - 110.0) < 1e-9
    assert sig["stop_loss"] < 99.0                 # стоп ниже минимума пробоя


def test_spring_filtered_by_downtrend():
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(df, levels, trend="down") is None


def test_spring_needs_abnormal_volume():
    df = _spring_df()
    df.iloc[23, df.columns.get_loc("volume")] = 100.0   # объём как у соседей — не Spring
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(df, levels, trend="up") is None


def test_spring_filtered_by_bad_rr():
    # Цель (ближайшее сопротивление 101.5) слишком близко: при стопе ниже 99 риск ~1.6,
    # а до цели всего ~1.0 → R:R < 1:2 → сигнал не берём.
    df = _spring_df()
    levels = [
        {"price": 100.0, "type": "support", "strength": "strong"},
        {"price": 101.5, "type": "resistance", "strength": "weak"},
    ]
    assert pattern_detector.detect_spring(df, levels, trend="up") is None


def test_spring_fallback_target_min_rr():
    # Сопротивления впереди нет → цель ставится ровно на MIN_RR × риск.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    sig = pattern_detector.detect_spring(df, levels, trend="up")
    assert sig is not None
    # Запасная цель считается ОТ ЗАКРЫТИЯ свечи, а не от цены заявки: так набор
    # сигналов не зависит от ENTRY_PULLBACK (см. пояснение в pattern_detector).
    risk = sig["signal_price"] - sig["stop_loss"]
    expected_tp = sig["signal_price"] + risk * config.MIN_RR
    assert abs(sig["take_profit"] - expected_tp) < 1e-9


def _upthrust_df() -> pd.DataFrame:
    rows = [(99.5, 99.8, 99.2, 99.5, 100.0) for _ in range(25)]
    # Пробой сопротивления 100 вверх, закрытие обратно ниже, всплеск объёма.
    rows[23] = (99.6, 101.0, 99.4, 99.5, 300.0)
    rows[24] = (99.5, 99.8, 99.3, 99.5, 50.0)
    return _df(rows)


def test_detect_upthrust():
    df = _upthrust_df()
    levels = [
        {"price": 100.0, "type": "resistance", "strength": "strong"},
        {"price": 90.0, "type": "support", "strength": "weak"},
    ]
    sig = pattern_detector.detect_upthrust(df, levels, trend="down")
    assert sig is not None
    assert sig["direction"] == "short"
    assert abs(sig["signal_price"] - 99.5) < 1e-9
    expected = 99.5 + config.ENTRY_PULLBACK * (100.0 - 99.5)
    assert abs(sig["entry_price"] - expected) < 1e-9
    assert abs(sig["take_profit"] - 90.0) < 1e-9
    assert sig["stop_loss"] > 101.0                # стоп выше максимума пробоя


def test_upthrust_filtered_by_uptrend():
    df = _upthrust_df()
    levels = [{"price": 100.0, "type": "resistance", "strength": "strong"}]
    assert pattern_detector.detect_upthrust(df, levels, trend="up") is None


# ── Трекинг исхода сигналов (evaluate_signal) ────────────────────────────────

def _long_signal(df) -> dict:
    """Лонг: стоп 99, цель 110, якорь — первая свеча df (следим за тем, что после)."""
    return {"direction": "long", "stop_loss": 99.0, "take_profit": 110.0,
            "bar_time": str(df.index[0])}


def test_evaluate_long_hit_tp():
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(5)]
    rows[3] = (105, 111, 104, 106, 100)        # high 111 ≥ цель 110
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "hit_tp"


def test_evaluate_long_hit_sl():
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(5)]
    rows[2] = (100, 100.5, 98, 99.5, 100)      # low 98 ≤ стоп 99
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "hit_sl"


def test_evaluate_conservative_stop_first():
    # Одна свеча накрыла и стоп, и цель → консервативно считаем стопом.
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(5)]
    rows[2] = (100, 111, 98, 100, 100)
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "hit_sl"


def test_evaluate_pending():
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(5)]  # ни цели, ни стопа
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "pending"


def test_evaluate_expired():
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(50)]  # 49ч ≥ 48ч порога
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "expired"


def test_evaluate_short_hit_tp():
    rows = [(99.5, 100, 99, 99.5, 100) for _ in range(5)]
    rows[3] = (95, 96, 89, 90, 100)            # low 89 ≤ цель 90
    df = _df(rows)
    sig = {"direction": "short", "stop_loss": 101.0, "take_profit": 90.0,
           "bar_time": str(df.index[0])}
    assert pattern_detector.evaluate_signal(sig, df) == "hit_tp"


def test_evaluate_no_bar_time_is_pending():
    # Старый сигнал без якоря не трогаем — исход не определить честно.
    rows = [(100.5, 101, 100, 100.5, 100) for _ in range(5)]
    rows[2] = (100, 111, 98, 100, 100)
    df = _df(rows)
    sig = {"direction": "long", "stop_loss": 99.0, "take_profit": 110.0, "bar_time": None}
    assert pattern_detector.evaluate_signal(sig, df) == "pending"


# ── Стакан (analyze_order_book) ──────────────────────────────────────────────

def test_orderbook_buyers_pressure():
    ob = {"bids": [[100.0, 10.0], [99.9, 10.0]], "asks": [[100.1, 1.0], [100.2, 1.0]]}
    s = analyzer.analyze_order_book(ob)
    assert s["pressure"] == "buyers"
    assert s["imbalance"] > 0


def test_orderbook_sellers_pressure():
    ob = {"bids": [[100.0, 1.0], [99.9, 1.0]], "asks": [[100.1, 10.0], [100.2, 10.0]]}
    s = analyzer.analyze_order_book(ob)
    assert s["pressure"] == "sellers"
    assert s["imbalance"] < 0


def test_orderbook_balance_and_spread():
    ob = {"bids": [[100.0, 5.0]], "asks": [[100.2, 5.0]]}
    s = analyzer.analyze_order_book(ob)
    assert s["pressure"] == "balance"
    assert abs(s["spread"] - 0.2) < 1e-9


def test_orderbook_walls():
    # Крупная заявка среди мелких → стена; ровные заявки → стены нет.
    ob = {
        "bids": [[100.0, 1.0], [99.9, 1.0], [99.8, 50.0], [99.7, 1.0]],
        "asks": [[100.1, 2.0], [100.2, 2.0]],
    }
    s = analyzer.analyze_order_book(ob, wall_mult=5)
    assert s["bid_wall"] is not None
    assert abs(s["bid_wall"]["price"] - 99.8) < 1e-9
    assert s["ask_wall"] is None


def test_orderbook_empty_is_none():
    assert analyzer.analyze_order_book({"bids": [], "asks": []}) is None


def test_orderbook_kraken_three_element_entries():
    # Kraken отдаёт заявки как [цена, объём, время] (3 элемента) — не должно падать.
    ob = {
        "bids": [[1.1430, 10.0, 1700000000], [1.1429, 10.0, 1700000000]],
        "asks": [[1.1432, 1.0, 1700000000], [1.1433, 1.0, 1700000000]],
    }
    s = analyzer.analyze_order_book(ob)
    assert s is not None
    assert s["pressure"] == "buyers"


# ── Лимитный вход (заявка вместо входа по рынку) ─────────────────────────────

def test_limit_price_fractions():
    # Лонг: закрытие 105, пробитый уровень 100. Половина пути → 102.5, весь → 100.
    assert pattern_detector.limit_price("long", 105, 100, 95, 0.0) == 105
    assert pattern_detector.limit_price("long", 105, 100, 95, 0.5) == 102.5
    assert pattern_detector.limit_price("long", 105, 100, 95, 1.0) == 100
    # Шорт — зеркально: закрытие 95, уровень 100.
    assert pattern_detector.limit_price("short", 95, 100, 105, 0.5) == 97.5


def test_limit_price_never_behind_stop():
    # Заявка за стопом оставила бы сделку без риска — падаем обратно на закрытие.
    assert pattern_detector.limit_price("long", 105, 100, 101, 1.0) == 105


def _limit_signal(entry=100.0, stop=98.0, tp=106.0, direction="long"):
    return {"direction": direction, "entry_price": entry, "stop_loss": stop,
            "take_profit": tp, "bar_time": "2024-01-01 00:00:00+00:00"}


def test_fill_waits_while_price_far():
    # Цена гуляет выше заявки и время ещё не вышло → ждём.
    df = _df([(105, 105.5, 104, 105, 10), (105, 105.5, 104, 105, 10)])
    res = pattern_detector.evaluate_fill(_limit_signal(), df, wait_bars=4)
    assert res["status"] == "waiting_fill"


def test_fill_happens_on_touch():
    df = _df([(105, 105.5, 104, 105, 10), (104, 105, 99, 103, 10)])
    res = pattern_detector.evaluate_fill(_limit_signal(), df, wait_bars=4)
    assert res["status"] == "filled"
    assert res["fill_time"].startswith("2024-01-01 01:00")
    assert res["stopped_at_fill"] is False


def test_fill_and_stop_on_same_candle():
    # Стоп лежит ДАЛЬШЕ заявки, поэтому порядок однозначен: вход, потом стоп.
    df = _df([(105, 105.5, 104, 105, 10), (104, 105, 97, 98, 10)])
    res = pattern_detector.evaluate_fill(_limit_signal(), df, wait_bars=4)
    assert res["status"] == "filled" and res["stopped_at_fill"] is True


def test_unfilled_when_price_runs_to_target():
    # Ушла к цели, не откатившись к заявке — сделки не было. Это и есть
    # адверс-селекция: лимитка теряет как раз сильные движения.
    df = _df([(105, 105.5, 104, 105, 10), (105, 107, 104, 106, 10)])
    res = pattern_detector.evaluate_fill(_limit_signal(), df, wait_bars=4)
    assert res["status"] == "expired_unfilled"


def test_unfilled_when_time_runs_out():
    df = _df([(105, 105.5, 104, 105, 10)] * 5)
    res = pattern_detector.evaluate_fill(_limit_signal(), df, wait_bars=4)
    assert res["status"] == "expired_unfilled"


def test_unfilled_when_breakout_older_than_window():
    # Бот лежал дольше окна свечей: первые свечи окна — уже не те, что шли за
    # пробоем. Судить по ним нельзя, заявка давно протухла — снимаем.
    df = _df([(105, 105.5, 99, 105, 10)] * 3)
    s = _limit_signal()
    s["bar_time"] = "2023-12-25 00:00:00+00:00"     # сильно раньше окна
    res = pattern_detector.evaluate_fill(s, df, wait_bars=4)
    assert res["status"] == "expired_unfilled"


def test_outcome_counted_from_fill_not_breakout():
    # До входа цена сходила к стопу, но нас там ещё не было: отсчёт идёт от
    # fill_time, поэтому этот провал в исход не попадает, а цель — попадает.
    df = _df([
        (105, 106, 97, 105, 10),    # 00:00 — «стоп» до входа, он не наш
        (105, 105, 99, 100, 10),    # 01:00 — заявка исполнилась
        (100, 107, 100, 106, 10),   # 02:00 — дошли до цели
    ])
    s = _limit_signal()
    s["fill_time"] = "2024-01-01 01:00:00+00:00"
    assert pattern_detector.evaluate_signal(s, df) == "hit_tp"


# ── ATR и фильтры строгости отбора (MAX_ENTRY_DIST_ATR / MAX_RISK_ATR) ────────

def test_atr_on_flat_candles():
    # Все свечи с размахом 2 и без гэпов → ATR ровно 2.
    rows = [(100, 101, 99, 100, 10) for _ in range(20)]
    assert abs(pattern_detector._atr(_df(rows), 19, period=14) - 2.0) < 1e-9


def test_atr_counts_gaps():
    # Гэп вверх: собственный размах свечи 1.0, но от прошлого закрытия (100) её
    # максимум ушёл на 5.5 — истинный диапазон берёт наибольшее из трёх, то есть 5.5.
    rows = [(100, 100.5, 99.5, 100, 10), (105, 105.5, 104.5, 105, 10)]
    assert abs(pattern_detector._atr(_df(rows), 1, period=1) - 5.5) < 1e-9


def test_entry_dist_filter_blocks_far_close():
    # Закрытие 100.5 при уровне 100.0 — это 0.58 ATR. Порог 0.05 ATR сигнал снимает.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"},
              {"price": 110.0, "type": "resistance", "strength": "weak"}]
    blocked = pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_ENTRY_DIST_ATR": 0.05})
    assert blocked is None
    # Тот же сетап с широким порогом проходит — значит режет именно фильтр.
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_ENTRY_DIST_ATR": 1.0}) is not None


def test_entry_dist_filter_tries_next_level():
    # Первый уровень в списке — дальний (его фильтр отбраковывает), второй — ближний.
    # Проверяем, что детектор идёт дальше по списку (continue), а не выходит (return).
    df = _spring_df()
    levels = [
        {"price": 99.4, "type": "support", "strength": "weak"},    # дальше: 1.1 от закрытия
        {"price": 100.0, "type": "support", "strength": "strong"},  # ближе: 0.5
        {"price": 110.0, "type": "resistance", "strength": "weak"},
    ]
    sig = pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_ENTRY_DIST_ATR": 0.7})
    assert sig is not None
    assert abs(sig["level_price"] - 100.0) < 1e-9   # взят ближний, дальний пропущен


def test_risk_filter_blocks_wide_candle():
    # Риск сделки на этой свече 1.85 ATR. Порог 0.5 ATR её снимает, 3.0 — пропускает.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"},
              {"price": 110.0, "type": "resistance", "strength": "weak"}]
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_RISK_ATR": 0.5}) is None
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_RISK_ATR": 3.0}) is not None


def test_filters_off_by_default():
    # Ноль = выключено: набор сигналов ровно тот же, что и без настроек вовсе.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"},
              {"price": 110.0, "type": "resistance", "strength": "weak"}]
    plain = pattern_detector.detect_spring(df, levels, trend="up")
    zeroed = pattern_detector.detect_spring(
        df, levels, trend="up", settings={"MAX_ENTRY_DIST_ATR": 0, "MAX_RISK_ATR": 0})
    assert plain is not None and zeroed is not None
    assert plain["entry_price"] == zeroed["entry_price"]


# ── Разбор для /analyze (explain) ────────────────────────────────────────────

_EX_LEVELS = [
    {"price": 100.0, "type": "support", "strength": "strong", "timeframe": "D1"},
    {"price": 110.0, "type": "resistance", "strength": "weak", "timeframe": "H1"},
]


def test_explain_sees_ready_signal():
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, trend="up")
    assert ex["enough_history"]
    assert ex["sides"]["long"]["ready"]
    assert ex["sides"]["long"]["blockers"] == []
    assert abs(ex["sides"]["long"]["broken_level"]["price"] - 100.0) < 1e-9
    assert ex["vol_ratio"] > config.VOL_MULT


def test_explain_names_missing_volume():
    df = _spring_df()
    df.iloc[23, df.columns.get_loc("volume")] = 100.0   # объём как у соседей
    ex = pattern_detector.explain(df, _EX_LEVELS, trend="up")
    assert not ex["sides"]["long"]["ready"]
    assert any("объём" in b for b in ex["sides"]["long"]["blockers"])


def test_explain_names_trend_block():
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, trend="down")
    assert not ex["sides"]["long"]["trend_ok"]
    assert any("тренд" in b for b in ex["sides"]["long"]["blockers"])


def test_explain_names_active_filter():
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, trend="up",
                                  settings={"MAX_ENTRY_DIST_ATR": 0.05})
    assert not ex["sides"]["long"]["ready"]
    assert any("вход у уровня" in b for b in ex["sides"]["long"]["blockers"])


def test_explain_agrees_with_detector():
    # Главное свойство разбора: он не должен расходиться с самим детектором.
    # Гоняем оба на одних данных при разных фильтрах и сверяем вердикт.
    df = _spring_df()
    for settings in ({}, {"MAX_ENTRY_DIST_ATR": 0.05}, {"MAX_RISK_ATR": 0.5},
                     {"MAX_ENTRY_DIST_ATR": 1.0, "MAX_RISK_ATR": 3.0}):
        ex = pattern_detector.explain(df, _EX_LEVELS, "up", settings)
        sig = pattern_detector.detect_spring(df, _EX_LEVELS, "up", settings)
        assert ex["sides"]["long"]["ready"] == (sig is not None), settings


def test_explain_measures_distance_in_atr():
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, trend="up")
    support = ex["supports"][0]
    assert abs(support["price"] - 100.0) < 1e-9
    # Расстояние в ATR — это то же расстояние, делённое на ATR, без магии.
    assert abs(support["dist_atr"] - support["dist"] / ex["atr"]) < 1e-9


def test_explain_short_history_is_flagged():
    rows = [(100, 101, 99, 100, 10) for _ in range(5)]
    assert pattern_detector.explain(_df(rows), _EX_LEVELS, "up")["enough_history"] is False


def test_explain_merges_close_levels():
    # Фрактал на H1 отмечает одну вершину тремя соседними барами. В отчёте это должен
    # быть ОДИН уровень, иначе печатается «1.3520, 1.3520, 1.3520» — так было у TON.
    levels = [
        {"price": 110.00, "type": "resistance", "strength": "weak", "timeframe": "H1"},
        {"price": 110.05, "type": "resistance", "strength": "strong", "timeframe": "D1"},
        {"price": 110.10, "type": "resistance", "strength": "weak", "timeframe": "H1"},
        {"price": 120.00, "type": "resistance", "strength": "weak", "timeframe": "H1"},
    ]
    ex = pattern_detector.explain(_spring_df(), levels, trend="up")
    prices = [round(r["price"], 2) for r in ex["resistances"]]
    assert prices == [110.0, 120.0]
    # При склейке сильный побеждает: среди слитых был strong.
    assert ex["resistances"][0]["strength"] == "strong"


def test_explain_break_note_is_about_breakout_only():
    # Фильтр «вдогонку» — это НЕ причина по пробою. Раньше отчёт брал первый блокер
    # подряд и печатал текст фильтра в пункте «Ложный пробой».
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, trend="up",
                                  settings={"MAX_RISK_ATR": 0.5})
    assert "вдогонку" not in (ex["sides"]["long"]["break_note"] or "")
    # Сам пробой при этом состоялся — фильтр снял сигнал, но не отменил прокол.
    assert ex["sides"]["long"]["broken_level"] is not None
    assert ex["sides"]["long"]["break_note"] is None


# ── Валютные пары: свои пороги детектора (форекс на фьючерсах BingX) ─────────
#
# Смысл блока: у форекса глубина пробоя и запас стопа заданы в долях ATR, а не в
# процентах цены, и ATR-фильтры строгости из /settings к нему не применяются.
# Данные подобраны так, чтобы ОТЛИЧИТЬ одно от другого: прокол здесь мельче, чем
# требует BREAK_PCT (0.05% от 1.1 = 0.00055), но крупнее, чем требует FX_BREAK_ATR
# (0.02 × ATR 0.0010 = 0.00002). Значит для валютной пары сигнал есть, а для
# крипты на тех же свечах — нет.

_FX_LEVELS = [
    {"price": 1.10000, "type": "support", "strength": "strong", "timeframe": "D1"},
    {"price": 1.11000, "type": "resistance", "strength": "weak", "timeframe": "H1"},
]


def _fx_df() -> pd.DataFrame:
    """Свечи «как у EUR/USD»: цена ~1.1, размах свечи 0.0010 → ATR(14) = 0.0010."""
    rows = [(1.10050, 1.10100, 1.10000, 1.10050, 100.0) for _ in range(25)]
    # Свеча пробоя (индекс 23 = последняя ЗАКРЫТАЯ): прокол уровня 1.10000 на
    # 0.00020 вниз и закрытие обратно выше, объём втрое выше среднего.
    rows[23] = (1.10040, 1.10070, 1.09980, 1.10050, 300.0)
    rows[24] = (1.10050, 1.10080, 1.10030, 1.10060, 50.0)   # текущая, не закрыта
    return _df(rows)


def test_fx_registry_has_engine_source():
    # Все пять валютных пар входят в движок и помечены флагом fx.
    for code in ("EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDJPY"):
        assert instruments.is_fx(code), code
        assert instruments.data_source(code) == "ccxt", code
        assert code in instruments.engine_codes(), code
    # Крипта и товары флагом не помечены — их пороги не меняются.
    for code in ("BTC", "ETH", "GOLD", "BRENT"):
        assert not instruments.is_fx(code), code
    assert not instruments.is_fx(None)
    assert not instruments.is_fx("EURJPY=X")      # своя пара — не форекс реестра


def test_fx_break_depth_in_atr_gives_signal():
    # Тот же самый набор свечей: валютная пара сигналит, крипта молчит.
    df = _fx_df()
    fx_sig = pattern_detector.detect_spring(df, _FX_LEVELS, "up", None, "EURUSD")
    assert fx_sig is not None, "форекс должен ловить мелкий прокол (порог в ATR)"
    crypto_sig = pattern_detector.detect_spring(df, _FX_LEVELS, "up", None, "BTC")
    assert crypto_sig is None, "у крипты порог в % цены — этот прокол мелкий"
    # Без кода инструмента ведём себя как раньше (крипта) — обратная совместимость.
    assert pattern_detector.detect_spring(df, _FX_LEVELS, "up") is None


def test_fx_stop_buffer_in_atr():
    df = _fx_df()
    sig = pattern_detector.detect_spring(df, _FX_LEVELS, "up", None, "EURUSD")
    # ATR берём у самого детектора, а не круглым числом: тест обязан проверять
    # ФОРМУЛУ запаса стопа, а не совпадение с приближённой константой.
    atr = pattern_detector._atr(df, len(df) - 2)
    assert 0.0009 < atr < 0.0011                   # по построению _fx_df ≈ размах свечи
    expected = 1.09980 - atr * config.FX_STOP_ATR  # минимум пробоя минус доля ATR
    assert abs(sig["stop_loss"] - expected) < 1e-9
    # Процентный запас дал бы стоп ВЫШЕ (0.1% от 1.0998 = 0.0011 против 0.00025),
    # то есть риск сделки был бы вчетверо больше — ровно тот блокиратор, из-за
    # которого форекс молчал.
    assert sig["stop_loss"] > 1.09980 * (1 - config.STOP_SPREAD)


def test_fx_ignores_strictness_filters():
    # Оба ATR-фильтра выкручены до заведомо непроходимых значений.
    strict = {"MAX_ENTRY_DIST_ATR": 0.01, "MAX_RISK_ATR": 0.05}
    df = _fx_df()
    assert pattern_detector.detect_spring(df, _FX_LEVELS, "up", strict, "EURUSD") is not None
    # А на крипте те же фильтры продолжают работать как работали.
    crypto_df = _spring_df()
    crypto_levels = [{"price": 100.0, "type": "support", "strength": "strong"},
                     {"price": 110.0, "type": "resistance", "strength": "weak"}]
    assert pattern_detector.detect_spring(crypto_df, crypto_levels, "up") is not None
    assert pattern_detector.detect_spring(crypto_df, crypto_levels, "up", strict, "BTC") is None


def test_fx_explain_agrees_with_detector():
    # То же требование, что и для крипты: разбор не должен расходиться с детектором.
    df = _fx_df()
    for settings in ({}, {"MAX_ENTRY_DIST_ATR": 0.01}, {"MAX_RISK_ATR": 0.05},
                     {"MAX_ENTRY_DIST_ATR": 1.0, "MAX_RISK_ATR": 3.0}):
        ex = pattern_detector.explain(df, _FX_LEVELS, "up", settings, "EURUSD")
        sig = pattern_detector.detect_spring(df, _FX_LEVELS, "up", settings, "EURUSD")
        assert ex["sides"]["long"]["ready"] == (sig is not None), settings
        assert ex["fx"] is True
        # Фильтры в отчёте показаны обнулёнными — /analyze не должен обещать
        # пользователю отбор, которого для этой пары не происходит.
        assert ex["filters"]["MAX_RISK_ATR"] == 0
        assert ex["filters"]["MAX_ENTRY_DIST_ATR"] == 0


def test_fx_explain_marks_crypto_as_not_fx():
    ex = pattern_detector.explain(_spring_df(), _EX_LEVELS, "up", None, "BTC")
    assert ex["fx"] is False


def test_fx_without_atr_stays_silent():
    # Плоские свечи → ATR = 0. Пороги форекса посчитать не на чем; детектор обязан
    # промолчать, а НЕ откатиться на проценты цены (это была бы тихая подмена).
    rows = [(1.10000, 1.10000, 1.10000, 1.10000, 100.0) for _ in range(25)]
    rows[23] = (1.10000, 1.10000, 1.10000, 1.10000, 300.0)
    assert pattern_detector.detect_spring(_df(rows), _FX_LEVELS, "up", None, "EURUSD") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"OK  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} тестов прошли")
