"""Юнит-тесты денежной симуляции (money.py) на рукописных сделках.

Сетевых запросов и истории нет — сделки задаются вручную, поэтому каждую цифру
можно проверить в уме. Запуск:  python tests/test_money.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import money  # noqa: E402


def _trade(outcome: str, rr: float = 2.0, entry: float = 100.0,
           risk: float = 1.0, hours: int = 0, **extra) -> dict:
    """Сделка в том виде, в каком её отдаёт бэктест. risk=1 при entry=100 → риск 1%."""
    return {"outcome": outcome, "rr": rr, "entry_price": entry, "risk": risk,
            "direction": "long", "stop_loss": entry - risk,
            "bar_time": f"2024-01-01 {hours:02d}:00:00+00:00", **extra}


# ── Результат сделки в R ────────────────────────────────────────────────────

def test_trade_r_target_gives_full_rr():
    assert money.trade_r(_trade("hit_tp", rr=2.5)) == 2.5


def test_trade_r_stop_gives_minus_one():
    assert money.trade_r(_trade("hit_sl")) == -1.0


def test_trade_r_expired_gives_zero():
    assert money.trade_r(_trade("expired")) == 0.0


def test_trade_r_closed_week_uses_exit_price():
    # Вход 100, стоп 99 (риск 1), выход 100.5 → +0.5R.
    s = _trade("closed_week", exit_price=100.5)
    assert abs(money.trade_r(s) - 0.5) < 1e-9


# ── Издержки ────────────────────────────────────────────────────────────────

def test_cost_in_r_scales_with_stop_width():
    """Чем теснее стоп, тем дороже сделка в R: издержки — доля цены, R — тоже."""
    wide = _trade("hit_tp", risk=1.0)     # риск 1% от цены
    tight = _trade("hit_tp", risk=0.5)    # риск 0.5% от цены
    assert abs(money.cost_in_r(wide, 0.001) - 0.1) < 1e-9    # 0.1% / 1%
    assert abs(money.cost_in_r(tight, 0.001) - 0.2) < 1e-9   # 0.1% / 0.5%


def test_cost_zero_when_no_fee():
    assert money.cost_in_r(_trade("hit_tp"), 0.0) == 0.0


def test_cost_differs_by_venue():
    """Ставка своя у каждой площадки: крипта на Bybit дороже фьючерсов через БКС.
    Инструмент берётся из сделки, источник — из реестра instruments."""
    costs = {"ccxt": 0.0015, "yahoo": 0.0005}
    btc = _trade("hit_tp", instrument="BTC")     # крипта → ccxt
    gold = _trade("hit_tp", instrument="GOLD")   # золото → yahoo
    assert abs(money.cost_in_r(btc, costs) - 0.15) < 1e-9    # 0.15% / 1%
    assert abs(money.cost_in_r(gold, costs) - 0.05) < 1e-9   # 0.05% / 1%
    assert money.cost_in_r(btc, costs) > money.cost_in_r(gold, costs)


def test_cost_scalar_still_works():
    """Одна ставка на всех — так бэктест считает колонку НЕТТО R."""
    assert abs(money.cost_in_r(_trade("hit_tp", instrument="BTC"), 0.001) - 0.1) < 1e-9


def test_unknown_instrument_costs_nothing():
    """Своя пара без источника (сырой тикер) — ставки для неё нет, не падаем."""
    assert money.cost_in_r(_trade("hit_tp", instrument="EURGBP=X"),
                           {"ccxt": 0.0015, "yahoo": 0.0005}) == 0.0


# ── Симуляция счёта ─────────────────────────────────────────────────────────

def test_simulate_compounds():
    # Две сделки по +2R при риске 1% → 1000 × 1.02 × 1.02 = 1040.4
    trades = [_trade("hit_tp", hours=0), _trade("hit_tp", hours=1)]
    res = money.simulate(trades, 1000, 0.01)
    assert abs(res["final"] - 1040.4) < 1e-6
    assert abs(res["profit_pct"] - 0.0404) < 1e-9


def test_simulate_costs_reduce_result():
    trades = [_trade("hit_tp", hours=0)]
    free = money.simulate(trades, 1000, 0.01)
    paid = money.simulate(trades, 1000, 0.01, costs=0.001)
    # Риск 1% от цены → издержки 0.1% стоят 0.1R: было +2R, стало +1.9R.
    assert abs(paid["final"] - 1000 * (1 + 0.01 * 1.9)) < 1e-9
    assert paid["final"] < free["final"]


def test_simulate_max_drawdown():
    # +2R, затем три стопа подряд: пик 1020, дно 1020×0.99³ = 989.7...
    trades = [_trade("hit_tp", hours=0)] + [_trade("hit_sl", hours=h) for h in (1, 2, 3)]
    res = money.simulate(trades, 1000, 0.01)
    expected_dd = 1 - 0.99 ** 3
    assert abs(res["max_dd"] - expected_dd) < 1e-9
    assert res["worst_streak"] == 3


def test_simulate_counts_expired_as_losing_streak():
    """Истёкшая сделка даёт 0R — прибыли нет, значит серию она не прерывает."""
    trades = [_trade("hit_sl", hours=0), _trade("expired", hours=1),
              _trade("hit_sl", hours=2)]
    assert money.simulate(trades, 1000, 0.01)["worst_streak"] == 3


def test_simulate_winrate_ignores_zero_trades():
    trades = [_trade("hit_tp", hours=0), _trade("hit_sl", hours=1)]
    assert money.simulate(trades, 1000, 0.01)["winrate"] == 0.5


def test_simulate_empty():
    res = money.simulate([], 1000, 0.01)
    assert res["final"] == 1000 and res["n"] == 0 and res["max_dd"] == 0.0


def test_chronological_interleaves_instruments():
    """Сделки разных инструментов идут вперемешку по времени — как на счёте."""
    rows = money.chronological({
        "BTC": [_trade("hit_tp", hours=3), _trade("hit_sl", hours=1)],
        "GOLD": [_trade("hit_tp", hours=2)],
    })
    assert [r["instrument"] for r in rows] == ["BTC", "GOLD", "BTC"]


def test_ruined_flag():
    # Риск 50% на сделку, два стопа подряд → счёт четверть от старта.
    trades = [_trade("hit_sl", hours=0), _trade("hit_sl", hours=1)]
    assert money.simulate(trades, 1000, 0.5)["ruined"]


def _run() -> None:
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
    print(f"\nвсего {len(tests)}, провалено {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run()
