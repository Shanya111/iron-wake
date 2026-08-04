"""Перевод результата бэктеста из R в деньги: что было бы со счётом.

Бэктест меряет всё в R (риск на сделку = 1R) — так сравнивают варианты движка между
собой, но для владельца счёта это ничего не значит. Здесь тот же набор сделок
прогоняется как реальный счёт: с процентом риска на сделку, сложным процентом,
просадкой и издержками.

Главная цифра тут НЕ прибыль, а **максимальная просадка** — сколько счёт терял от
пика. Прибыль решает, стоит ли игра свеч; просадка решает, доживёшь ли ты до неё.

Про издержки. Бэктест их не считает вовсе: вход по цене закрытия свечи, выход по
цене цели или стопа. В жизни есть комиссия биржи, спред и проскальзывание. В шкале
этого движка они весят много: средний риск сделки ≈ 0.5% от цены, то есть 1R ≈
полпроцента движения, и комиссия «туда-обратно» 0.1% съедает уже 0.2R. Поэтому
результат без издержек показывать нельзя — он завышен и вреден.
"""

from datetime import datetime

import pandas as pd

import trading_week
from instruments import data_source

# Сценарии издержек «туда-обратно», в долях от цены. Ставка РАЗНАЯ у разных
# площадок, поэтому задаётся по источнику данных инструмента (instruments.data_source):
#   "ccxt"  — крипта (BTC/ETH/SOL/TON), торгуется на Bybit;
#   "yahoo" — золото и нефть, торгуются фьючерсами через БКС.
# Комиссия фьючерсов на МосБирже копеечная относительно объёма контракта, поэтому
# у золота с нефтью ставка в разы ниже, чем у крипты, — и это заметно меняет итог.
#
# В каждой ставке сидит и комиссия, и спред с проскальзыванием: вход по рыночной
# цене закрытия свечи — это заявка «по рынку», и она всегда берёт худшую сторону.
COST_SCENARIOS = (
    ({"ccxt": 0.0, "yahoo": 0.0},
     "идеал: издержек нет (так считает сам бэктест)"),
    ({"ccxt": 0.0006, "yahoo": 0.0003},
     "лимитом: Bybit мейкер 0.02%×2, БКС фьючерсы + узкий спред"),
    ({"ccxt": 0.0015, "yahoo": 0.0005},
     "по рынку: Bybit тейкер 0.055%×2 + проскальзывание, БКС фьючерсы"),
    ({"ccxt": 0.003, "yahoo": 0.001},
     "осторожно: вдвое дороже — тонкая ликвидность, ночь, новости"),
)

# Сценарий «как в бою» для коротких сводок — второй из списка (реальный, не идеал).
LIVE_COSTS = COST_SCENARIOS[2][0]

# Сценарии счёта по умолчанию: (стартовый капитал, риск на сделку в долях).
DEFAULT_ACCOUNTS = ((1000, 0.01), (1000, 0.02), (5000, 0.01))


def trade_r(signal: dict) -> float:
    """Результат сделки в R. Цель даёт +фактический R:R, стоп −1, истёкшая 0.

    Закрытая по неделе считается по фактической цене выхода (её R — любой между
    −1 и +R:R), как и в /stats у бота.
    """
    outcome = signal.get("outcome") or signal.get("status")
    if outcome == "hit_tp":
        return signal["rr"]
    if outcome == "hit_sl":
        return -1.0
    if outcome == "closed_week" and signal.get("exit_price") is not None:
        return trading_week.realized_r(signal["direction"], signal["entry_price"],
                                       signal["stop_loss"], signal["exit_price"])
    return 0.0


def cost_of(signal: dict, costs) -> float:
    """Ставка издержек для этой сделки. costs — число (одна ставка на всех) или
    словарь по источнику данных инструмента: {'ccxt': …, 'yahoo': …}."""
    if not isinstance(costs, dict):
        return costs or 0.0
    return costs.get(data_source(signal.get("instrument", "")) or "", 0.0)


