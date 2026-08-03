"""Юнит-тесты недельного окна: вход только пн–чт, выход до выходных.

Сетевых запросов нет — календарь и свечи строятся вручную. Запуск без pytest:
    python tests/test_trading_week.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import pattern_detector  # noqa: E402
import trading_week  # noqa: E402

# 2024-03-04 — понедельник, 2024-03-08 — пятница.
MON = "2024-03-04"
THU = "2024-03-07"
FRI = "2024-03-08"
SAT = "2024-03-09"
SUN = "2024-03-10"


def _ts(day: str, hour: int) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hour:02d}:00", tz="UTC")


def _df(rows: list[tuple], start: str, hour: int = 0) -> pd.DataFrame:
    """rows: (open, high, low, close, volume). Индекс — почасовой UTC от start."""
    idx = pd.date_range(f"{start} {hour:02d}:00", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ── Календарь недели ─────────────────────────────────────────────────────────

def test_week_close_is_friday_evening():
    close = trading_week.week_close_after(_ts(MON, 10))
    assert close.weekday() == 4                      # пятница
    assert close.hour == config.WEEK_CLOSE_HOUR
    assert str(close.date()) == FRI


def test_week_close_rolls_over_after_friday_evening():
    """После пятничного закрытия ближайшее закрытие — уже следующей недели."""
    after_close = _ts(FRI, config.WEEK_CLOSE_HOUR + 1)
    assert str(trading_week.week_close_after(after_close).date()) == "2024-03-15"


def test_week_close_on_weekend_points_to_next_friday():
    assert str(trading_week.week_close_after(_ts(SAT, 12)).date()) == "2024-03-15"
    assert str(trading_week.week_close_after(_ts(SUN, 12)).date()) == "2024-03-15"


def test_hours_until_close():
    # Пятница 09:00 → до закрытия WEEK_CLOSE_HOUR − 9 часов.
    assert trading_week.hours_until_close(_ts(FRI, 9)) == config.WEEK_CLOSE_HOUR - 9


def test_naive_time_treated_as_utc():
    """Время без зоны считаем UTC — свечи движка приходят именно такими."""
    assert trading_week.to_utc(pd.Timestamp(f"{MON} 10:00")).tz is not None


# ── Разрешение на вход ───────────────────────────────────────────────────────

def test_no_entry_on_friday_at_all():
    """Пятница закрыта целиком — даже утром, когда времени до закрытия ещё вагон."""
    for hour in (0, 6, 12, 20):
        allowed, why = trading_week.is_entry_allowed(_ts(FRI, hour))
        assert allowed is False, f"пятница {hour}:00 не должна пускать вход"
        assert why == "weekday"


def test_no_entry_on_weekend():
    assert trading_week.is_entry_allowed(_ts(SAT, 12))[0] is False
    assert trading_week.is_entry_allowed(_ts(SUN, 12))[0] is False


def test_entry_allowed_monday_to_thursday():
    for day in (MON, "2024-03-05", "2024-03-06", THU):
        allowed, why = trading_week.is_entry_allowed(_ts(day, 10))
        assert allowed is True, f"{day} должен пускать вход (причина отказа: {why})"


def test_entry_allowed_late_thursday():
    """Четверг 23:00 — до пятничного закрытия остаётся больше запаса, вход можно."""
    assert trading_week.hours_until_close(_ts(THU, 23)) >= config.MIN_HOURS_BEFORE_CLOSE
    assert trading_week.is_entry_allowed(_ts(THU, 23))[0] is True


def test_tail_guard_blocks_entry_too_close_to_close(monkeypatched=None):
    """Если до закрытия недели меньше запаса — вход не даём (причина 'tail').
    Проверяем на четверге, временно подняв требуемый запас выше суток."""
    saved = config.MIN_HOURS_BEFORE_CLOSE
    config.MIN_HOURS_BEFORE_CLOSE = 48
    try:
        allowed, why = trading_week.is_entry_allowed(_ts(THU, 10))
        assert allowed is False and why == "tail"
    finally:
        config.MIN_HOURS_BEFORE_CLOSE = saved


# ── Детектор не выдаёт сигналы вне окна ──────────────────────────────────────

def _spring_rows() -> list[tuple]:
    rows = [(100.5, 101.0, 100.2, 100.6, 100) for _ in range(config.VOL_LOOKBACK + 1)]
    rows.append((100.4, 100.6, 98.0, 100.9, 1000))   # свеча пробоя
    rows.append((100.9, 101.0, 100.8, 100.95, 100))  # формирующаяся
    return rows


LEVELS = [{"price": 100.0, "type": "support", "strength": "strong",
           "is_liquidity": 0, "timeframe": "H1"}]
BASE = {"VOL_MULT": 1.5, "BREAK_PCT": 0.0005, "MIN_RR": 1.5}


def _detect_with_break_at(day: str, hour: int):
    """Собирает окно так, чтобы свеча пробоя (предпоследняя) пришлась на day/hour."""
    rows = _spring_rows()
    start = pd.Timestamp(f"{day} {hour:02d}:00", tz="UTC") - pd.Timedelta(hours=len(rows) - 2)
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    assert str(df.index[-2].date()) == day and df.index[-2].hour == hour
    return pattern_detector.detect_spring(df, LEVELS, "sideways", BASE)


def test_detector_gives_signal_on_thursday():
    assert _detect_with_break_at(THU, 12) is not None


def test_detector_silent_on_friday():
    assert _detect_with_break_at(FRI, 12) is None


def test_detector_silent_on_weekend():
    assert _detect_with_break_at(SAT, 12) is None
    assert _detect_with_break_at(SUN, 12) is None


def test_week_filter_can_be_disabled_for_backtest():
    """WEEK_FILTER=False возвращает старое поведение — нужно для сравнения в бэктесте."""
    rows = _spring_rows()
    start = pd.Timestamp(f"{FRI} 12:00", tz="UTC") - pd.Timedelta(hours=len(rows) - 2)
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    assert pattern_detector.detect_spring(df, LEVELS, "sideways", BASE) is None
    assert pattern_detector.detect_spring(
        df, LEVELS, "sideways", {**BASE, "WEEK_FILTER": False}) is not None


# ── Принудительное закрытие на выходе недели ────────────────────────────────

def test_signal_closed_at_week_end_when_nothing_hit():
    """Сделка четверга не дошла ни до цели, ни до стопа → гасим по последней свече
    перед пятничным закрытием, по её close."""
    signal = {"direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
              "take_profit": 110.0, "bar_time": str(_ts(THU, 20))}
    # Свечи с четверга 21:00 и дальше через всю пятницу — ничего не задевают.
    rows = [(100, 101, 99, 100 + i * 0.1, 10) for i in range(30)]
    df = _df(rows, THU, hour=21)
    assert df.index[-1] > trading_week.week_close_after(signal["bar_time"])
    res = pattern_detector.evaluate_signal_detailed(signal, df, week_close=True)
    assert res["status"] == "closed_week"
    assert res["exit_time"] <= trading_week.week_close_after(signal["bar_time"])
    assert res["exit_price"] == float(df[df.index <= res["exit_time"]]["close"].iloc[-1])


def test_target_before_week_end_still_wins():
    """Цель, взятая до закрытия недели, остаётся целью — окно её не отменяет."""
    signal = {"direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
              "take_profit": 102.0, "bar_time": str(_ts(THU, 20))}
    rows = [(100, 103, 99, 102, 10)] + [(102, 103, 101, 102, 10) for _ in range(29)]
    df = _df(rows, THU, hour=21)
    res = pattern_detector.evaluate_signal_detailed(signal, df, week_close=True)
    assert res["status"] == "hit_tp"


def test_no_force_close_before_week_actually_ends():
    """Пока пятничное закрытие не наступило, сделка просто ждёт."""
    signal = {"direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
              "take_profit": 110.0, "bar_time": str(_ts(THU, 20))}
    df = _df([(100, 101, 99, 100, 10) for _ in range(3)], THU, hour=21)
    res = pattern_detector.evaluate_signal_detailed(signal, df, week_close=True)
    assert res["status"] == "pending"


def test_monday_signal_lives_until_friday_close():
    """Вход в начале недели держим до конца этой же недели: сигнал понедельника не
    истекает на середине недели, а гасится по рынку на пятничном закрытии.

    Это и есть недельная логика — горизонт задаёт закрытие недели (максимум 117ч
    от понедельника), а SIGNAL_EXPIRE_HOURS (120ч) до него не дотягивается.
    """
    signal = {"direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
              "take_profit": 110.0, "bar_time": str(_ts(MON, 10))}
    rows = [(100, 101, 99, 100, 10) for _ in range(24 * 6)]
    df = _df(rows, MON, hour=11)
    res = pattern_detector.evaluate_signal_detailed(signal, df, week_close=True)
    assert res["status"] == "closed_week"
    assert res["exit_time"] <= trading_week.week_close_after(signal["bar_time"])
    # Горизонта истечения сигнал внутри недели не достигает — проверяем явно.
    assert config.SIGNAL_EXPIRE_HOURS > trading_week.hours_until_close(_ts(MON, 0))


def test_journal_untouched_without_week_close():
    """week_close=False (журнал сделок) — прежнее поведение: выходные сделку не
    закрывают, она висит открытой, пока не дойдёт до цели/стопа или не истечёт."""
    signal = {"direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
              "take_profit": 110.0, "bar_time": str(_ts(THU, 20))}
    rows = [(100, 101, 99, 100, 10) for _ in range(30)]   # чт 21:00 → сб 02:00, 30ч
    df = _df(rows, THU, hour=21)
    assert pattern_detector.evaluate_signal(signal, df) == "pending"
    # ...а с недельным окном тот же сигнал был бы погашен в пятницу.
    assert pattern_detector.evaluate_signal(signal, df, week_close=True) == "closed_week"


# ── Результат закрытой по рынку сделки в R ──────────────────────────────────

def test_realized_r_long_partial_profit():
    """Лонг: вход 100, стоп 95 (риск 5), вышли по 102.5 → +0.5R."""
    assert trading_week.realized_r("long", 100.0, 95.0, 102.5) == 0.5


def test_realized_r_long_partial_loss():
    assert trading_week.realized_r("long", 100.0, 95.0, 98.0) == -0.4


def test_realized_r_short_mirror():
    """Шорт: вход 100, стоп 105 (риск 5), вышли по 97.5 → +0.5R."""
    assert trading_week.realized_r("short", 100.0, 105.0, 97.5) == 0.5


def test_realized_r_zero_risk_is_safe():
    assert trading_week.realized_r("long", 100.0, 100.0, 120.0) == 0.0


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
