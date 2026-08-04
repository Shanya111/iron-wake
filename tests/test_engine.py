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
import pattern_detector  # noqa: E402


def _df(rows: list[tuple]) -> pd.DataFrame:
    """rows: список (open, high, low, close, volume). Индекс — почасовой UTC.

    Дата начала выбрана не случайно: 2024-01-01 — понедельник, поэтому свечи
    попадают в недельное окно торговли (входы только пн–чт, см. trading_week).
    Сдвинешь дату на пятницу/выходные — детектор перестанет выдавать сигналы и
    тесты паттернов упадут. Проверка окна — в tests/test_trading_week.py.
    """
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

# Синтетические свечи пробоя нарочно с большим фитилём — так паттерн однозначен.
# В бою такой вход отсекает фильтр цены входа (риск ≤ MAX_RISK_ATR×ATR, см. config):
# он бракует именно аномально размашистые свечи. Тесты ниже проверяют ДРУГУЮ логику
# (тренд, объём, R:R, стоп), поэтому фильтр им отключаем — у него свои тесты.
NO_RISK_FILTER = {"MAX_RISK_ATR": None}

# Фикстуры ниже проходят и боевой фильтр «поглощение» (config.MAX_BODY_RATIO=0.15):
# у свечи пробоя тело 0.1 при размахе 0.8–1.7, то есть 0.06–0.13 размаха. Это не
# случайность — так выглядит пружина по VSA. Если будешь добавлять фикстуру, где
# свеча пробоя закрывается далеко от открытия, детектор её забракует, и тест упадёт
# не по той причине, которую проверяет: тогда фильтр надо отключить явно
# ({"MAX_BODY_RATIO": None}), как это сделано для фильтра цены входа.


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
    sig = pattern_detector.detect_spring(df, levels, trend="up", settings=NO_RISK_FILTER)
    assert sig is not None
    assert sig["direction"] == "long"
    assert sig["priority"] == "high"               # пробитый уровень сильный
    assert abs(sig["entry_price"] - 100.5) < 1e-9
    assert abs(sig["take_profit"] - 110.0) < 1e-9
    assert sig["stop_loss"] < 99.0                 # стоп ниже минимума пробоя


def test_spring_filtered_by_downtrend():
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(df, levels, trend="down", settings=NO_RISK_FILTER) is None


def test_spring_needs_abnormal_volume():
    df = _spring_df()
    df.iloc[23, df.columns.get_loc("volume")] = 100.0   # объём как у соседей — не Spring
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(df, levels, trend="up", settings=NO_RISK_FILTER) is None


# ── Фильтр цены входа (риск ≤ MAX_RISK_ATR × ATR) ───────────────────────────

def _tight_spring_df() -> pd.DataFrame:
    """Аккуратная пружина: свеча пробоя не крупнее соседних, вход рядом с уровнем.
    Риск (закрытие → минимум + запас) ≈ 0.70 при ATR ≈ 0.80, то есть внутри 1×ATR."""
    rows = [(100.5, 101.0, 100.2, 100.6, 100.0) for _ in range(25)]
    rows[23] = (100.4, 100.7, 99.9, 100.5, 300.0)   # сходили под 100 и вернулись
    rows[24] = (100.5, 100.8, 100.3, 100.6, 50.0)   # формирующаяся
    return _df(rows)


_SUPPORT_AND_TARGET = [
    {"price": 100.0, "type": "support", "strength": "strong"},
    {"price": 110.0, "type": "resistance", "strength": "weak"},
]


def test_entry_filter_allows_tight_spring():
    """Вход рядом с уровнем проходит фильтр — это и есть рабочий сетап."""
    sig = pattern_detector.detect_spring(_tight_spring_df(), _SUPPORT_AND_TARGET, trend="up")
    assert sig is not None
    risk = sig["entry_price"] - sig["stop_loss"]
    atr = pattern_detector._atr(_tight_spring_df(), 23, config.STOP_ATR_PERIOD)
    assert risk <= atr * config.MAX_RISK_ATR


def test_entry_filter_rejects_oversized_break_candle():
    """Свеча пробоя вдвое размашистее соседних → вход далеко от уровня, бракуем.
    Именно эти сделки убыточны по бэктесту (см. config.MAX_RISK_ATR)."""
    df = _spring_df()          # у него риск ≈ 1.85×ATR
    atr = pattern_detector._atr(df, 23, config.STOP_ATR_PERIOD)
    risk = 100.5 - 99.0 + 99.0 * config.STOP_SPREAD
    assert risk > atr * config.MAX_RISK_ATR, "фикстура должна быть за порогом"
    assert pattern_detector.detect_spring(df, _SUPPORT_AND_TARGET, trend="up") is None