def cost_in_r(signal: dict, costs) -> float:
    """Во сколько R обходятся издержки на этой сделке.

    Издержки заданы долей от ЦЕНЫ, а R — это риск сделки, тоже доля от цены.
    Значит цена сделки в R = издержки ÷ риск. Чем теснее стоп, тем дороже обходится
    каждая сделка: при риске 0.5% комиссия 0.1% стоит 0.2R, а при риске 0.2% — уже 0.5R.
    Это и есть причина, по которой у движка с тесным стопом издержки решают всё.
    """
    cost_frac = cost_of(signal, costs)
    if not cost_frac:
        return 0.0
    risk_frac = signal["risk"] / signal["entry_price"] if signal["entry_price"] else 0.0
    return cost_frac / risk_frac if risk_frac > 0 else 0.0


def chronological(per_instrument: dict[str, list[dict]]) -> list[dict]:
    """Все сделки всех инструментов вперемешку, по времени входа — как в жизни."""
    rows = [{**s, "instrument": code}
            for code, signals in per_instrument.items() for s in signals]
    rows.sort(key=lambda s: pd.Timestamp(s["bar_time"]))
    return rows


def simulate(trades: list[dict], capital: float, risk_frac: float,
             costs=0.0) -> dict:
    """Прогон сделок по счёту: сложный процент, риск risk_frac от текущего капитала.

    Модель намеренно простая и оптимистичная в одном месте: сделки считаются
    последовательно, хотя в жизни несколько позиций бывают открыты одновременно и
    суммарный риск в такие моменты выше заявленного. Реальная просадка от этого
    может оказаться ГЛУБЖЕ расчётной — не мельче.
    """
    equity = capital
    peak = capital
    max_dd = 0.0          # максимальная просадка в долях
    max_dd_money = 0.0
    losses = streak = 0   # текущая и худшая серия убыточных сделок подряд
    wins = 0
    curve = [capital]
    for s in trades:
        r = trade_r(s) - cost_in_r(s, costs)
        equity *= (1 + risk_frac * r)
        curve.append(equity)
        if r > 0:
            wins += 1
            losses = 0
        else:
            losses += 1
            streak = max(streak, losses)
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak else 0.0
        if drawdown > max_dd:
            max_dd, max_dd_money = drawdown, peak - equity
    span_days = 0.0
    if trades:
        first = pd.Timestamp(trades[0]["bar_time"])
        last = pd.Timestamp(trades[-1]["bar_time"])
        span_days = (last - first).total_seconds() / 86400
    months = span_days / 30.44 if span_days else 0.0
    return {
        "n": len(trades),
        "start": capital,
        "final": equity,
        "profit": equity - capital,
        "profit_pct": (equity / capital - 1) if capital else 0.0,
        "max_dd": max_dd,
        "max_dd_money": max_dd_money,
        "worst_streak": streak,
        "winrate": wins / len(trades) if trades else 0.0,
        "per_month": len(trades) / months if months else 0.0,
        "months": months,
        "curve": curve,
        "ruined": equity <= capital * 0.5,   # счёт уполовинен — дальше уже не отыграть
    }


def compare(per_instrument: dict[str, dict[str, list[dict]]], names: list[str],
            column: str, capital: float = 1000, risk_frac: float = 0.01) -> None:
    """Итоговый счёт по каждому варианту при разных издержках — таблица для выбора.

    Зачем отдельно от сравнения в R. Вариант с бо́льшим числом сделок выигрывает по
    сумме R, но платит комиссию за каждую сделку. Пока издержки не учтены, выбор
    систематически смещён в сторону вариантов, которые торгуют чаще, — а на счёте
    они могут оказаться хуже. Здесь видно сразу, при каких издержках вариант ещё жив.
    """
    print("\n" + "═" * 96)
    print(f"  ВЫБОР ВАРИАНТА ПО ДЕНЬГАМ — счёт {capital:,.0f}$, риск {risk_frac:.0%} на сделку")
    print("═" * 96)
    header = "".join(f"{label.split(':')[0]:>13}" for _, label in COST_SCENARIOS)
    print(f"  {column:<20}{'сделок':>8}{'в мес.':>8}" + header + f"{'просадка':>10}")
    print("  " + "─" * 98)
    for name in names:
        trades = chronological({c: r.get(name, []) for c, r in per_instrument.items()})
        if not trades:
            print(f"  {name:<20}{'—':>8}  (сделок нет)")
            continue
        cells = [f"{simulate(trades, capital, risk_frac, costs)['final']:>12,.0f}$"
                 for costs, _ in COST_SCENARIOS]
        base = simulate(trades, capital, risk_frac, LIVE_COSTS)
        print(f"  {name:<20}{base['n']:>8}{base['per_month']:>8.1f}"
              + "".join(cells) + f"{base['max_dd']:>9.0%}")
    print("\n    Колонки — сценарии издержек (расшифровка в money.COST_SCENARIOS).")
    print("    Просадка показана для сценария «по рынку».")
    print("    Вариант, живой только в колонке «идеал» — это не стратегия, а иллюзия.")


