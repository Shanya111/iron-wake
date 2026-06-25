"""Фоновые задачи торгового движка: контекстный анализ и мониторинг паттернов.

Использует тот же AsyncIOScheduler, что и простые алерты (см. bot.py). Три задачи:
  • run_analysis  (раз в час) — пересчитывает тренд/уровни/зоны и пишет в БД (levels);
  • monitor_signals (каждые 5 мин) — ищет Spring/Upthrust по свежим H1-свечам, пишет
    в signals и рассылает подписчикам;
  • track_signals (каждые 5 мин) — следит за исходом открытых сигналов (дошёл до
    цели/стопа/истёк) и сообщает подписчикам результат.

Анализируются только инструменты с биржевым объёмом (крипта + форекс через Kraken)
из числа тех, на которые есть хотя бы одна подписка — лишние пары не дёргаем.
"""

import asyncio
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import analyzer
import config
import data_fetcher
import database
import llm
import pattern_detector
from instruments import ccxt_symbol, fmt, infer_decimals, resolve


async def _fetch(code: str, timeframe: str, limit: int):
    sym = ccxt_symbol(code)
    return await data_fetcher.get_candles(sym["symbol"], timeframe, limit, sym["exchange"])


def _subscribed_engine() -> list[str]:
    """Инструменты с подпиской, по которым есть биржевой объём (CCXT)."""
    return [c for c in database.get_subscribed_instruments() if ccxt_symbol(c)]


async def run_analysis(bot=None) -> None:
    """Контекстный анализ (раз в час): тренд D1 + уровни D1/H1 + зоны ликвидности → БД."""
    codes = _subscribed_engine()
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
    """Каждые 5 минут: ищем Spring/Upthrust по H1 и шлём новые сигналы.

    Сигналы теперь персональные: по каждому инструменту прогоняем детект отдельно
    для каждого подписчика — с его личными порогами (config.effective поверх его
    user_settings). Дедуп и трекинг исхода тоже идут по конкретному пользователю.
    Свечи/уровни/тренд считаются один раз на инструмент (детект — чистый CPU по кешу).
    """
    codes = _subscribed_engine()
    for code in codes:
        subscribers = database.get_subscribers(code)
        if not subscribers:
            continue
        try:
            h1 = await _fetch(code, config.H1_TIMEFRAME, config.H1_LIMIT)
            d1 = await _fetch(code, config.D1_TIMEFRAME, config.D1_LIMIT)
        except Exception as e:
            print(f"[monitor_signals] {code}: ошибка данных: {e}")
            continue
        trend = analyzer.get_trend(d1)
        levels = database.get_levels(code)
        # Комментарий LLM считаем один раз на одинаковый сигнал в цикле (а не на каждого
        # подписчика): ключ — паттерн+направление+цель (цель зависит от личного R:R).
        comment_cache: dict[tuple, str | None] = {}
        for user_id in subscribers:
            settings = config.effective(database.get_user_settings(user_id))
            for detector in (pattern_detector.detect_spring, pattern_detector.detect_upthrust):
                signal = detector(h1, levels, trend, settings)
                if signal is None:
                    continue
                # Дедуп персональный: тот же паттерн тому же пользователю не чаще,
                # чем раз в SIGNAL_DEDUP_MIN минут.
                since = (datetime.now() - timedelta(minutes=config.SIGNAL_DEDUP_MIN)).isoformat(timespec="seconds")
                if database.recent_signal_exists(code, signal["pattern"], signal["direction"], since, user_id):
                    continue
                database.add_signal(
                    code, signal["pattern"], signal["direction"],
                    signal["entry_price"], signal["stop_loss"], signal["take_profit"],
                    priority=signal["priority"], bar_time=signal.get("bar_time"),
                    user_id=user_id,
                )
                print(f"[monitor_signals] СИГНАЛ {code} {signal['pattern']} {signal['direction']} → {user_id}")
                key = (signal["pattern"], signal["direction"], round(signal["take_profit"], 10))
                if key not in comment_cache:
                    comment_cache[key] = await _signal_comment(code, signal, trend)
                await _notify(bot, code, signal, user_id, comment_cache[key])