def test_entry_filter_can_be_disabled():
    """MAX_RISK_ATR=None снимает фильтр — нужно бэктесту (--no-risk-filter) и тестам
    другой логики, которые пользуются нарочно размашистыми фикстурами."""
    df = _spring_df()
    assert pattern_detector.detect_spring(
        df, _SUPPORT_AND_TARGET, trend="up", settings={"MAX_RISK_ATR": None}) is not None


def test_entry_filter_mirrors_for_upthrust():
    """Шорт считает риск вверх от закрытия до максимума — зеркально лонгу."""
    rows = [(99.5, 99.8, 99.0, 99.4, 100.0) for _ in range(25)]
    rows[23] = (99.6, 102.0, 99.5, 99.5, 300.0)    # огромный вынос вверх → вход далеко
    rows[24] = (99.5, 99.7, 99.2, 99.4, 50.0)
    levels = [{"price": 100.0, "type": "resistance", "strength": "strong"},
              {"price": 90.0, "type": "support", "strength": "weak"}]
    assert pattern_detector.detect_upthrust(_df(rows), levels, trend="down") is None
    assert pattern_detector.detect_upthrust(
        _df(rows), levels, trend="down", settings={"MAX_RISK_ATR": None}) is not None


def test_spring_filtered_by_bad_rr():
    # Цель (ближайшее сопротивление 101.5) слишком близко: при стопе ниже 99 риск ~1.6,
    # а до цели всего ~1.0 → R:R < 1:2 → сигнал не берём.
    df = _spring_df()
    levels = [
        {"price": 100.0, "type": "support", "strength": "strong"},
        {"price": 101.5, "type": "resistance", "strength": "weak"},
    ]
    assert pattern_detector.detect_spring(df, levels, trend="up", settings=NO_RISK_FILTER) is None


def test_spring_fallback_target_min_rr():
    # Сопротивления впереди нет → цель ставится ровно на MIN_RR × риск.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    sig = pattern_detector.detect_spring(df, levels, trend="up", settings=NO_RISK_FILTER)
    assert sig is not None
    risk = sig["entry_price"] - sig["stop_loss"]
    expected_tp = sig["entry_price"] + risk * config.get("MIN_RR")
    assert abs(sig["take_profit"] - expected_tp) < 1e-9


# ── Пресеты /settings (частота сигналов, вход вдогонку) ─────────────────────

def test_sensitivity_presets_round_trip():
    """Каждый пресет узнаётся обратно по действующим порогам — иначе меню
    показывало бы «средне» на любом положении кнопок."""
    for name, (_, values) in config.SENSITIVITY.items():
        assert config.sensitivity_of(config.effective(values)) == name


def test_sensitivity_presets_stay_in_measured_range():
    """Кнопки двигают порог объёма только внутри диапазона, который прошёл
    проверку на устойчивость (P60–P80). За его границами замер разваливается."""
    for _, values in config.SENSITIVITY.values():
        assert 60 <= values["VOL_PCTL"] <= 80


def test_sensitivity_unknown_combo_falls_back_to_mid():
    assert config.sensitivity_of({"VOL_PCTL": 42, "BREAK_PCT": 0.0005}) == "mid"


def test_chase_filter_toggles_off_with_zero():
    """«Не входить вдогонку: выкл» пишет 0 — в БД нельзя положить None."""
    df = _spring_df()          # у него риск ≈ 1.85×ATR, это вход вдогонку
    assert pattern_detector.detect_spring(
        df, _SUPPORT_AND_TARGET, trend="up", settings=config.effective()) is None
    assert pattern_detector.detect_spring(
        df, _SUPPORT_AND_TARGET, trend="up",
        settings=config.effective({"MAX_RISK_ATR": 0})) is not None


# ── Фильтр «поглощение» (тело свечи пробоя мало относительно размаха) ───────

def _body_df(open_price: float) -> pd.DataFrame:
    """Пружина с размахом 1.7 (99.0…100.7); тело задаётся ценой открытия."""
    rows = [(100.5, 101.0, 100.2, 100.6, 100.0) for _ in range(25)]
    rows[23] = (open_price, 100.7, 99.0, 100.5, 300.0)
    rows[24] = (100.5, 100.8, 100.3, 100.6, 50.0)
    return _df(rows)


_BODY_LEVELS = [{"price": 100.0, "type": "support", "strength": "strong"},
                {"price": 110.0, "type": "resistance", "strength": "weak"}]


