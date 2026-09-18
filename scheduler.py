"""Фоновые задачи бота. Планировщик в проекте ровно один — этот.

Семь задач:
  • run_analysis  (раз в час) — пересчитывает тренд/уровни/зоны и пишет в БД (levels);
  • monitor_signals (каждые 5 мин) — ищет Spring/Upthrust по свежим H1-свечам, пишет
    в signals и рассылает подписчикам;
  • monitor_trend (каждые 5 мин) — стратегия №4, тренд по недельному каналу: ведёт
    общую позицию модели по всем инструментам движка (правила — trend.py);
  • monitor_breakout (каждые 5 мин) — стратегия №5, пробой сильного уровня: считает
    сигналы и ведёт их исходы, но НИКОМУ НЕ ШЛЁТ, пока config.BREAKOUT_SIGNALS =
    False (слежка, решение владельца 17.09.2026; правила — breakout.py, сводка —
    команда /breakout);
  • track_signals (каждые 5 мин) — ведёт сигнал по двум ступеням: исполнилась ли
    лимитная заявка, а потом — дошла ли сделка до цели/стопа; сообщает владельцу;
  • track_trades (каждые 5 мин) — исход сделок журнала;
  • check_alerts (каждые 5 мин) — алерты «касание уровня» (правило — в alerts.py).

Первые две (run_analysis и monitor_signals) ставятся только при config.SPRING_SIGNALS,
состав задач решает jobs(). С 15 сентября 2026 ложный пробой шлёт сигналы по правилам
23 июня (spring_june, выбор — spring_rules). Кому слать, решает подписка в /subscribe:
она на пару «инструмент + стратегия» (database.get_subscribers).

Анализируются инструменты движка (все 21 из реестра: крипта, золото, нефть и пять
валютных пар) из числа подписанных — лишние пары не дёргаем. Форекс вернулся в движок
3 сентября 2026; вне форекс-сессии биржа держит его контракты на паузе, и запрос
свечей падает — задачи ловят ошибку по инструменту и идут дальше.

Источник данных в боте ровно один — БИРЖА BingX: и движок, и журнал сделок,
и алерты берут свечи через fetch_candles. Yahoo убран 26 августа 2026.
"""

import math
from datetime import datetime, timedelta

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import alerts
import analyzer
import breakout
import config
import data_fetcher
import database
import ict
import llm
import pattern_detector
import spring_june
import trend as channel_trend  # стратегия №4; имя «trend» занято трендом дневки в monitor_signals
from instruments import (asset_class, ccxt_symbol, engine_codes, fmt, infer_decimals,
                         resolve, short)


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


async def engine_candles(code: str):
    """Свечи, по которым движок ищет сигнал.

    При config.ROLLING_HOUR это СКОЛЬЗЯЩИЙ ЧАС, собранный из пятнадцатиминуток:
    свеча той же длины, но пересчитанная заново каждые 15 минут, а не раз в час.
    Иначе — обычные часовые свечи биржи, как было до 3 сентября 2026.

    Уровни и тренд считаются НЕ отсюда, а по обычным часовым и дневным свечам
    (run_analysis). Так и мерилось: отдельный арм замера гонял скользящий час с
    уровнями с календарного часа и совпал с основным. Держать уровни на круглом
    часе проще и честнее по отношению к человеку — на графике он видит именно их.
    """
    if not config.ROLLING_HOUR:
        return await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
    m15 = await fetch_candles(code, config.M15_TIMEFRAME, config.M15_LIMIT)
    return analyzer.rolling_hours(m15, config.H1_LIMIT)


def _subscribed_engine(strategy: str = "spring") -> list[str]:
    """Инструменты с подпиской, входящие в движок.

    Спрашиваем engine_codes(), а не «есть ли источник данных», хотя с 3 сентября
    2026 это одно и то же: форекс вернули в движок, и других условий там не
    осталось. Вопрос всё равно задаём составом движка — состав может снова
    измениться, а подписки в базе живут дольше любого решения о нём.

    Своя пара сюда не попадает никогда: подписки ставятся только по реестру.

    strategy — чьи подписки спрашиваем: ложного пробоя (по умолчанию) или ICT.
    Стратегии живут в одном движке, но подписки у них раздельные с 15.09.2026."""
    engine = set(engine_codes())
    return [c for c in database.get_subscribed_instruments(strategy) if c in engine]


