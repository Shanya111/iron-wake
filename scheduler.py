"""Фоновые задачи бота. Планировщик в проекте ровно один — этот.

Пять задач:
  • run_analysis  (раз в час) — пересчитывает тренд/уровни/зоны и пишет в БД (levels);
  • monitor_signals (каждые 5 мин) — ищет Spring/Upthrust по свежим H1-свечам, пишет
    в signals и рассылает подписчикам;
  • track_signals (каждые 5 мин) — ведёт сигнал по двум ступеням: исполнилась ли
    лимитная заявка, а потом — дошла ли сделка до цели/стопа; сообщает владельцу;
  • track_trades (каждые 5 мин) — исход сделок журнала;
  • check_alerts (каждые 5 мин) — алерты «касание уровня» (правило — в alerts.py).

Анализируются инструменты движка (16: крипта + золото + нефть) из числа подписанных —
лишние пары не дёргаем. Валютные пары в движок не входят, но алерты и журнал по ним
работают: у них есть биржевой источник, просто сигналов по ним нет.

Источник данных в боте ровно один — БИРЖА BingX: и движок, и журнал сделок,
и алерты берут свечи через fetch_candles. Yahoo убран 26 августа 2026.
"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import alerts
import analyzer
import config
import data_fetcher
import database
import llm
import pattern_detector
from instruments import ccxt_symbol, engine_codes, fmt, infer_decimals, resolve, short


async def fetch_candles(code: str, timeframe: str, limit: int):
    """Свечи инструмента с биржи: DataFrame OHLCV с UTC-индексом.

    Работает и по реестровому коду (BTC, GOLD…), и по своей паре — она хранится
    сразу символом контракта («WIF/USDT:USDT»), см. instruments.ccxt_symbol. Источник
    один на весь бот, поэтому и ветка одна: развилку по data_source убрали вместе
    с Yahoo 26 августа 2026.

    ValueError — если источника нет. Так выглядят старые записи журнала с тикерами
    Yahoo: читаться они читаются, а вот вести их больше не по чему.
    """
    sym = ccxt_symbol(code)
    if sym is None:
        raise ValueError(f"нет биржевого источника для {code}")
    return await data_fetcher.get_candles(sym["symbol"], timeframe, limit, sym["exchange"])


def _subscribed_engine() -> list[str]:
    """Инструменты с подпиской, входящие в движок.

    Спрашиваем именно engine_codes(), а не «есть ли источник данных»: с 29 августа
    2026 у валютных пар источник есть (он нужен алертам и журналу), но в движок они
    не входят. Проверка по источнику продолжила бы их сканировать.

    Старые подписки на форекс в базе от этого не ломаются — они просто перестают
    давать сигналы."""
    engine = set(engine_codes())
    return [c for c in database.get_subscribed_instruments() if c in engine]


async def run_analysis(bot=None) -> None:
    """Контекстный анализ (раз в час): тренд D1 + уровни D1/H1 + зоны ликвидности → БД."""
    codes = _subscribed_engine()
    print(f"[run_analysis] инструментов к анализу: {len(codes)}")
    for code in codes:
        try:
            d1 = await fetch_candles(code, config.D1_TIMEFRAME, config.D1_LIMIT)
            h1 = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
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
            h1 = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
            d1 = await fetch_candles(code, config.D1_TIMEFRAME, config.D1_LIMIT)
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
                sig_id = database.add_signal(
                    code, signal["pattern"], signal["direction"],
                    signal["entry_price"], signal["stop_loss"], signal["take_profit"],
                    priority=signal["priority"], bar_time=signal.get("bar_time"),
                    user_id=user_id, signal_price=signal.get("signal_price"),
                )
                # При нулевом откате заявка стоит по цене закрытия, то есть там, где
                # рынок и так стоит: ждать нечего, сделка открыта. Помечаем сразу, иначе
                # трекинг стал бы гадать по часовым свечам, успела ли заявка исполниться
                # раньше, чем цена ушла к цели, — и с близкой целью отвечал бы «не
                # успела» по сделкам, которые открылись.
                if not config.ENTRY_PULLBACK:
                    database.mark_signal_filled(sig_id, signal.get("bar_time"))
                print(f"[monitor_signals] СИГНАЛ {code} {signal['pattern']} {signal['direction']} → {user_id}")
                key = (signal["pattern"], signal["direction"], round(signal["take_profit"], 10))
                if key not in comment_cache:
                    comment_cache[key] = await _signal_comment(code, signal, trend)
                await _notify(bot, code, signal, user_id, comment_cache[key])


async def track_signals(bot) -> None:
    """Каждые N минут: ведём сигнал по двум ступеням — сначала ВХОД, потом ИСХОД.

    При НУЛЕВОМ откате (ENTRY_PULLBACK = 0, как сейчас) первой ступени фактически нет:
    сигнал рождается уже исполненным, см. monitor_signals. Ступень 1 остаётся рабочей для
    сигналов, созданных при ненулевом откате, и для старых записей в базе.

    Вход лимитной заявкой, поэтому при ненулевом откате сделки может не случиться вовсе:
      1. status='waiting_fill' — дошла ли цена до заявки (pattern_detector.evaluate_fill).
         Не дошла за ENTRY_WAIT_BARS часов или ушла к цели без нас → 'expired_unfilled',
         сделки не было. Дошла → 'filled', запоминаем момент исполнения.
      2. status='filled' — куда дошла цена ПОСЛЕ входа: цель, стоп или истечение.
         Отсчёт идёт от fill_time, а не от свечи пробоя: между сигналом и входом
         проходит до нескольких часов, и приписывать себе то, что случилось до входа,
         нельзя.
    Свечи берём по разу на инструмент (кеш H1 общий с monitor_signals, так что
    лишних запросов к бирже нет).
    """
    open_signals = database.get_open_signals()
    if not open_signals:
        return
    candles: dict[str, object] = {}
    for code in {s["instrument"] for s in open_signals}:
        try:
            candles[code] = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
        except Exception as e:
            print(f"[track_signals] {code}: ошибка данных: {e}")
    for s in open_signals:
        df = candles.get(s["instrument"])
        if df is None:
            continue
        # Ступень 1 — исполнение заявки.
        if s.get("status") == "waiting_fill":
            fill = pattern_detector.evaluate_fill(s, df)
            if fill["status"] == "waiting_fill":
                continue
            if fill["status"] == "expired_unfilled":
                s["fill_reason"] = fill.get("reason")
                database.update_signal_status(s["id"], "expired_unfilled")
                print(f"[track_signals] {s['instrument']} #{s['id']} → заявка не исполнена")
                await _notify_unfilled(bot, s)
                continue
            database.mark_signal_filled(s["id"], fill["fill_time"])
            s["fill_time"] = fill["fill_time"]
            print(f"[track_signals] {s['instrument']} #{s['id']} → вход состоялся")
            await _notify_filled(bot, s)
        # Ступень 2 — исход уже открытой сделки.
        outcome = pattern_detector.evaluate_signal(s, df)
        if outcome == "pending":
            continue
        database.update_signal_status(s["id"], outcome)
        print(f"[track_signals] {s['instrument']} #{s['id']} → {outcome}")
        if outcome in ("hit_tp", "hit_sl"):
            await _notify_outcome(bot, s, outcome)


async def _send_to_owner(bot, signal: dict, text: str) -> None:
    """Шлёт текст владельцу сигнала. Старые «общие» сигналы (user_id NULL, до
    перехода на персональные) уходят всем текущим подписчикам, как раньше."""
    owner = signal.get("user_id")
    recipients = [owner] if owner else database.get_subscribers(signal["instrument"])
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[track_signals] не отправить {user_id}: {e}")


async def _notify_unfilled(bot, signal: dict) -> None:
    """Заявка не исполнилась — сделки не было. Сказать об этом надо: иначе заявка
    так и провисит в терминале и однажды сработает не вовремя."""
    info = resolve(signal["instrument"])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    # Причина важна: «ушла к цели без нас» и «не дошла за N часов» — разные события.
    # При заявке по закрытию свечи (ENTRY_PULLBACK = 0) преобладает первое.
    if signal.get("fill_reason") == "target":
        why = (f"Цена ушла к цели, не задев {fmt(signal['entry_price'], d)}. "
               "Сделки не было — движение случилось без нас.")
    elif signal.get("fill_reason") == "stale":
        why = (f"Заявка по {fmt(signal['entry_price'], d)} протухла: я не видел свечей "
               "с момента сигнала. Сделки не было.")
    else:
        why = (f"Цена так и не дошла до {fmt(signal['entry_price'], d)} "
               f"за {config.ENTRY_WAIT_BARS} ч. Сделки не было.")
    await _send_to_owner(bot, signal, (
        f"⏹ Заявка снята — {info['short']}\n"
        f"{why}\n"
        "Если заявка ещё стоит в терминале — сними её."
    ))


async def _notify_filled(bot, signal: dict) -> None:
    """Заявка исполнилась — сделка открыта, дальше ведём её до цели или стопа."""
    info = resolve(signal["instrument"])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    arrow = "🟢 ЛОНГ" if signal["direction"] == "long" else "🔴 ШОРТ"
    await _send_to_owner(bot, signal, (
        f"▶️ Заявка исполнена — {info['short']} ({arrow})\n"
        f"Вход {fmt(signal['entry_price'], d)}, "
        f"стоп {fmt(signal['stop_loss'], d)}, цель {fmt(signal['take_profit'], d)}.\n"
        "Дальше веду её сам — сообщу, когда дойдёт до цели или стопа."
    ))


async def track_trades(bot) -> None:
    """Каждые N минут: проверяем сделки журнала — дошли ли до цели/стопа.

    Свечи — BingX H1 для всех: и для реестровых инструментов (общий кеш с сигналами),
    и для своей пары, которая теперь хранится символом контракта. Журнал НЕ истекает:
    'expired' трактуем как 'ещё открыта' — держим до цели/стопа/ручного закрытия.
    Внутри свечи при двусмысленности pattern_detector считает стоп раньше.

    Сделка по инструменту без биржевого источника (запись с тикером Yahoo, оставшаяся
    в журнале с прежних времён) просто не ведётся: в лог уйдёт строка, сама сделка
    останется открытой и закрывается кнопкой в /trades.
    """
    trades = database.get_open_trades()
    if not trades:
        return
    candles: dict[str, object] = {}
    for code in {t["instrument"] for t in trades}:
        try:
            candles[code] = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
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
        f"{info['short']} ({arrow})\n"
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
        f"{head} — {info['short']} ({arrow})\n"
        f"Вход был {fmt(signal['entry_price'], d)}, цена дошла до {fmt(price, d)}.\n\n"
        "Это итог подсказки, не финсовет."
    )
    # Сигнал персональный → исход шлём его владельцу (см. _send_to_owner).
    await _send_to_owner(bot, signal, text)


async def _signal_comment(code: str, signal: dict, trend: str) -> str | None:
    """1–2 предложения контекста к сигналу от LLM: тренд + сила уровня + стакан.
    Стакан тянем здесь (раз на сигнал, кеш 30 сек). Любая осечка → None (сигнал
    уйдёт без комментария)."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(signal["entry_price"])
    dom = ""
    sym = ccxt_symbol(code)
    if sym:  # стакан есть у всех инструментов движка (BingX), включая золото и нефть
        try:
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
        f"Инструмент: {info['short']}\n"
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
    # Вход — ЛИМИТНОЙ заявкой, поэтому сообщение обязано называть три вещи: по какой
    # цене ставить заявку, сколько она живёт и что бывает, если не исполнится.
    # Без этого пользователь по привычке войдёт по рынку и заплатит тейкера — то
    # есть ровно ту разницу, ради которой всё и делалось.
    side = "покупку" if signal["direction"] == "long" else "продажу"
    # Строку «сигнал был по …» показываем, только когда заявка ДЕЙСТВИТЕЛЬНО стоит
    # не по закрытию (ENTRY_PULLBACK > 0). Иначе она повторяла бы ту же цену дважды.
    sig_price = signal.get("signal_price")
    was = ""
    if sig_price and abs(sig_price - signal["entry_price"]) > 1e-12:
        was = f"Сигнал был по {fmt(sig_price, d)} — заявка ставится ближе к уровню.\n"
    # Про ожидание заявки пишем, только если она ДЕЙСТВИТЕЛЬНО ждёт (ENTRY_PULLBACK > 0).
    # При откате 0 заявка стоит там, где цена, и исполняется сразу — обещание «напишу,
    # если не исполнится» было бы про случай, которого не бывает.
    wait = ""
    if config.ENTRY_PULLBACK:
        wait = (f"⏳ Заявка живёт {config.ENTRY_WAIT_BARS} ч. Не исполнится — сделки нет, "
                "я об этом напишу.\n")
    text = (
        f"{star}{arrow} — {info['short']}\n"
        f"Паттерн: {name}\n"
        f"📥 ЛИМИТНАЯ заявка на {side}: {fmt(signal['entry_price'], d)}\n"
        f"Стоп: {fmt(signal['stop_loss'], d)}\n"
        f"Цель: {fmt(signal['take_profit'], d)}\n"
        f"Профит/риск: 1:{rr:.1f}\n"
        f"{was}{wait}\n"
        "Это подсказка, не приказ. Решение и риск — на тебе."
    )
    if comment:
        text += f"\n\n🤖 {comment}"
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        print(f"[monitor_signals] не отправить {user_id}: {e}")