def test_absorption_passes_small_body():
    # open 100.4, close 100.5 → тело 0.1 при размахе 1.7 ≈ 0.06 размаха.
    sig = pattern_detector.detect_spring(
        _body_df(100.4), _BODY_LEVELS, trend="up",
        settings={"MAX_BODY_RATIO": 0.3, **NO_RISK_FILTER})
    assert sig is not None


def test_absorption_rejects_large_body():
    # open 99.2, close 100.5 → тело 1.3 при размахе 1.7 ≈ 0.76 размаха.
    sig = pattern_detector.detect_spring(
        _body_df(99.2), _BODY_LEVELS, trend="up",
        settings={"MAX_BODY_RATIO": 0.3, **NO_RISK_FILTER})
    assert sig is None


def test_absorption_off_by_default():
    """None отключает фильтр — свеча с большим телом проходит."""
    sig = pattern_detector.detect_spring(
        _body_df(99.2), _BODY_LEVELS, trend="up",
        settings={"MAX_BODY_RATIO": None, **NO_RISK_FILTER})
    assert sig is not None


# ── Зоны ликвидности как уровни для пробоя ──────────────────────────────────

def _zone_df() -> pd.DataFrame:
    """Одна объёмная свеча (размах 99…101) среди спокойных — будущая зона."""
    rows = [(100.0, 100.3, 99.8, 100.1, 100.0) for _ in range(10)]
    rows[4] = (100.0, 101.0, 99.0, 100.5, 900.0)
    return _df(rows)


def test_liquidity_levels_mid_uses_candle_middle():
    zones = analyzer.find_liquidity_zones(_zone_df())
    levels = analyzer.liquidity_levels(zones, "mid")
    assert {l["price"] for l in levels} == {100.0}          # (101 + 99) / 2
    assert {l["type"] for l in levels} == {"support", "resistance"}


def test_liquidity_levels_edge_uses_candle_bounds():
    zones = analyzer.find_liquidity_zones(_zone_df())
    levels = analyzer.liquidity_levels(zones, "edge")
    by_type = {l["type"]: l["price"] for l in levels}
    assert by_type["support"] == 99.0        # низ объёмной свечи
    assert by_type["resistance"] == 101.0    # её верх


def test_liquidity_levels_are_weak():
    """Сигнал по зоне — обычного приоритета: ⭐ остаётся уровням с подтверждением D1."""
    levels = analyzer.liquidity_levels(analyzer.find_liquidity_zones(_zone_df()), "mid")
    assert all(l["strength"] == "weak" and l["is_liquidity"] == 1 for l in levels)


def test_liquidity_zone_becomes_tradable_level():
    """Главное, ради чего всё затевалось: по зоне детектор ловит пробой.

    Уровней-пивотов тут нет вовсе — ровно та ситуация, когда /analyze пишет
    «сильных часовых уровней нет, ориентируйся на зоны ликвидности».
    """
    rows = [(100.5, 101.0, 100.2, 100.6, 100.0) for _ in range(25)]
    rows[5] = (100.4, 100.6, 99.9, 100.5, 900.0)   # объёмная свеча → зона на 100.25
    rows[23] = (100.4, 100.7, 99.9, 100.5, 300.0)  # пробой зоны вниз и возврат
    rows[24] = (100.5, 100.8, 100.3, 100.6, 50.0)
    df = _df(rows)
    zones = analyzer.find_liquidity_zones(df.iloc[:-config.LIQ_SKIP_LAST])
    levels = analyzer.liquidity_levels(zones, "mid")
    assert levels, "объёмная свеча должна дать зону"
    sig = pattern_detector.detect_spring(df, levels, trend="up", settings=NO_RISK_FILTER)
    assert sig is not None
    assert sig["priority"] == "normal"      # зона не даёт ⭐


def test_liquidity_zone_cannot_break_itself():
    """Ловушка самоссылки: свеча пробоя объёмная по построению, и её собственная
    середина не должна становиться уровнем, который она же «пробивает».

    Здесь других объёмных свечей нет вовсе — значит после исключения последних
    LIQ_SKIP_LAST свечей зон не остаётся, и сигналу взяться неоткуда.
    """
    df = _spring_df()          # объёмная только свеча 23 — она же свеча пробоя
    source = df.iloc[:-config.LIQ_SKIP_LAST]
    assert not analyzer.find_liquidity_zones(source), "свеча пробоя не должна давать зону"


# ── Порог аномального объёма: множитель против процентиля ───────────────────