async def track_signals(bot) -> None:
    """Каждые N минут: смотрим, дошли ли открытые сигналы до цели/стопа, и
    сообщаем подписчикам исход. Свечи берём по разу на инструмент (кеш H1 общий
    с monitor_signals, так что лишних запросов к бирже нет)."""
    open_signals = database.get_open_signals()
    if not open_signals:
        return
    candles: dict[str, object] = {}
    for code in {s["instrument"] for s in open_signals}:
        try:
            candles[code] = await _fetch(code, config.H1_TIMEFRAME, config.H1_LIMIT)
        except Exception as e:
            print(f"[track_signals] {code}: ошибка данных: {e}")
    for s in open_signals:
        df = candles.get(s["instrument"])
        if df is None:
            continue
        outcome = pattern_detector.evaluate_signal(s, df)
        if outcome == "pending":
            continue
        database.update_signal_status(s["id"], outcome)
        print(f"[track_signals] {s['instrument']} #{s['id']} → {outcome}")
        if outcome in ("hit_tp", "hit_sl"):
            await _notify_outcome(bot, s, outcome)


async def track_trades(bot) -> None:
    """Каждые N минут: проверяем сделки журнала — дошли ли до цели/стопа.

    Источник свечей по инструменту: движковые (BTC, EUR/USD…) — Kraken H1 (общий кеш
    с сигналами), остальные (золото, нефть, своя пара) — часовые свечи Yahoo. Журнал
    НЕ истекает: 'expired' трактуем как 'ещё открыта' — держим до цели/стопа/ручного
    закрытия. Внутри свечи при двусмысленности pattern_detector считает стоп раньше.
    """
    trades = database.get_open_trades()
    if not trades:
        return
    candles: dict[str, object] = {}
    for code in {t["instrument"] for t in trades}:
        try:
            if ccxt_symbol(code):
                candles[code] = await _fetch(code, config.H1_TIMEFRAME, config.H1_LIMIT)
            else:
                ticker = resolve(code)["ticker"]
                candles[code] = await asyncio.to_thread(database.get_hourly_candles, ticker)
        except Exception as e:
            print(f"[track_trades] {code}: ошибка данных: {e}")
    for t in trades:
        df = candles.get(t["instrument"])
        if df is None:
            continue
        # Переходник под pattern_detector.evaluate_signal (он ждёт stop_loss/take_profit).
        probe = {
            "direction": t["direction"], "stop_loss": t["stop_loss"],
            "take_profit": t["take_profit"], "bar_time": t["bar_time"],
        }
        outcome = pattern_detector.evaluate_signal(probe, df)
        if outcome in ("pending", "expired"):
            continue  # журнал не истекает — оставляем открытой
        database.update_trade_status(t["id"], outcome)
        print(f"[track_trades] {t['instrument']} сделка #{t['id']} → {outcome}")
        await _notify_trade_outcome(bot, t, outcome)


async def _notify_trade_outcome(bot, trade: dict, outcome: str) -> None:
    info = resolve(trade["instrument"])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(trade["entry_price"])
    arrow = "🟢 ЛОНГ" if trade["direction"] == "long" else "🔴 ШОРТ"
    if outcome == "hit_tp":
        head, price = "✅ Цель достигнута", trade["take_profit"]
    else:
        head, price = "🛑 Сработал стоп", trade["stop_loss"]
    text = (
        f"📒 Сделка из журнала — {head}\n"
        f"{info['name']} ({arrow})\n"
        f"Вход был {fmt(trade['entry_price'], d)}, цена дошла до {fmt(price, d)}.\n\n"
        "Журнал ведётся для статистики, это не финсовет."
    )
    try:
        await bot.send_message(trade["user_id"], text)
    except Exception as e:
        print(f"[track_trades] не отправить {trade['user_id']}: {e}")