async def alert_window(pair: str) -> dict:
    """Куда цена заходила за последние минуты: {low, high, last, decimals}.

    Источник — тот же, что у уровней в /analyze: минутные свечи БИРЖИ. Это и было
    главным при возврате алертов — считать уровень по одному графику, а касание
    проверять по другому нельзя. С уходом Yahoo развилка исчезла совсем: своя пара
    тоже биржевая, просто контракт не из реестра.
    """
    info = resolve(pair)
    df = await fetch_candles(pair, config.ALERT_TIMEFRAME, config.ALERT_LOOKBACK)
    window = alerts.window_from_candles(df.tail(config.ALERT_LOOKBACK))
    window["decimals"] = (info["decimals"] if info["decimals"] is not None
                          else infer_decimals(window["last"]))
    return window


async def check_alerts(bot) -> None:
    """Алерты «касание уровня» (каждые 5 минут): дошла ли цена до уровня пользователя.

    По одному запросу цены на инструмент за цикл, а не на алерт: на одной паре у разных
    людей могут стоять свои уровни. Правило срабатывания — alerts.hit (диапазон свечей,
    а не одна точка), поэтому касание фитилём между проверками не теряется.
    """
    pending = database.get_pending_alerts()
    print(f"[check_alerts] активных алертов: {len(pending)}")
    if not pending:
        return

    windows: dict[str, dict] = {}
    for pair in {a["pair"] for a in pending}:
        try:
            windows[pair] = await alert_window(pair)
        except Exception as e:
            print(f"[check_alerts] {pair}: цену не получили — {e}")

    for a in pending:
        window = windows.get(a["pair"])
        if window is None:
            continue  # по этой паре цены в этом цикле нет — ждём следующего
        info = resolve(a["pair"])
        low, high, last, d = window["low"], window["high"], window["last"], window["decimals"]

        # Первая проверка ВЗВОДИТ алерт: запоминаем сторону цены и в этом цикле не
        # срабатываем. Это не задержка ради задержки — окно свечей смотрит назад, и без
        # такого шага свежий алерт сработал бы на диапазоне, который был ДО его
        # постановки. Цена расплаты — касание в первые 5 минут жизни алерта не поймается.
        if a["start_above"] is None:
            database.set_alert_side(a["id"], alerts.side_of(last, a["threshold"]))
            print(f"  • взведён id={a['id']} {info['short']} {fmt(a['threshold'], d)}")
            continue

        if not alerts.hit(low, high, last, a["threshold"], a["start_above"]):
            continue

        print(f"  [!] СРАБОТАЛ id={a['id']} user={a['user_id']} {info['short']} "
              f"{fmt(a['threshold'], d)} диапазон=[{fmt(low, d)}; {fmt(high, d)}]")
        database.mark_alert_triggered(a["id"])
        try:
            await bot.send_message(
                a["user_id"],
                f"🔔 {info['short']} дошёл до твоего уровня {fmt(a['threshold'], d)}.\n"
                f"Сейчас {fmt(last, d)} (за последние минуты ходил "
                f"{fmt(low, d)}–{fmt(high, d)}).\n"
                "Алерт снят. Поставить новый — /alert, список — /myalerts."
            )
        except Exception as e:
            print(f"[check_alerts] не отправилось {a['user_id']}: {e}")


def setup(bot) -> AsyncIOScheduler:
    """Создаёт и запускает единственный планировщик бота (пять задач, см. модуль)."""
    sched = AsyncIOScheduler()
    sched.add_job(run_analysis, "interval", minutes=config.ANALYZE_EVERY_MIN, args=[bot])
    sched.add_job(monitor_signals, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.add_job(track_signals, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.add_job(track_trades, "interval", minutes=config.MONITOR_EVERY_MIN, args=[bot])
    sched.add_job(check_alerts, "interval", minutes=config.ALERT_EVERY_MIN, args=[bot])
    sched.start()
    return sched