def _pctl_settings(pctl: float, window: int = 20) -> dict:
    """Пороги для режима «объём выше P-го процентиля за N свечей»."""
    return {"VOL_MODE": "pctl", "VOL_PCTL": pctl, "VOL_WINDOW": window,
            **NO_RISK_FILTER}


def test_volume_percentile_accepts_top_bar():
    # Свеча пробоя — самая объёмная из окна (300 против 100 у соседей),
    # то есть заведомо выше любого процентиля → сигнал есть.
    df = _spring_df()
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings=_pctl_settings(90)) is not None


def test_volume_percentile_rejects_ordinary_bar():
    # Объём как у соседей → в верхние проценты не попадает → не Spring.
    df = _spring_df()
    df.iloc[23, df.columns.get_loc("volume")] = 100.0
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings=_pctl_settings(90)) is None


def test_volume_percentile_is_relative_not_absolute():
    """Смысл процентиля: порог свой у каждого инструмента.

    Здесь объём свечи пробоя (150) НИЖЕ среднего соседей (200), то есть множитель
    ×1.5 её забракует. Но соседи разношёрстные, и 150 всё равно выше 70% из них —
    процентиль пропустит. Ровно этого и ждём от «относительного» порога: он
    сравнивает свечу с её же окружением, а не с общим числом.
    """
    rows = [(100.5, 101.0, 100.2, 100.6, 100.0 if i % 4 else 600.0) for i in range(25)]
    rows[23] = (100.4, 100.7, 99.0, 100.5, 150.0)
    rows[24] = (100.5, 100.8, 100.3, 100.6, 50.0)
    df = _df(rows)
    levels = [{"price": 100.0, "type": "support", "strength": "strong"}]
    # Множитель: 150 < среднее(≈225) × 1.5 → сигнала нет.
    assert pattern_detector.detect_spring(
        df, levels, trend="up",
        settings={"VOL_MODE": "mult", "VOL_MULT": 1.5, **NO_RISK_FILTER}) is None
    # Процентиль P70: 150 выше 70% соседей (их большинство — по 100) → сигнал есть.
    assert pattern_detector.detect_spring(
        df, levels, trend="up", settings=_pctl_settings(70)) is not None


def test_volume_percentile_window_excludes_own_bar():
    """Свеча не входит в собственную норму — иначе задирала бы порог, который
    сама должна перепрыгнуть. Проверяем на окне, где она единственная крупная."""
    df = _spring_df()
    window = df["volume"].iloc[3:23]           # 20 свечей ПЕРЕД свечой пробоя
    assert float(window.max()) == 100.0, "в окне не должно быть свечи пробоя"
    assert pattern_detector._volume_ok(
        df, 23, {"VOL_MODE": "pctl", "VOL_PCTL": 95, "VOL_WINDOW": 20}, 1.5)


def test_volume_percentile_needs_enough_history():
    # Окна короче VOL_LOOKBACK не хватает даже на осмысленный процентиль.
    df = _spring_df()
    assert not pattern_detector._volume_ok(
        df, 5, {"VOL_MODE": "pctl", "VOL_PCTL": 70, "VOL_WINDOW": 20}, 1.5)


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
    sig = pattern_detector.detect_upthrust(df, levels, trend="down", settings=NO_RISK_FILTER)
    assert sig is not None
    assert sig["direction"] == "short"
    assert abs(sig["entry_price"] - 99.5) < 1e-9
    assert abs(sig["take_profit"] - 90.0) < 1e-9
    assert sig["stop_loss"] > 101.0                # стоп выше максимума пробоя


def test_upthrust_filtered_by_uptrend():
    df = _upthrust_df()
    levels = [{"price": 100.0, "type": "resistance", "strength": "strong"}]
    assert pattern_detector.detect_upthrust(df, levels, trend="up", settings=NO_RISK_FILTER) is None


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
    # Без недельного окна (журнал сделок) горизонт задаёт SIGNAL_EXPIRE_HOURS.
    rows = [(100.5, 101, 100, 100.5, 100)
            for _ in range(config.SIGNAL_EXPIRE_HOURS + 2)]
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "expired"


def test_evaluate_not_expired_within_week():
    # Сделка внутри недели (меньше горизонта) ещё не истекла — ждём дальше.
    rows = [(100.5, 101, 100, 100.5, 100)
            for _ in range(config.SIGNAL_EXPIRE_HOURS - 10)]
    df = _df(rows)
    assert pattern_detector.evaluate_signal(_long_signal(df), df) == "pending"


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"OK  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} тестов прошли")
