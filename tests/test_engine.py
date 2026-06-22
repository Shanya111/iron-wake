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
    assert abs(sig["entry_price"] - 100.5) < 1e-9
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
    assert abs(sig["entry_price"] - 99.5) < 1e-9
    assert abs(sig["take_profit"] - 90.0) < 1e-9
    assert sig["stop_loss"] > 101.0                # стоп выше максимума пробоя


def test_upthrust_filtered_by_uptrend():
    df = _upthrust_df()
    levels = [{"price": 100.0, "type": "resistance", "strength": "strong"}]
    assert pattern_detector.detect_upthrust(df, levels, trend="up") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"OK  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} тестов прошли")