def spring_rules():
    """Модуль с правилами ложного пробоя: по нему бот шлёт сигналы и строит /analyze.

    config.SPRING_ENGINE = "june23" — редакция 23 июня (spring_june), любое другое
    значение — сентябрьская (pattern_detector). У обоих модулей одинаковые
    detect_spring / detect_upthrust / explain, поэтому выбирается модуль целиком:
    сигнал и разбор не могут оказаться по разным правилам.
    """
    return spring_june if config.SPRING_ENGINE == "june23" else pattern_detector


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
    # Тренд D1 печатается СПРАВОЧНО: направление движок с 16.09.2026 берёт с
    # 6-часовых свечей (analyzer.engine_trend), а эта задача считает только уровни.
    print(f"[analysis] {code}: тренд D1 (справочно)={analyzer.get_trend(d1)}, "
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
    rules = spring_rules()
    for code in codes:
        # Только те, кто подписан на этот инструмент по ложному пробою.
        subscribers = database.get_subscribers(code, "spring")
        if not subscribers:
            continue
        try:
            h1 = await engine_candles(code)
            # Направление — по 6-часовым свечам (с 16.09.2026), поэтому дневные свечи
            # тут больше не нужны: уровни приходят готовыми из таблицы levels.
            h1_trend = await fetch_candles(code, config.H1_TIMEFRAME, config.TREND_TF_H1_LIMIT)
        except Exception as e:
            print(f"[monitor_signals] {code}: ошибка данных: {e}")
            continue
        if len(h1) < config.VOL_LOOKBACK + 3:
            continue
        trend = analyzer.engine_trend(h1_trend)
        levels = database.get_levels(code)
        # Комментарий LLM считаем один раз на одинаковый сигнал в цикле (а не на каждого
        # подписчика): ключ — паттерн+направление+цель (цель зависит от личного R:R).
        comment_cache: dict[tuple, str | None] = {}
        # Минимальная цель зависит от РЫНКА, а не от пользователя: у крипты один риск,
        # у валюты полриска, у золота с нефтью правила нет (config.JUNE_MIN_TP_R).
        min_tp_r = config.JUNE_MIN_TP_R.get(asset_class(code), 0.0)
        for user_id in subscribers:
            settings = {**config.effective(database.get_user_settings(user_id)),
                        "MIN_TP_R": min_tp_r}
            for detector in (rules.detect_spring, rules.detect_upthrust):
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


def _tracked(signal: dict) -> dict:
    """Сигнал с якорем, пригодным для трекинга ПО ПЯТНАДЦАТИМИНУТКАМ.

    evaluate_signal берёт свечи строго правее якоря. На часовых свечах якорем
    служит время открытия сигнальной свечи (bar_time) — и это в точности даёт
    «свечи после входа», потому что вход у движка по её закрытию.

    На M15 то же самое правило звучит так: якорь — открытие ПОСЛЕДНЕЙ
    пятнадцатиминутки сигнальной свечи, то есть bar_time + 45 минут. Свечи правее
    него начинаются ровно в момент входа. Без этой поправки трекинг заглянул бы
    внутрь самой сигнальной свечи и записал бы её собственный ход как исход
    сделки — а там по построению лежит и прокол уровня, и возврат.

    Правило одно на все записи, включая старые: у них bar_time на круглом часе,
    и bar_time + 45 минут даёт ту же точку отсчёта, что и раньше. Настоящее
    время исполнения (ненулевой откат) якорь не двигает назад — берём позднее.
    """
    if not config.ROLLING_HOUR:
        return signal
    bar = signal.get("bar_time")
    if not bar:
        return signal
    floor = pd.Timestamp(bar) + timedelta(minutes=45)
    fill = signal.get("fill_time")
    anchor = floor
    if fill:
        # Сравнивать «с часовым поясом» и «без» pandas не умеет и падает. Такой
        # записи взяться неоткуда (оба поля пишутся из одного индекса свечей), но
        # ронять из-за неё весь трекинг нельзя: берём заведомо верный bar_time.
        try:
            anchor = max(pd.Timestamp(fill), floor)
        except TypeError:
            pass
    return {**signal, "fill_time": str(anchor)}


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
    Свечи берём по разу на инструмент, из общего кеша с monitor_signals, так что
    лишних запросов к бирже нет.

    ИСХОД СЧИТАЕТСЯ ПО ПЯТНАДЦАТИМИНУТКАМ (с 3 сентября 2026, вместе со
    скользящим часом). Это не украшение, а необходимость: сигнальная свеча теперь
    может кончаться на :15, и по ЧАСОВЫМ свечам первая же свеча «после входа»
    захватила бы кусок ДО него — то есть приписала бы сделке движение, которого
    она не застала. На M15 такой ошибки нет, а заодно вчетверо уменьшается
    неоднозначность «что было раньше внутри бара, стоп или цель».

    Ступень 1 осталась на ЧАСОВЫХ свечах: она работает только для старых записей
    с ненулевым откатом, и её срок жизни заявки (ENTRY_WAIT_BARS) задан в часах —
    на M15 те же четыре бара означали бы час вместо четырёх.
    """
    open_signals = database.get_open_signals()
    if not open_signals:
        return
    candles: dict[str, object] = {}
    for code in {s["instrument"] for s in open_signals}:
        try:
            candles[code] = (
                await fetch_candles(code, config.M15_TIMEFRAME, config.M15_LIMIT)
                if config.ROLLING_HOUR
                else await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT))
        except Exception as e:
            print(f"[track_signals] {code}: ошибка данных: {e}")
    # Часовые свечи нужны только ступени 1 — тянем их лишь для тех инструментов,
    # где действительно висит неисполненная заявка.
    hourly: dict[str, object] = {}
    for code in {s["instrument"] for s in open_signals
                 if s.get("status") == "waiting_fill"}:
        try:
            hourly[code] = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
        except Exception as e:
            print(f"[track_signals] {code}: ошибка часовых данных: {e}")
    for s in open_signals:
        df = candles.get(s["instrument"])
        if df is None:
            continue
        # Ступень 1 — исполнение заявки.
        if s.get("status") == "waiting_fill":
            h1 = hourly.get(s["instrument"])
            if h1 is None:
                continue
            fill = pattern_detector.evaluate_fill(s, h1)
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
        outcome = pattern_detector.evaluate_signal(_tracked(s), df)
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
    recipients = [owner] if owner else database.get_subscribers(signal["instrument"], "spring")
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


async def monitor_breakout(bot) -> None:
    """Стратегия №5 — пробой сильного уровня (каждые 5 минут). Правила — breakout.py.

    В СЛЕЖКЕ: считает сигналы и доводит их до цели, стопа или истечения, но НИКОМУ
    НЕ ШЛЁТ, пока config.BREAKOUT_SIGNALS = False (решение владельца 17.09.2026 —
    сначала посмотреть на живых данных). Сводка — команда /breakout.

    Идёт по ВСЕМ инструментам движка, а не только по подписанным: смысл слежки в
    статистике, а она не должна зависеть от того, кто на что подписан.

    Уровни берутся из таблицы levels — те же, что видит человек в /analyze, их раз
    в час пересчитывает run_analysis. Значит при выключенном ложном пробое
    (SPRING_SIGNALS = False) run_analysis не работает и уровни устаревают: об этом
    печатается предупреждение, а не тихо считаются сигналы по вчерашним уровням.
    """
    if config.SPRING_SIGNALS is False:
        print("[monitor_breakout] уровни не пересчитываются (run_analysis выключен) — пропуск")
        return
    for code in engine_codes():
        try:
            df = await engine_candles(code)
            h1_trend = await fetch_candles(code, config.H1_TIMEFRAME, config.TREND_TF_H1_LIMIT)
        except Exception as e:
            print(f"[monitor_breakout] {code}: ошибка данных: {e}")
            continue
        levels = database.get_levels(code)
        if not levels:
            continue
        for sig in breakout.detect(df, levels, analyzer.engine_trend(h1_trend)):
            sig_id = database.add_breakout_signal(code, sig)
            if sig_id is None:          # тот же пробой уже записан на прошлом круге
                continue
            print(f"[monitor_breakout] ПРОБОЙ {code} {sig['direction']} "
                  f"уровень {sig['level_price']} вход {sig['entry_price']} "
                  f"стоп {sig['stop_loss']} цель {sig['take_profit']}")

    # Исходы открытых сигналов — по тем же часовым свечам из общего кеша.
    for sig in database.get_open_breakout_signals():
        code = sig["instrument"]
        try:
            df = await fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
        except Exception as e:
            print(f"[monitor_breakout] {code}: ошибка данных при трекинге: {e}")
            continue
        status = breakout.outcome(sig, df)
        if status == "pending":
            continue
        database.close_breakout_signal(sig["id"], status, breakout.result_r(sig, status))
        print(f"[monitor_breakout] {code} #{sig['id']}: {status}")


async def monitor_ict(bot) -> None:
    """Стратегия №6 — ICT на пятнадцатиминутках (каждые 5 минут). Правила — ict.py.

    Идёт по инструментам, У КОТОРЫХ ЕСТЬ ПОДПИСКА НА ICT, — в отличие от тренда и
    пробоя, которые считаются по всему движку. Причина в том, что состояния между
    свечами у ICT нет: сигнал рождается и закрывается сам, и пропущенный сетап по
    инструменту без подписчиков ничего не ломает.

    Свечи M15 берутся одним запросом на config.ICT_M15_LIMIT (960 = 10 суток). Этого
    хватает и пулам ликвидности (живут config.ICT_POOL_AGE_H = 240 ч), и трекингу
    исхода (горизонт 48 ч). Валютные пары вне форекс-сессии на паузе: запрос падает,
    инструмент пропускается до следующего раза — как у всех остальных задач.
    """
    if not config.ICT_SIGNALS:
        return
    for code in _subscribed_engine("ict"):
        try:
            df = await fetch_candles(code, config.ICT_TIMEFRAME, config.ICT_M15_LIMIT)
        except Exception as e:
            print(f"[monitor_ict] {code}: ошибка данных: {e}")
            continue
        for sig in ict.detect(df):
            if database.add_ict_signal(code, sig) is None:
                continue        # тот же сетап уже записан либо дедуп по времени
            print(f"[monitor_ict] СИГНАЛ {code} {sig['direction']} "
                  f"пул {sig['pool_price']} вход {sig['entry_price']} "
                  f"стоп {sig['stop_loss']} цель {sig['take_profit']}")
            await _notify_ict(bot, code, sig)

    # Исходы открытых сигналов — по тем же пятнадцатиминуткам из общего кеша.
    for sig in database.get_open_ict_signals():
        code = sig["instrument"]
        try:
            df = await fetch_candles(code, config.ICT_TIMEFRAME, config.ICT_M15_LIMIT)
        except Exception as e:
            print(f"[monitor_ict] {code}: ошибка данных при трекинге: {e}")
            continue
        status = ict.outcome(sig, df)
        if status == "pending":
            continue
        result_r = ict.result_r(sig, status)
        database.close_ict_signal(sig["id"], status, result_r)
        print(f"[monitor_ict] {code} #{sig['id']}: {status}")
        await _notify_ict_outcome(bot, code, sig, status, result_r)


async def _notify_ict(bot, code: str, sig: dict) -> None:
    """Сигнал ICT — подписчикам инструмента по этой стратегии. Коротко, цифрами."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(sig["entry_price"])
    long_ = sig["direction"] == "long"
    arrow = "🟢 ЛОНГ" if long_ else "🔴 ШОРТ"
    risk = abs(sig["entry_price"] - sig["stop_loss"])
    risk_pct = risk / sig["entry_price"] if sig["entry_price"] else 0.0
    rr = abs(sig["take_profit"] - sig["entry_price"]) / risk if risk else 0.0
    # Объём под риск 1% депозита — как в сообщении тренда. Стоп у ICT узкий, поэтому
    # объём почти всегда больше депозита, и плечо называется прямо.
    size = 0.01 / risk_pct if risk_pct else 0.0
    size_txt = (f"{size:.0%} депозита" if size <= 1
                else f"{size:.1f} депозита, плечо x{math.ceil(size)}")
    late = ""
    age = pd.Timestamp.now(tz="UTC") - pd.Timestamp(sig["bar_time"])
    if age > timedelta(minutes=30):
        late = (f"⚠️ Сообщение запоздало на {age.total_seconds() / 60:.0f} мин — "
                "цена могла уйти.\n")
    took = "минимум" if long_ else "максимум"
    text = (
        f"💧 ICT — {info['short']} {arrow}\n"
        f"Сняли {took} {fmt(sig['pool_price'], d)}, разрыв не закрыт\n\n"
        f"Вход: {fmt(sig['entry_price'], d)} по рынку\n"
        f"Стоп: {fmt(sig['stop_loss'], d)} ({'−' if long_ else '+'}{risk_pct:.1%})\n"
        f"Цель: {fmt(sig['take_profit'], d)} (1:{rr:.1f})\n"
        f"Объём: {size_txt} = риск 1%\n"
        f"{late}"
    ).rstrip()
    for user_id in database.get_subscribers(code, "ict"):
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[monitor_ict] не отправить {user_id}: {e}")


async def _notify_ict_outcome(bot, code: str, sig: dict, status: str,
                              result_r: float | None) -> None:
    """Итог сделки ICT — тем же подписчикам инструмента."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(sig["entry_price"])
    arrow = "🟢 ЛОНГ" if sig["direction"] == "long" else "🔴 ШОРТ"
    if status == "hit_tp":
        text = (f"✅ ICT — {info['short']} {arrow}: цель {fmt(sig['take_profit'], d)} взята, "
                f"итог {result_r:+.1f}R (без комиссии).")
    elif status == "hit_sl":
        text = (f"🛑 ICT — {info['short']} {arrow}: стоп {fmt(sig['stop_loss'], d)}, "
                f"итог −1.0R (без комиссии).")
    else:
        text = (f"⌛ ICT — {info['short']} {arrow}: за {config.ICT_EXPIRE_HOURS} ч "
                f"не дошло ни до цели, ни до стопа. Сделка снята со счёта.")
    for user_id in database.get_subscribers(code, "ict"):
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[monitor_ict] не отправить {user_id}: {e}")


async def monitor_trend(bot) -> None:
    """Стратегия №4 — тренд по недельному каналу (каждые 5 минут). Правила — trend.py.

    Идёт по ВСЕМ инструментам движка, а не только по подписанным. Позиция модели одна
    на инструмент и общая, и вести её надо непрерывно: иначе стоп или выход, случившийся,
    пока подписчиков не было, потерялся бы, и /trend показывал бы позицию, которой нет.

    Свечи обрабатываются по одной, начиная со следующей после trend_state.last_bar.
    Поэтому рестарт и простой счёт не ломают: пропущенные часы догоняются по очереди,
    как их прошёл бы замер. Окно — config.TREND_H1_LIMIT часов; лежал дольше — старые
    часы уже не восстановить, об этом строка в логе.

    Сообщения уходят подписчикам инструмента из /subscribe (решение владельца 14.09.2026),
    подписанным на него именно по тренду (с 15.09.2026). Валютные пары вне сессии на паузе:
    запрос свечей падает, инструмент пропускается до следующего раза.

    При config.TREND_SIGNALS = False (выключено 17.09.2026) задача продолжает
    работать, но НОВЫХ позиций не открывает: она доводит уже открытые до исхода и
    двигает last_bar, чтобы при возврате стратегии в бой вход не объявился по
    пробою, случившемуся дни назад. Подробности — в комментарии к константе.
    """
    for code in engine_codes():
        try:
            df = await fetch_candles(code, config.H1_TIMEFRAME, config.TREND_H1_LIMIT)
        except Exception as e:
            print(f"[monitor_trend] {code}: ошибка данных: {e}")
            continue
        if len(df) <= channel_trend.WARM + 1:
            continue
        last_bar, position = database.get_trend_state(code)
        if last_bar is not None and pd.Timestamp(last_bar) < df.index[channel_trend.WARM - 1]:
            print(f"[monitor_trend] {code}: бот не видел рынок дольше окна свечей "
                  f"(с {last_bar}) — часть часов пропущена")
        open_id = position["id"] if position else None
        new_last, _, events = channel_trend.step(df, last_bar, position,
                                                 config.TREND_SIGNALS)
        if new_last == last_bar and not events:
            continue
        database.save_trend_step(code, new_last, events, open_id)
        for ev in events:
            print(f"[monitor_trend] {code}: {ev['type']} {ev['direction']} "
                  f"вход {ev['entry_price']} стоп {ev['stop_loss']}")
            await _notify_trend(bot, code, ev)


async def _notify_trend(bot, code: str, ev: dict) -> None:
    """Сообщение о входе, стопе или выходе модели тренда — подписчикам инструмента."""
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(ev["entry_price"])
    long_ = ev["direction"] == "long"
    arrow = "🟢 ЛОНГ" if long_ else "🔴 ШОРТ"
    when = ev["entry_time"] if ev["type"] == "entry" else ev["exit_time"]
    late = ""
    age = pd.Timestamp.now(tz="UTC") - pd.Timestamp(when)
    if age > timedelta(hours=2):
        late = (f"⚠️ Сообщение запоздало на {age.total_seconds() / 3600:.0f} ч — "
                "бот не видел рынок. Цена могла уйти.\n")
    if ev["type"] == "entry":
        risk_pct = abs(ev["entry_price"] - ev["stop_loss"]) / ev["entry_price"]
        # Объём позиции, при котором стоп стоит 1% депозита. У валюты стоп узкий,
        # и нужный объём больше депозита — тогда называем и плечо.
        size = 0.01 / risk_pct if risk_pct else 0.0
        size_txt = (f"{size:.0%} депозита" if size <= 1
                    else f"{size:.1f} депозита, плечо x{math.ceil(size)}")
        # Коротко, цифрами: владелец просил «меньше букв» (15.09.2026). Цели у тренда
        # нет, поэтому вместо неё строка выхода — текущая граница канала 42 ч.
        stop_sign = "−" if long_ else "+"
        back = "ниже" if long_ else "выше"
        text = (
            f"📈 ТРЕНД — {info['short']} {arrow}\n"
            f"Пробой недели {fmt(ev['level'], d)}\n\n"
            f"Вход: {fmt(ev['entry_price'], d)} по рынку\n"
            f"Стоп: {fmt(ev['stop_loss'], d)} ({stop_sign}{risk_pct:.1%})\n"
            f"Выход: час {back} {fmt(ev['exit_level'], d)} — напишу\n"
            f"Объём: {size_txt} = риск 1%\n"
            f"{late}"
        ).rstrip()
    elif ev["type"] == "stop":
        text = (
            f"🛑 ТРЕНД — {info['short']} {arrow}: сработал стоп {fmt(ev['stop_loss'], d)}.\n"
            f"Вход был {fmt(ev['entry_price'], d)}, закрытие ≈ {fmt(ev['exit_price'], d)} → "
            f"итог {ev['result_r']:+.1f}R (без комиссии и фандинга).\n"
            f"{late}"
            "Позиция закрыта, жду нового пробоя недели."
        )
    else:
        back = "ниже минимума" if long_ else "выше максимума"
        text = (
            f"🏁 ТРЕНД — {info['short']} {arrow}: выход по каналу.\n"
            f"Час закрылся {back} последних 42 часов ({fmt(ev['level'], d)}). "
            f"Закрывай по рынку ≈ {fmt(ev['exit_price'], d)}.\n"
            f"Вход был {fmt(ev['entry_price'], d)} → итог {ev['result_r']:+.1f}R "
            "(без комиссии и фандинга).\n"
            f"{late}"
        )
    for user_id in database.get_subscribers(code, "trend"):
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            print(f"[monitor_trend] не отправить {user_id}: {e}")


def ict_overview() -> str:
    """Текст /ict: что настреляла стратегия №6 с момента включения.

    Сети не требует, как и сводка пробоя: у сигнала ICT стоп и цель заданы при входе,
    показывать текущую цену незачем — сделка либо закрыта, либо ждёт своего часа.
    """
    rows = database.get_ict_signals()
    head = ("💧 ICT — свип ликвидности и разрыв на 15-минутках (стратегия №6).\n"
            f"Правило: сняли пул ликвидности → импульс с разрывом (FVG) → слом "
            f"структуры → вход по рынку, стоп за манипуляцией + "
            f"{config.ICT_STOP_ATR:g} ATR, цель — встречная ликвидность не ближе "
            f"{config.ICT_MIN_TP_R:g} риска, срок {config.ICT_EXPIRE_HOURS} ч.")
    if not config.ICT_SIGNALS:
        head = "⛔ Стратегия ВЫКЛЮЧЕНА — новых сигналов не будет.\n" + head
    if not rows:
        return (head + "\n\nПока ни одного сигнала. Сетап редкий: мало снять "
                "ликвидность — нужен ещё импульс с незакрытым разрывом и слом структуры.")
    opened = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] != "open"]
    done = [r for r in closed if r["status"] in ("hit_tp", "hit_sl")]
    lines = [head, "",
             f"Всего сигналов: {len(rows)} · открыто: {len(opened)} · "
             f"закрыто: {len(closed)}"]
    if done:
        wins = [r for r in done if r["status"] == "hit_tp"]
        total = sum(r["result_r"] or 0.0 for r in done)
        lines.append(f"Из закрытых дошли до цели {len(wins)} из {len(done)} "
                     f"({len(wins) / len(done) * 100:.0f}%), итог {total:+.1f}R "
                     f"(без комиссии и фандинга)")
    expired = sum(1 for r in closed if r["status"] == "expired")
    if expired:
        lines.append(f"Истекло, не дойдя ни до цели, ни до стопа: {expired}")
    if opened:
        lines += ["", "Открытые:"]
        for r in opened[:10]:
            info = resolve(r["instrument"])
            d = (info["decimals"] if info["decimals"] is not None
                 else infer_decimals(r["entry_price"]))
            arrow = "🟢" if r["direction"] == "long" else "🔴"
            lines.append(f"{arrow} {info['short']} пул {fmt(r['pool_price'], d)} · вход "
                         f"{fmt(r['entry_price'], d)} · стоп {fmt(r['stop_loss'], d)} · "
                         f"цель {fmt(r['take_profit'], d)} ({r['bar_time'][:16]} UTC)")
    if done:
        lines += ["", "Последние закрытые:"]
        mark = {"hit_tp": "✅", "hit_sl": "🛑"}
        for r in done[:5]:
            info = resolve(r["instrument"])
            lines.append(f"{mark.get(r['status'], '⌛')} {info['short']} "
                         f"{'лонг' if r['direction'] == 'long' else 'шорт'} "
                         f"{(r['result_r'] or 0.0):+.1f}R ({r['bar_time'][:16]} UTC)")
    return "\n".join(lines)


def breakout_overview() -> str:
    """Текст /breakout: что стратегия №5 насчитала, пока она в слежке.

    Сети не требует (в отличие от trend_overview): у пробоя цель и стоп заданы при
    входе, текущую цену показывать незачем — сделка либо уже закрыта, либо ждёт.
    """
    rows = database.get_breakout_signals()
    if not rows:
        return ("🧪 Пробой сильного уровня — стратегия в СЛЕЖКЕ: бот считает сигналы, "
                "но никому их не шлёт.\nПока ни одного сигнала не набралось — "
                "часовая свеча должна закрыться за сильным уровнем (⭐ в /analyze).")
    opened = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] != "open"]
    done = [r for r in closed if r["status"] in ("hit_tp", "hit_sl")]
    lines = [
        "🧪 Пробой сильного уровня — стратегия №5, В СЛЕЖКЕ.",
        "Сигналы считаются и ведутся, но НИКОМУ НЕ ШЛЮТСЯ: смотрим на живых данных, "
        "что она даёт.",
        f"Правило: часовая свеча закрылась за СИЛЬНЫМ уровнем (⭐), вход по закрытию, "
        f"стоп за фитилём + {config.BREAKOUT_STOP_ATR:g} ATR, цель — встречный уровень "
        f"не ближе {config.BREAKOUT_MIN_TP_R:g} рисков, срок {config.BREAKOUT_EXPIRE_HOURS} ч.",
        "",
        f"Всего сигналов: {len(rows)} · открыто: {len(opened)} · закрыто: {len(closed)}",
    ]
    if done:
        wins = [r for r in done if r["status"] == "hit_tp"]
        total = sum(r["result_r"] or 0.0 for r in done)
        lines.append(f"Из закрытых дошли до цели {len(wins)} из {len(done)} "
                     f"({len(wins) / len(done) * 100:.0f}%), итог {total:+.1f}R "
                     f"(без комиссии и фандинга)")
    expired = sum(1 for r in closed if r["status"] == "expired")
    if expired:
        lines.append(f"Истекло, не дойдя ни до цели, ни до стопа: {expired}")
    if opened:
        lines.append("")
        lines.append("Открытые:")
        for r in opened[:10]:
            info = resolve(r["instrument"])
            d = info["decimals"] if info["decimals"] is not None else infer_decimals(r["entry_price"])
            arrow = "🟢" if r["direction"] == "long" else "🔴"
            lines.append(f"{arrow} {info['short']} от {fmt(r['level_price'], d)} · вход "
                         f"{fmt(r['entry_price'], d)} · стоп {fmt(r['stop_loss'], d)} · "
                         f"цель {fmt(r['take_profit'], d)} ({r['bar_time'][:16]} UTC)")
    if done:
        lines.append("")
        lines.append("Последние закрытые:")
        mark = {"hit_tp": "✅", "hit_sl": "🛑"}
        for r in done[:5]:
            info = resolve(r["instrument"])
            lines.append(f"{mark.get(r['status'], '⌛')} {info['short']} "
                         f"{'лонг' if r['direction'] == 'long' else 'шорт'} "
                         f"{(r['result_r'] or 0.0):+.1f}R ({r['bar_time'][:16]} UTC)")
    return "\n".join(lines)


async def trend_overview() -> str:
    """Текст /trend: открытые позиции модели с текущим итогом и закрытые сделки."""
    rows = database.get_trend_positions()
    opened = [p for p in rows if p["status"] == "open"]
    closed = [p for p in rows if p["status"] != "open"]
    lines = [
        "📈 Тренд по недельному каналу — вторая стратегия бота.",
        "Вход: час закрылся за максимумом (минимумом) последних 168 ч. Стоп 3 ATR. "
        "Выход: час закрылся за встречным каналом 42 ч. Цели нет.",
        "Модель ведёт одну позицию на инструмент по часовым свечам BingX, сигналы "
        "приходят по инструментам, отмеченным для тренда в /subscribe.",
        "",
    ]
    # Выключенная стратегия обязана сказать об этом здесь: иначе человек читает
    # правила и ждёт сигналов, которых не будет. Про доведение открытых пишем только
    # пока они есть — иначе строка обещает работу, которой не осталось.
    if not config.TREND_SIGNALS:
        tail = (" Уже открытые позиции доводятся до стопа или выхода по каналу."
                if opened else "")
        lines.insert(1, f"⛔ Стратегия ВЫКЛЮЧЕНА — новых входов не будет.{tail}")
    if opened:
        lines.append("Открытые позиции:")
        for p in opened:
            info = resolve(p["instrument"])
            d = info["decimals"] if info["decimals"] is not None else infer_decimals(p["entry_price"])
            arrow = "🟢" if p["direction"] == "long" else "🔴"
            tail = ""
            try:
                df = await fetch_candles(p["instrument"], config.H1_TIMEFRAME, config.TREND_H1_LIMIT)
                snap = channel_trend.snapshot(df, p)
                sign = "<" if p["direction"] == "long" else ">"
                tail = (f" · сейчас {snap['open_r']:+.1f}R · выход при закрытии часа "
                        f"{sign} {fmt(snap['exit_level'], d)}")
            except Exception as e:
                print(f"[trend_overview] {p['instrument']}: {e}")
            lines.append(f"{arrow} {info['short']} вход {fmt(p['entry_price'], d)} "
                         f"({p['entry_time'][:16]} UTC) · стоп {fmt(p['stop_loss'], d)}{tail}")
    else:
        lines.append("Открытых позиций нет — модель ждёт пробоя недели.")
    lines.append("")
    if closed:
        total = sum(p["result_r"] or 0.0 for p in closed)
        wins = sum(1 for p in closed if (p["result_r"] or 0.0) > 0)
        lines.append(f"Закрыто с запуска — сделок: {len(closed)}, в плюсе: {wins}, "
                     f"итог {total:+.1f}R (без комиссии и фандинга). Последние:")
        for p in closed[:5]:
            info = resolve(p["instrument"])
            arrow = "🟢" if p["direction"] == "long" else "🔴"
            # 'manual' — закрыто рукой владельца (17.09.2026, снятие стратегии с боя).
            # Назвать его «выходом» значило бы приписать модели решение, которого она
            # не принимала: канал в тот момент позицию держал.
            how = {"stop": "стоп", "exit": "выход"}.get(p["status"], "закрыто вручную")
            lines.append(f"  {arrow} {info['short']} — {how}, {p['result_r']:+.1f}R "
                         f"({(p['exit_time'] or '')[:10]})")
    else:
        lines.append("Закрытых сделок пока нет — модель запущена 14.09.2026.")
    lines.append("")
    lines.append("На истории стратегия в плюсе на ETH и SOL, около нуля на BTC и золоте, "
                 "в минусе на валюте и нефти. Замер по инструменту — в каждом сигнале.")
    return "\n".join(lines)


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


def jobs() -> list[tuple]:
    """Какие задачи ставятся в планировщик и с каким интервалом (в минутах).

    Вынесено из setup, чтобы состав проверялся тестом без запуска планировщика.
    Ложный пробой (run_analysis + monitor_signals) ставится только при
    config.SPRING_SIGNALS — выключался утром 15 сентября 2026, в тот же день включён
    с правилами 23 июня. track_signals остаётся всегда: при выключении он доводит до
    исхода уже открытые сигналы.
    """
    out = []
    if config.SPRING_SIGNALS:
        out += [(run_analysis, config.ANALYZE_EVERY_MIN),
                (monitor_signals, config.MONITOR_EVERY_MIN)]
    if config.ICT_SIGNALS:
        out.append((monitor_ict, config.MONITOR_EVERY_MIN))
    out += [
        (monitor_trend, config.MONITOR_EVERY_MIN),
        (monitor_breakout, config.MONITOR_EVERY_MIN),
        (track_signals, config.MONITOR_EVERY_MIN),
        (track_trades, config.MONITOR_EVERY_MIN),
        (check_alerts, config.ALERT_EVERY_MIN),
    ]
    return out


def setup(bot) -> AsyncIOScheduler:
    """Создаёт и запускает единственный планировщик бота (состав задач — jobs())."""
    sched = AsyncIOScheduler()
    for func, minutes in jobs():
        sched.add_job(func, "interval", minutes=minutes, args=[bot])
    if not config.SPRING_SIGNALS:
        print("[scheduler] ложный пробой выключен (SPRING_SIGNALS = False): "
              "run_analysis и monitor_signals не запущены")
    if not config.TREND_SIGNALS:
        print("[scheduler] тренд выключен (TREND_SIGNALS = False): новых позиций "
              "не открываем, открытые доводим до стопа или выхода по каналу")
    sched.start()
    return sched
