"""Фоновые задачи торгового движка: контекстный анализ и мониторинг паттернов.

Использует тот же AsyncIOScheduler, что и простые алерты (см. bot.py). Две задачи:
  • run_analysis  (раз в час) — пересчитывает тренд/уровни/зоны и пишет в БД (levels);
  • monitor_signals (каждые 5 мин) — ищет Spring/Upthrust по свежим H1-свечам, пишет
    в signals и рассылает подписчикам.

Анализируются только КРИПТО-инструменты (есть биржевой объём) из числа тех, на
которые есть хотя бы одна подписка — лишние пары не дёргаем.
"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import analyzer
import config
import data_fetcher
import database
import pattern_detector
from instruments import ccxt_symbol, fmt, infer_decimals, resolve


async def _fetch(code: str, timeframe: str, limit: int):
    sym = ccxt_symbol(code)
    return await data_fetcher.get_candles(sym["symbol"], timeframe, limit, sym["exchange"])


def _subscribed_crypto() -> list[str]:
    """Инструменты с подпиской, по которым есть биржевой объём (CCXT)."""
    return [c for c in database.get_subscribed_instruments() if ccxt_symbol(c)]


async def run_analysis(bot=None) -> None:
    """Контекстный анализ (раз в час): тренд D1 + уровни D1/H1 + зоны ликвидности → БД."""
    codes = _subscribed_crypto()
    print(f"[run_analysis] инструментов к анализу: {len(codes)}")
    for code in codes:
        try:
            d1 = await _fetch(code, config.D1_TIMEFRAME, config.D1_LIMIT)
            h1 = await _fetch(code, config.H1_TIMEFRAME, config.H1_LIMIT)
        except Exception as e:
            print(f"[run_analysis] {code}: ошибка данных: {e}")
            continue
        analyze_and_store(code, d1, h1)


def analyze_and_store(code: str, d1, h1) -> list[dict]:
    """Считает уровни/зоны по D1+H1, сохраняет в БД и возвращает их (для /analyze)."""
    global_levels = analyzer.find_levels(d1, config.D1_PIVOT_WINDOW, "D1")
    local_levels = analyzer.find_levels(h1, config.H1_PIVOT_WINDOW, "H1")
    prioritized = analyzer.prioritize_levels(global_levels, local_levels)
    zones = analyzer.find_liquidity_zones(d1)
    liquidity_levels = [
        {"price": z["price"], "type": "liquidity", "strength": "strong",
         "is_liquidity": 1, "timeframe": "D1"}
        for z in zones
    ]
    database.save_levels(code, prioritized + liquidity_levels)
    print(f"[analysis] {code}: тренд={analyzer.get_trend(d1)}, "
          f"уровней={len(prioritized)}, зон ликвидности={len(zones)}")
    return prioritized


async def monitor_signals(bot) -> None:
    """Каждые 5 минут: ищем Spring/Upthrust по H1 и шлём подписчикам новые сигналы."""
    codes = _subscribed_crypto()
    for code in codes:
        try:
            h1 = await _fetch(code, config.H1_TIMEFRAME, config.H1_LIMIT)
            d1 = await _fetch(code, config.D1_TIMEFRAME, config.D1_LIMIT)
        except Exception as e:
            print(f"[monitor_signals] {code}: ошибка данных: {e}")
            continue
        trend = analyzer.get_trend(d1)
        levels = database.get_levels(code)
        for detector in (pattern_detector.detect_spring, pattern_detector.detect_upthrust):
            signal = detector(h1, levels, trend)
            if signal is None:
                continue
            # Дедуп: тот же паттерн на той же закрытой свече держится до часа —
            # не шлём одинаковый сигнал чаще, чем раз в SIGNAL_DEDUP_MIN минут.
            since = (datetime.now() - timedelta(minutes=config.SIGNAL_DEDUP_MIN)).isoformat(timespec="seconds")
            if database.recent_signal_exists(code, signal["pattern"], signal["direction"], since):
                continue
            database.add_signal(
                code, signal["pattern"], signal["direction"],
                signal["entry_price"], signal["stop_loss"], signal["take_profit"],
                priority=signal["priority"],
            )
            print(f"[monitor_signals] СИГНАЛ {code} {signal['pattern']} {signal['direction']}")
            await _notify(bot, code, signal)


async def _notify(bot, code: str, signal: dict) -> None:
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    arrow = "🟢 ЛОНГ" if signal["direction"] == "long" else "🔴 ШОРТ"
    name = "Spring (пружина)" if signal["pattern"] == "spring" else "Upthrust (зеркало)"
    star = "⭐ " if signal["priority"] == "high" else ""
    text = (
        f"{star}{arrow} — {info['name']}\n"
        f"Паттерн: {name}\n"
        f"Вход: {fmt(signal['entry_price'], d)}\n"
        f"Стоп: {fmt(signal['stop_loss'], d)}\n"
        f"Цель: {fmt(signal['take_profit'], d)}\n\n"
        "Это подсказка, не приказ. Решение и риск — на тебе."
    )
    for user_id in database.get_subscribers(code):
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[monitor_signals] не отправить {user_id}: {e}")


def setup(bot) -> AsyncIOScheduler:
    """Создаёт и запускает планировщик движка (анализ + мониторинг сигналов)."""
    sched = AsyncIOScheduler()
    sched.add_job(run_analysis, "interval", minutes=config.ANALYZE_EVERY_MIN, args=[bot])
    sched.add_job(monitor_signals, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.start()
    return sched