def _fmt(res: dict) -> str:
    return (f"{res['final']:>12,.0f}$ {res['profit_pct']:>+8.0%}"
            f"{res['max_dd']:>10.0%} ({res['max_dd_money']:>7,.0f}$)"
            f"{res['worst_streak']:>8}"
            f"{res['winrate']:>9.0%}")


def report(per_instrument: dict[str, list[dict]],
           mids: dict[str, pd.Timestamp] | None = None,
           accounts=DEFAULT_ACCOUNTS) -> None:
    """Отчёт «что было бы со счётом» — по всей истории и по свежей половине."""
    trades = chronological(per_instrument)
    if not trades:
        print("\n  Сделок нет — считать нечего.")
        return

    print("\n" + "═" * 96)
    print("  ЧТО БЫЛО БЫ СО СЧЁТОМ (перевод результата из R в деньги)")
    print("═" * 96)
    base = simulate(trades, 1000, 0.01)
    print(f"  Сделок: {base['n']}, история {base['months']:.1f} мес. "
          f"→ в среднем {base['per_month']:.1f} сделок в месяц.")
    print("  Сделки идут в хронологическом порядке, все инструменты вперемешку.")

    # Из чего складывается счёт по инструментам — видно, кто его тянет и кто топит.
    by_code: dict[str, int] = {}
    for s in trades:
        by_code[s["instrument"]] = by_code.get(s["instrument"], 0) + 1
    print("  Сделок по инструментам: "
          + ", ".join(f"{c} {n}" for c, n in sorted(by_code.items(), key=lambda x: -x[1])))

    for costs, label in COST_SCENARIOS:
        rates = ", ".join(f"{k} {v * 100:g}%" for k, v in costs.items())
        print(f"\n  ИЗДЕРЖКИ: {label}")
        print(f"            (туда-обратно: {rates}; ccxt = крипта, yahoo = золото/нефть)")
        print(f"  {'счёт / риск':<20}{'итог':>13}{'прибыль':>9}"
              f"{'макс. просадка':>20}{'серия −':>8}{'винрейт':>9}")
        print("  " + "─" * 80)
        for capital, risk in accounts:
            res = simulate(trades, capital, risk, costs)
            flag = "  ← счёт уполовинен" if res["ruined"] else ""
            print(f"  {capital:,}$ риск {risk:.0%}".ljust(22) + _fmt(res) + flag)

    if mids:
        second = [s for s in trades
                  if pd.Timestamp(s["bar_time"]) >= mids.get(s["instrument"], pd.Timestamp.min)]
        if second:
            print("\n" + "─" * 96)
            print("  ТО ЖЕ ПО СВЕЖЕЙ ПОЛОВИНЕ ИСТОРИИ (её отбор порогов не видел —")
            print("  это ближе всего к тому, чего ждать дальше)")
            print("─" * 96)
            for costs, label in COST_SCENARIOS:
                res = simulate(second, 1000, 0.01, costs)
                print(f"  1,000$ риск 1%, {label.split(':')[0]:<12}" + _fmt(res))

    print("\n  ⚠️  Это история, а не прогноз. У движка НЕТ доказанного преимущества:")
    print("      на проверочной половине результат задевает ноль (разброс сопоставим")
    print("      с самим результатом). Год данных по крипте и полтора по золоту —")
    print("      мало для сильных утверждений.")
    print("  ⚠️  Просадка в жизни будет ГЛУБЖЕ расчётной: модель считает сделки")
    print("      последовательно, а в реальности несколько позиций открыты разом.")