async def _notify_outcome(bot, signal: dict, outcome: str) -> None:
    info = resolve(signal["instrument"])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    arrow = "🟢 ЛОНГ" if signal["direction"] == "long" else "🔴 ШОРТ"
    if outcome == "hit_tp":
        head, price = "✅ Цель достигнута", signal["take_profit"]
    else:
        head, price = "🛑 Сработал стоп", signal["stop_loss"]
    text = (
        f"{head} — {info['name']} ({arrow})\n"
        f"Вход был {fmt(signal['entry_price'], d)}, цена дошла до {fmt(price, d)}.\n\n"
        "Это итог подсказки, не финсовет."
    )
    # Сигнал персональный → исход шлём его владельцу. Старые «общие» сигналы (до
    # перехода, user_id отсутствует/NULL) — всем текущим подписчикам, как раньше.
    owner = signal.get("user_id")
    recipients = [owner] if owner else database.get_subscribers(signal["instrument"])
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[track_signals] не отправить {user_id}: {e}")


async def _signal_comment(code: str, signal: dict, trend: str) -> str | None:
    """1–2 предложения контекста к сигналу от LLM: тренд + сила уровня + стакан.
    Стакан тянем здесь (раз на сигнал, кеш 30 сек). Любая осечка → None (сигнал
    уйдёт без комментария)."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    dom = ""
    try:
        sym = ccxt_symbol(code)
        ob = analyzer.analyze_order_book(
            await data_fetcher.get_order_book(sym["symbol"], exchange=sym["exchange"])
        )
        if ob:
            pr = {"buyers": "перевес покупателей", "sellers": "перевес продавцов",
                  "balance": "баланс сил"}[ob["pressure"]]
            dom = f"Стакан: {pr} (дисбаланс {ob['imbalance'] * 100:+.0f}%).\n"
    except Exception:
        dom = ""
    trend_ru = {"up": "восходящий", "down": "нисходящий", "sideways": "боковик"}[trend]
    strength = "сильный (часовой совпал с дневным)" if signal["priority"] == "high" else "обычный"
    pat = ("Spring — ложный пробой поддержки вниз с возвратом (лонг)"
           if signal["pattern"] == "spring"
           else "Upthrust — ложный пробой сопротивления вверх с возвратом (шорт)")
    summary = (
        f"Инструмент: {info['name']}\n"
        f"Паттерн: {pat}\n"
        f"Тренд D1: {trend_ru}\n"
        f"Сила пробитого уровня: {strength}\n"
        f"Вход {fmt(signal['entry_price'], d)}, стоп {fmt(signal['stop_loss'], d)}, "
        f"цель {fmt(signal['take_profit'], d)}.\n"
        f"{dom}"
    )
    return await llm.comment_on_signal(summary)


async def _notify(bot, code: str, signal: dict, user_id: int, comment: str | None) -> None:
    """Шлёт персональный сигнал одному подписчику. comment — готовый AI-комментарий
    (считается один раз на одинаковый сигнал в monitor_signals, см. comment_cache)."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    arrow = "🟢 ЛОНГ" if signal["direction"] == "long" else "🔴 ШОРТ"
    name = "Spring (пружина)" if signal["pattern"] == "spring" else "Upthrust (зеркало)"
    star = "⭐ " if signal["priority"] == "high" else ""
    risk = abs(signal["entry_price"] - signal["stop_loss"])
    reward = abs(signal["take_profit"] - signal["entry_price"])
    rr = reward / risk if risk else 0
    text = (
        f"{star}{arrow} — {info['name']}\n"
        f"Паттерн: {name}\n"
        f"Вход: {fmt(signal['entry_price'], d)}\n"
        f"Стоп: {fmt(signal['stop_loss'], d)}\n"
        f"Цель: {fmt(signal['take_profit'], d)}\n"
        f"Профит/риск: 1:{rr:.1f}\n\n"
        "Это подсказка, не приказ. Решение и риск — на тебе."
    )
    if comment:
        text += f"\n\n🤖 {comment}"
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        print(f"[monitor_signals] не отправить {user_id}: {e}")


def setup(bot) -> AsyncIOScheduler:
    """Создаёт и запускает планировщик движка (анализ + мониторинг сигналов)."""
    sched = AsyncIOScheduler()
    sched.add_job(run_analysis, "interval", minutes=config.ANALYZE_EVERY_MIN, args=[bot])
    sched.add_job(monitor_signals, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.add_job(track_signals, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.add_job(track_trades, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.start()
    return sched
