"""Юнит-тесты бэктеста: замер MAE, сводка по варианту, честность прогона.

Сетевых запросов нет — свечи строятся вручную. Запуск без pytest:
    python tests/test_backtest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import backtest  # noqa: E402
import config  # noqa: E402
import pattern_detector  # noqa: E402


def _df(rows: list[tuple], start: str = "2024-01-01") -> pd.DataFrame:
    """rows: список (open, high, low, close, volume). Индекс — почасовой UTC."""
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ── Замер хода против позиции ────────────────────────────────────────────────

def test_mae_measures_from_break_extreme():
    """Лонг: вход 100, лоу свечи пробоя 98, цена сходила на 97 и дошла до цели 110.
    Ход против позиции от экстремума = 98 − 97 = 1."""
    signal = {"direction": "long", "entry_price": 100.0, "take_profit": 110.0,
              "break_extreme": 98.0}
    future = _df([
        (100, 101, 97, 99, 10),    # свип на 97 — на 1 ниже экстремума пробоя
        (99, 111, 99, 110, 10),    # дошли до цели
    ])
    m = backtest.measure_excursion(signal, future)
    assert m["reached_tp"] is True
    assert round(m["mae_abs"], 6) == 1.0
    assert round(m["mae_from_entry"], 6) == 3.0
    assert m["bars"] == 2


def test_mae_ignores_moves_after_target():
    """Провал после достижения цели в MAE не попадает — сделка к тому моменту закрыта."""
    signal = {"direction": "long", "entry_price": 100.0, "take_profit": 105.0,
              "break_extreme": 99.0}
    future = _df([
        (100, 106, 99.5, 105, 10),  # цель взята сразу
        (105, 105, 80.0, 81, 10),   # обвал уже после закрытия — не считается
    ])
    m = backtest.measure_excursion(signal, future)
    assert m["reached_tp"] is True
    assert round(m["mae_abs"], 6) == 0.0


def test_mae_short_mirror():
    """Шорт: ход против позиции считается вверх от хая свечи пробоя."""
    signal = {"direction": "short", "entry_price": 100.0, "take_profit": 90.0,
              "break_extreme": 102.0}
    future = _df([
        (100, 104, 99, 100, 10),   # свип вверх на 104 → 2 выше экстремума
        (100, 100, 89, 90, 10),    # дошли до цели
    ])
    m = backtest.measure_excursion(signal, future)
    assert m["reached_tp"] is True
    assert round(m["mae_abs"], 6) == 2.0


def test_mae_no_target_reached():
    signal = {"direction": "long", "entry_price": 100.0, "take_profit": 200.0,
              "break_extreme": 99.0}
    future = _df([(100, 101, 95, 96, 10)])
    m = backtest.measure_excursion(signal, future)
    assert m["reached_tp"] is False
    assert round(m["mae_abs"], 6) == 4.0


# ── Сводка по варианту ───────────────────────────────────────────────────────

def test_summary_counts_r_correctly():
    """Итог в R: цель даёт +фактический R:R, стоп −1, истёкшие 0."""
    signals = [
        {"outcome": "hit_tp", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": True},
        {"outcome": "hit_tp", "rr": 3.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": True},
        {"outcome": "hit_sl", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": False},
        {"outcome": "expired", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": False},
    ]
    s = backtest.summarize(signals)
    assert s["n"] == 4 and s["tp"] == 2 and s["sl"] == 1 and s["other"] == 1
    assert round(s["total_r"], 6) == 4.0          # +2 +3 −1
    assert round(s["winrate"], 6) == round(2 / 3, 6)  # истёкший в винрейт не входит
    assert round(s["pf"], 6) == 5.0               # 5 плюсов / 1 минус


def test_summary_counts_closed_week_by_exit_price():
    """Закрытая по неделе считается по фактической цене выхода: плюсовой выход идёт
    в плюсы, минусовой — в минусы, а не выпадает из статистики."""
    signals = [
        {"outcome": "closed_week", "rr": 2.0, "risk": 5.0, "direction": "long",
         "entry_price": 100.0, "stop_loss": 95.0, "exit_price": 102.5, "reached_tp": False},
        {"outcome": "closed_week", "rr": 2.0, "risk": 5.0, "direction": "short",
         "entry_price": 100.0, "stop_loss": 105.0, "exit_price": 101.0, "reached_tp": False},
    ]
    s = backtest.summarize(signals)
    assert s["week"] == 2
    assert s["tp"] == 1 and s["sl"] == 1        # разнесены по знаку результата
    assert round(s["total_r"], 6) == 0.3        # +0.5R и −0.2R
    assert s["other"] == 0                      # это закрытые сделки, не «прочее»


def test_summary_closed_week_without_exit_price_is_neutral():
    """Без цены выхода результат посчитать нечем — сделка идёт в ноль, а не в минус."""
    signals = [{"outcome": "closed_week", "rr": 2.0, "risk": 5.0, "direction": "long",
                "entry_price": 100.0, "stop_loss": 95.0, "exit_price": None,
                "reached_tp": False}]
    s = backtest.summarize(signals)
    assert s["week"] == 1 and round(s["total_r"], 6) == 0.0
    assert s["tp"] == 0 and s["sl"] == 0


def test_summary_counts_swept_and_ran():
    """«Выбило и поехало» — сделка закрылась по стопу, хотя без стопа дошла бы до цели."""
    signals = [
        {"outcome": "hit_sl", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": True},
        {"outcome": "hit_sl", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": False},
        {"outcome": "hit_tp", "rr": 2.0, "risk": 1.0, "entry_price": 100.0, "reached_tp": True},
    ]
    s = backtest.summarize(signals)
    assert s["swept"] == 1          # только тот стоп, после которого цена дошла бы до цели
    assert round(s["swept_pct"], 6) == round(1 / 3, 6)


# ── Варианты стопа доезжают до боевого детектора ─────────────────────────────

# Фикстуры ниже проверяют ДРУГУЮ логику — как варианты стопа доезжают до боевого
# детектора. Свеча пробоя у них нарочно размашистая (сходили на 98 и вернулись),
# чтобы паттерн был однозначен, а боевые фильтры отбора такой вход как раз бракуют:
# цену входа — по риску в ATR, дистанцию — по расстоянию закрытия до уровня,
# «поглощение» — по слишком большому телу. Поэтому все три отключены явно; у самих
# фильтров свои тесты в tests/test_engine.py.
BASE = {"BREAK_PCT": 0.0005, "MIN_RR": 1.5, "MAX_RISK_ATR": None,
        "MAX_ENTRY_DIST_ATR": None, "MAX_BODY_RATIO": None}


def _spring_setup():
    """Свечи с ложным пробоем поддержки 100 на повышенном объёме + сам уровень."""
    rows = [(100.5, 101.0, 100.2, 100.6, 100) for _ in range(config.VOL_LOOKBACK + 1)]
    rows.append((100.4, 100.6, 98.0, 100.9, 1000))   # свеча пробоя: сходили на 98, закрылись выше
    rows.append((100.9, 101.0, 100.8, 100.95, 100))  # формирующаяся (детектор её не читает)
    levels = [{"price": 100.0, "type": "support", "strength": "strong",
               "is_liquidity": 0, "timeframe": "H1"}]
    return _df(rows), levels


def test_stop_spread_override_widens_stop():
    """STOP_SPREAD из настроек переопределяет config.STOP_SPREAD."""
    df, levels = _spring_setup()
    base = dict(BASE)
    narrow = pattern_detector.detect_spring(df, levels, "sideways", {**base, "STOP_SPREAD": 0.001})
    wide = pattern_detector.detect_spring(df, levels, "sideways", {**base, "STOP_SPREAD": 0.01})
    assert narrow is not None and wide is not None
    assert round(narrow["stop_loss"], 4) == round(98.0 * 0.999, 4)
    assert round(wide["stop_loss"], 4) == round(98.0 * 0.99, 4)
    assert wide["stop_loss"] < narrow["stop_loss"]


def test_stop_abs_beats_percent():
    """STOP_ABS (например k×ATR) задаёт запас абсолютным числом и главнее процента."""
    df, levels = _spring_setup()
    base = dict(BASE)
    sig = pattern_detector.detect_spring(
        df, levels, "sideways", {**base, "STOP_SPREAD": 0.001, "STOP_ABS": 2.0})
    assert sig is not None
    assert round(sig["stop_loss"], 6) == 96.0     # 98.0 − 2.0, процент проигнорирован


def _detect_in_mode(mode: str, df, levels, base):
    """Прогон детектора в заданном режиме стопа. Режим — глобальная настройка,
    поэтому меняем её только на время проверки и возвращаем обратно."""
    saved = config.STOP_MODE
    config.STOP_MODE = mode
    try:
        return pattern_detector.detect_spring(df, levels, "sideways", base)
    finally:
        config.STOP_MODE = saved


def test_default_stop_is_pct():
    """По умолчанию стоп — процент от цены. Так решил бэктест: ATR проигрывает
    проценту и по сумме R, и на сделку, и на обеих половинах истории (см. config)."""
    assert config.STOP_MODE == "pct"
    df, levels = _spring_setup()
    base = dict(BASE)
    sig = pattern_detector.detect_spring(df, levels, "sideways", base)
    assert sig is not None
    assert round(sig["stop_loss"], 6) == round(98.0 * (1 - config.STOP_SPREAD), 6)


def test_atr_mode_uses_atr():
    """Режим STOP_MODE='atr' считает запас в долях ATR. Сейчас не включён, но
    механизм рабочий — на нём гоняются ATR-варианты в бэктесте."""
    df, levels = _spring_setup()
    base = dict(BASE)
    sig = _detect_in_mode("atr", df, levels, base)
    assert sig is not None
    atr = pattern_detector._atr(df, len(df) - 2, config.STOP_ATR_PERIOD)
    assert atr > 0
    assert round(sig["stop_loss"], 6) == round(98.0 - atr * config.STOP_ATR_MULT, 6)


def test_atr_stop_scales_with_volatility():
    """Главное свойство ATR-стопа: на более размашистых свечах запас шире.
    В бою режим выключен (процент считает лучше, см. config), но свойство должно
    работать — иначе ATR-варианты в бэктесте сравнивались бы вхолостую."""
    quiet, levels = _spring_setup()
    wild = quiet.copy()
    # Раздуваем размах свечей ДО пробоя, сам пробой и уровень не трогаем.
    for col, shift in (("high", +2.0), ("low", -2.0)):
        wild.iloc[:-2, wild.columns.get_loc(col)] = quiet[col].iloc[:-2] + shift
    base = dict(BASE)
    a = _detect_in_mode("atr", quiet, levels, base)
    b = _detect_in_mode("atr", wild, levels, base)
    assert a is not None and b is not None
    assert b["stop_loss"] < a["stop_loss"]   # волатильнее → стоп дальше от входа


def test_wider_stop_lowers_rr():
    """Расширение стопа не бесплатно: тот же вход даёт меньший R:R."""
    df, levels = _spring_setup()
    base = {**BASE, "MIN_RR": 1.0}
    narrow = pattern_detector.detect_spring(df, levels, "sideways", {**base, "STOP_SPREAD": 0.001})
    wide = pattern_detector.detect_spring(df, levels, "sideways", {**base, "STOP_SPREAD": 0.01})
    risk_n = narrow["entry_price"] - narrow["stop_loss"]
    risk_w = wide["entry_price"] - wide["stop_loss"]
    assert risk_w > risk_n


# ── Реконструкция дневной свечи без заглядывания в будущее ──────────────────

def test_d1_view_builds_today_from_past_hours_only():
    """Дневная свеча текущего дня собирается из H1-баров ДО сигнального включительно —
    итог дня, которого в бою ещё не знали, в неё попасть не должен."""
    h1 = _df([
        (10, 12, 9, 11, 5),      # 00:00 текущего дня
        (11, 15, 10, 14, 5),     # 01:00 — сигнальный бар (i=1)
        (14, 99, 1, 50, 5),      # 02:00 — будущее, попасть в срез не должно
    ], start="2024-03-05")
    d1 = pd.DataFrame(
        [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}],
        index=pd.to_datetime(["2024-03-04"], utc=True))
    view = backtest._d1_view(d1, h1, 1)
    today = view.iloc[-1]
    assert float(today["open"]) == 10        # открытие первого часа дня
    assert float(today["high"]) == 15        # максимум только по двум первым часам
    assert float(today["low"]) == 9
    assert float(today["close"]) == 14       # закрытие сигнального бара
    assert float(today["volume"]) == 10
    assert len(view) == 2                    # закрытый вчерашний день + сегодняшний


def test_d1_view_excludes_ready_today_bar():
    """Готовая дневная свеча текущего дня из d1 не берётся — она содержит итог дня."""
    h1 = _df([(10, 12, 9, 11, 5), (11, 13, 10, 12, 5)], start="2024-03-05")
    d1 = pd.DataFrame(
        [{"open": 10, "high": 500, "low": 1, "close": 400, "volume": 999}],
        index=pd.to_datetime(["2024-03-05"], utc=True))
    view = backtest._d1_view(d1, h1, 1)
    assert len(view) == 1
    assert float(view.iloc[-1]["high"]) == 13   # из H1, а не 500 из готовой дневки


# ── ATR ──────────────────────────────────────────────────────────────────────

def test_atr_on_constant_range():
    """Свечи с постоянным диапазоном 2 и без гэпов → ATR = 2."""
    rows = [(100, 101, 99, 100, 10) for _ in range(30)]
    atr = backtest._atr(_df(rows), period=14)
    assert round(float(atr.iloc[-1]), 6) == 2.0


# ── Новые переборы: дистанция, слой H4, дневной гейт ────────────────────────

def _base() -> dict:
    return config.effective({})


def test_dist_sweep_has_current_variant_and_grid():
    """Перебор обязан содержать вариант «как сейчас» — иначе не с чем сравнивать."""
    names = [v["name"] for v in backtest.build_variants("dist", _base())]
    assert any("сейчас" in n for n in names)
    assert len(names) == 1 + len(backtest.ENTRY_DISTS)


def test_dist_sweep_patches_only_distance():
    """Вариант меняет ровно один рычаг: иначе непонятно, что дало разницу."""
    for v in backtest.build_variants("dist", _base()):
        assert set(v["patch"](1.0)) <= {"MAX_ENTRY_DIST_ATR"}


def test_tf_sweep_covers_all_timeframes():
    """Три таймфрейма тренда плюс уровни H4 — и комбинации, потому что рычаги,
    полезные по отдельности, вместе могут мешать друг другу."""
    variants = backtest.build_variants("tf", _base())
    assert {v["trend_tf"] for v in variants} == {"h1", "h4", "d1"}
    assert {v["h4"] for v in variants} == {True, False}


def test_d1_sweep_includes_hard_control():
    """Жёсткий запрет контртренда обязан быть в переборе контролем: без него
    не видно, отличается ли мягкий гейт от простого запрета."""
    patches = [v["patch"](1.0) for v in backtest.build_variants("d1", _base())]
    gates = {p.get("D1_GATE") for p in patches}
    assert gates == {"off", "soft", "hard"}
    assert {p.get("D1_CONFIRM") for p in patches if p.get("D1_GATE") == "soft"} == \
        {"strong", "volume", "rr"}


def test_d1_sweep_marks_variants_needing_daily_trend():
    """Вариантам с гейтом нужен дневной тренд, даже когда рабочий ТФ часовой.
    Флаг needs_d1 — то, по чему replay решает его посчитать."""
    for v in backtest.build_variants("d1", _base()):
        needs = v["patch"](1.0).get("D1_GATE") != "off"
        assert bool(v.get("needs_d1")) == needs


def test_levels_at_h4_adds_levels():
    """Средний слой добавляет уровни, а без него набор ровно прежний."""
    rows = [(100 + i % 5, 102 + i % 5, 98 + i % 5, 100 + i % 5, 100.0) for i in range(80)]
    h1 = _df(rows)
    d1 = _df([(100, 105, 95, 100, 1000.0) for _ in range(20)])
    plain = backtest._levels_at(d1, h1, "off", h4_levels=False)
    with_h4 = backtest._levels_at(d1, h1, "off", h4_levels=True)
    assert len(with_h4) >= len(plain)
    assert not any(l.get("timeframe") == "H4" for l in plain)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"✗ {name}: {type(e).__name__}: {e}")
    print(f"\nвсего {len(tests)}, провалено {failed}")
    sys.exit(1 if failed else 0)
