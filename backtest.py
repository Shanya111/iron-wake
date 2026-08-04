"""Бэктест движка на исторических свечах: калибровка стопа по факту, а не на глаз.

Зачем. Стоп сейчас ставится за экстремум свечи пробоя с фиксированным запасом
config.STOP_SPREAD (0.1%). Живое наблюдение: часто цена свипает этот стоп ещё
на пару десятых процента глубже и только потом идёт в нужную сторону. Вопрос
«насколько шире должен быть буфер» решается замером, а не подкруткой на глаз.

Что считает:
  1. MAE (maximum adverse excursion) — насколько цена уходит ПРОТИВ входа, прежде
     чем дойти до цели. Меряем по сигналам, которые без стопа до цели дошли: это
     ровно те сделки, где стоп либо спас, либо зря выбил. Перцентили MAE и есть
     ответ «какой буфер нужен, чтобы пережить N% таких свипов».
  2. Сравнение вариантов стопа (проценты и ATR) сквозным прогоном БОЕВОГО
     детектора: широкий стоп меняет риск → меняет цель и фильтр R:R, поэтому
     каждый вариант гоняется заново целиком. Сравнивать варианты нужно по итогу
     в R, а не по винрейту: широкий стоп всегда поднимает винрейт и всегда
     увеличивает цену ошибки.

Честность прогона (без заглядывания в будущее):
  • уровни на момент сигнала считаются по окну, которое заканчивается на самой
    свече пробоя (в бою окно на бар длиннее — то есть здесь мы даже строже);
  • дневная свеча текущего дня собирается из H1-баров до сигнального включительно,
    а не берётся готовой (в бою она на этот момент ещё не закрыта);
  • при двусмысленности внутри свечи (накрыла и стоп, и цель) исход считает
    pattern_detector.evaluate_signal — то есть консервативно, стоп первым.

Запуск:
    python backtest.py BTC
    python backtest.py BTC ETH GOLD --bars 8760 --min-rr 1.5 --vol-mult 1.4
    python backtest.py --all --csv отчёт.csv
    python backtest.py --all --trend-d1              # сравнить с дневным трендом
    python backtest.py --all --entry-days 0,1,2      # сравнить с окном входа пн–ср
"""

import argparse
import asyncio
import csv
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

import analyzer
import config
import database
import pattern_detector
import trading_week
from instruments import INSTRUMENTS, data_source, engine_codes, resolve

# Kraken (боевой источник движка) отдаёт максимум 720 часовых свечей — 30 дней,
# на статистику не хватает. Для глубокой истории берём биржу с пагинацией по
# `since`: Bitfinex даёт котировки к USD по всей нашей крипте и не блокирует РФ.
# Цены/объёмы там чуть отличаются от Kraken — для калибровки порогов это неважно,
# для абсолютных цифр доходности учитывать стоит.
# Список запасных: биржа может временно закрыть доступ по рейт-лимиту, и без
# запасного варианта инструмент молча выпадает из прогона — а выводы по половине
# набора хуже, чем никаких. Пробуем по порядку, берём первую ответившую.
HISTORY_EXCHANGE = {
    "BTC": [("bitfinex", "BTC/USD"), ("bitstamp", "BTC/USD"), ("kucoin", "BTC/USDT")],
    "ETH": [("bitfinex", "ETH/USD"), ("bitstamp", "ETH/USD"), ("kucoin", "ETH/USDT")],
    "SOL": [("bitfinex", "SOL/USD"), ("bitstamp", "SOL/USD"), ("kucoin", "SOL/USDT")],
    "TON": [("bitfinex", "TON/USD"), ("gate", "TON/USDT")],
}

# Варианты запаса стопа за экстремум свечи пробоя.
#   ("pct", x) — x доля от цены (как сейчас в config.STOP_SPREAD);
#   ("atr", k) — k × ATR(14) на H1 в момент сигнала (адаптируется к волатильности).
# Сетку держим шире предполагаемого оптимума с обеих сторон: если победитель
# оказался на краю списка, значит перебор обрезан и настоящий оптимум не проверен.
STOP_VARIANTS = [
    ("0.1% (сейчас)", "pct", 0.001),
    ("0.2%",          "pct", 0.002),
    ("0.3%",          "pct", 0.003),
    ("0.5%",          "pct", 0.005),
    ("0.8%",          "pct", 0.008),
    ("1.2%",          "pct", 0.012),
    ("ATR×0.25",      "atr", 0.25),
    ("ATR×0.5",       "atr", 0.5),
    ("ATR×1.0",       "atr", 1.0),
    ("ATR×1.5",       "atr", 1.5),
    ("ATR×2.0",       "atr", 2.0),
    ("ATR×3.0",       "atr", 3.0),
]

ATR_PERIOD = 14

# Кеш скачанной истории. Лежит рядом с кодом, в .gitignore. Нужен, чтобы разные
# варианты движка сравнивались на одном и том же срезе данных (и чтобы биржа не
# банила за повторные закачки). Сбросить — удалить папку или запустить --no-cache.
CACHE_DIR = Path(__file__).parent / ".backtest_cache"


# ── Загрузка истории ────────────────────────────────────────────────────────

async def _fetch_paged(exchange: str, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """Свечи с биржи через CCXT постранично (по `since`), пока не наберём `bars`.

    Нужно, т.к. за один запрос биржа отдаёт 500–1000 свечей. Формат результата —
    как у data_fetcher.get_candles: open/high/low/close/volume, индекс UTC.
    """
    import ccxt.async_support as ccxt

    ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 20000})
    step_ms = ex.parse_timeframe(timeframe) * 1000
    now_ms = int(time.time() * 1000)
    since = now_ms - bars * step_ms
    rows: list[list] = []
    try:
        for _ in range(200):  # предохранитель от бесконечного цикла
            # Биржа может ответить «ratelimit» — не ошибка данных, а просьба
            # подождать. Без ретрая инструмент молча выпадает из прогона, и
            # выводы делаются по половине набора — а это хуже, чем медленный
            # прогон. Bitfinex при перегрузе банит примерно на минуту, поэтому
            # паузы длинные: секунды его не устраивают.
            for attempt in range(5):
                try:
                    chunk = await ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
                    break
                except Exception as e:
                    if "ratelimit" not in str(e).lower() or attempt == 4:
                        raise
                    wait = 30 * (attempt + 1)
                    print(f"    {symbol}: биржа просит подождать, пауза {wait}с "
                          f"(попытка {attempt + 2}/5)")
                    await asyncio.sleep(wait)
            chunk = [c for c in chunk if not rows or c[0] > rows[-1][0]]
            if not chunk:
                break
            rows += chunk
            since = rows[-1][0] + step_ms
            if since >= now_ms:
                break
    finally:
        try:
            await ex.close()
        except Exception:
            pass
    if not rows:
        raise ValueError(f"{exchange} не отдал свечи по {symbol}")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop(columns=["ts"])


async def load_history(code: str, bars: int,
                       use_cache: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """H1 и D1 свечи инструмента на максимальную доступную глубину + описание источника.

    Крипта — биржа из HISTORY_EXCHANGE (глубокая история). Золото/нефть — Yahoo
    (там же, где их берёт движок; до 730 дней H1). Форекс — только Kraken, а он
    отдаёт максимум 720 часовых свечей: выборка выйдет тонкой, об этом печатается
    предупреждение.

    Загруженная история кладётся в `.backtest_cache/` и переиспользуется. Это не
    только про скорость: когда сравниваешь варианты движка несколькими прогонами,
    их надо считать на ОДНОМ И ТОМ ЖЕ срезе данных, иначе разница вариантов
    смешается с разницей выборок. Плюс биржа не банит за повторные закачки.
    `--no-cache` качает заново.
    """
    cached = CACHE_DIR / f"{code}_{bars}.pkl"
    if use_cache and cached.exists():
        h1, d1, source = pd.read_pickle(cached)
        age = (time.time() - cached.stat().st_mtime) / 3600
        print(f"  {code}: история из кеша ({age:.0f}ч назад, {len(h1)} свечей)")
        return h1, d1, f"{source} (кеш)"

    src = data_source(code)
    if code in HISTORY_EXCHANGE:
        last_error = None
        result = None
        for exchange, symbol in HISTORY_EXCHANGE[code]:
            try:
                h1 = await _fetch_paged(exchange, symbol, "1h", bars)
                d1 = await _fetch_paged(exchange, symbol, "1d",
                                        bars // 24 + config.D1_LIMIT + 10)
                result = (h1, d1, f"{exchange} {symbol}")
                break
            except Exception as e:
                last_error = e
                print(f"  {code}: {exchange} не отдал историю ({str(e)[:60]}) — пробую следующую")
        if result is None:
            raise ValueError(f"ни одна биржа не отдала историю по {code}: {last_error}")
    elif src == "yahoo":
        ticker = resolve(code)["ticker"]
        days = min(730, bars // 12 + 10)  # у Yahoo H1 не глубже 730 дней
        h1 = await asyncio.to_thread(database.get_hourly_candles, ticker, days)
        d1 = await asyncio.to_thread(database.get_daily_candles, ticker, days + 60)
        result = (h1.tail(bars), d1, f"yahoo {ticker}")
    elif src == "ccxt":
        import data_fetcher
        from instruments import ccxt_symbol
        sym = ccxt_symbol(code)
        h1 = await data_fetcher.get_candles(sym["symbol"], "1h", 720, sym["exchange"])
        d1 = await data_fetcher.get_candles(sym["symbol"], "1d", 720, sym["exchange"])
        print(f"  ⚠️  {code}: глубокой истории нет — Kraken отдаёт максимум "
              f"{len(h1)} часовых свечей. Выборка будет тонкой.")
        result = (h1, d1, f"{sym['exchange']} {sym['symbol']}")
    else:
        raise ValueError(f"{code} не входит в движок — бэктестить нечего")

    CACHE_DIR.mkdir(exist_ok=True)
    pd.to_pickle(result, cached)
    return result


# ── Реконструкция состояния движка на момент бара ───────────────────────────

def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """ATR — средний истинный диапазон. Мера волатильности инструмента: в ней
    удобно задавать запас стопа, чтобы он не зависел от масштаба цены."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _d1_view(d1: pd.DataFrame, h1: pd.DataFrame, i: int) -> pd.DataFrame:
    """Дневные свечи, какими они были в момент бара `i` (без будущего).

    Закрытые дни берём как есть, а текущий день собираем из H1-баров до `i`
    включительно — в бою дневная свеча на этот момент ещё формируется, и брать
    её готовой значило бы подсматривать итог дня.
    """
    day = h1.index[i].normalize()
    closed = d1[d1.index.normalize() < day]
    today = h1.iloc[int(h1.index.searchsorted(day)):i + 1]
    if len(today):
        bar = pd.DataFrame([{
            "open": float(today["open"].iloc[0]),
            "high": float(today["high"].max()),
            "low": float(today["low"].min()),
            "close": float(today["close"].iloc[-1]),
            "volume": float(today["volume"].sum()),
        }], index=[day])
        closed = pd.concat([closed, bar])
    return closed.tail(config.D1_LIMIT)


def _levels_at(d1_view: pd.DataFrame, h1_window: pd.DataFrame) -> list[dict]:
    """Уровни, какими их посчитал бы run_analysis по этим окнам.

    Зоны ликвидности пропускаем осознанно: detect_spring/upthrust фильтруют
    уровни по type support/resistance, так что на сигналы они не влияют.
    """
    global_levels = analyzer.find_levels(d1_view, config.D1_PIVOT_WINDOW, "D1")
    local_levels = analyzer.find_levels(h1_window, config.H1_PIVOT_WINDOW, "H1")
    return analyzer.prioritize_levels(global_levels, local_levels)


def _variant_settings(base: dict, kind: str, value: float, atr: float) -> dict | None:
    """Пороги детектора для конкретного варианта стопа. None — если ATR ещё не
    посчитан (начало истории) и вариант посчитать не на чем."""
    s = dict(base)
    if kind == "pct":
        s["STOP_SPREAD"] = value
    else:
        if not atr or pd.isna(atr):
            return None
        s["STOP_ABS"] = atr * value
    return s


# ── Замер хода против позиции ───────────────────────────────────────────────

def measure_excursion(signal: dict, future: pd.DataFrame) -> dict:
    """Как повела себя цена после входа, ЕСЛИ БЫ СТОПА НЕ БЫЛО.

    Возвращает:
      • reached_tp — дошла ли до цели в пределах окна;
      • mae_abs — максимальный ход против позиции от экстремума свечи пробоя
        (для лонга: насколько ниже её low цена провалилась) до момента цели;
      • mae_from_entry — то же, но от цены входа;
      • mfe_abs — максимальный ход в плюс от входа;
      • bars — сколько свечей до цели.

    Меряем именно от экстремума свечи пробоя, потому что запас стопа отсчитывается
    от него же — так замер сразу даёт нужную величину буфера.
    """
    long = signal["direction"] == "long"
    entry, tp = signal["entry_price"], signal["take_profit"]
    extreme = signal["break_extreme"]
    worst = extreme
    best = entry
    for n, (_, c) in enumerate(future.iterrows(), start=1):
        hi, lo = float(c["high"]), float(c["low"])
        if long:
            worst = min(worst, lo)
            best = max(best, hi)
            if hi >= tp:
                return {"reached_tp": True, "bars": n,
                        "mae_abs": extreme - worst, "mae_from_entry": entry - worst,
                        "mfe_abs": best - entry}
        else:
            worst = max(worst, hi)
            best = min(best, lo)
            if lo <= tp:
                return {"reached_tp": True, "bars": n,
                        "mae_abs": worst - extreme, "mae_from_entry": worst - entry,
                        "mfe_abs": entry - best}
    return {
        "reached_tp": False, "bars": len(future),
        "mae_abs": (extreme - worst) if long else (worst - extreme),
        "mae_from_entry": (entry - worst) if long else (worst - entry),
        "mfe_abs": (best - entry) if long else (entry - best),
    }


# ── Прогон ──────────────────────────────────────────────────────────────────

def replay(h1: pd.DataFrame, d1: pd.DataFrame, base: dict, horizon: int,
           week_filter: bool = True, trend_d1: bool = False,
           risk_filter: bool = True) -> dict[str, list[dict]]:
    """Прогон истории бар за баром: сигналы каждого варианта стопа + их исходы.

    week_filter=True (как в бою) — входы только пн–чт, а сделка, не отработавшая
    к пятничному закрытию, гасится по рынку (статус 'closed_week'). False — как
    было до недельного окна, для сравнения.

    trend_d1=False (как в бою) — фильтр направления по часовому тренду. True —
    по дневному, как было до перехода на недельный горизонт: даёт сравнить, что
    даёт смена таймфрейма ориентира, замером, а не на глаз.

    Возвращает {имя варианта: [сигналы с исходом и замерами]}.
    """
    atr = _atr(h1)
    # Предфильтр: детектор всё равно отсеет свечи без всплеска объёма, а считать
    # уровни на каждом из тысяч баров дорого. Условие то же, что в _avg_volume.
    avg_vol = h1["volume"].rolling(config.VOL_LOOKBACK).mean().shift(1)
    spike = h1["volume"] > avg_vol * base["VOL_MULT"]

    warmup = max(config.VOL_LOOKBACK + 3, config.H1_PIVOT_WINDOW * 2 + 2)
    last_ts = h1.index[-1]
    results: dict[str, list[dict]] = {name: [] for name, _, _ in STOP_VARIANTS}
    # Дедуп как в бою: один и тот же паттерн не чаще SIGNAL_DEDUP_MIN минут.
    last_emit: dict[tuple[str, str, str], pd.Timestamp] = {}

    for i in range(warmup, len(h1) - 1):
        if not bool(spike.iloc[i]):
            continue
        bar_ts = h1.index[i]
        # Сигналу нужен полный горизонт будущего, иначе исход неизвестен —
        # такие хвостовые сигналы в статистику не берём.
        if last_ts - bar_ts < timedelta(hours=horizon):
            break
        # С недельным окном исход может решиться пятничным закрытием, поэтому
        # свечей нужно с запасом за него — иначе не увидим, что неделя кончилась.
        deadline = trading_week.week_close_after(bar_ts) if week_filter else None
        eval_end = bar_ts + timedelta(hours=horizon)
        if deadline is not None:
            eval_end = max(eval_end, deadline + timedelta(hours=2))
            if last_ts < eval_end:
                continue

        d1_view = _d1_view(d1, h1, i)
        if len(d1_view) < config.D1_PIVOT_WINDOW * 2 + 2:
            continue
        # Окно уровней заканчивается на самой свече пробоя (в бою оно на бар
        # длиннее) — так гарантированно без будущего.
        h1_window = h1.iloc[max(0, i + 1 - config.H1_LIMIT):i + 1]
        # Фильтр направления — по часовому тренду, как в бою (scheduler.monitor_signals).
        # Считаем по окну до свечи пробоя включительно: в бою тренд берётся по всему
        # H1 вместе с ещё формирующимся баром, здесь строже — без будущего.
        trend = analyzer.get_trend(d1_view if trend_d1 else h1_window)
        levels = _levels_at(d1_view, h1_window)
        # Детектор берёт свечу как df.iloc[-2], поэтому окно на бар длиннее;
        # сам бар i+1 он не читает (только df.iloc[pos] и объёмы до pos).
        window = h1.iloc[max(0, i + 2 - config.H1_LIMIT):i + 2]
        future = h1[(h1.index > bar_ts) & (h1.index <= eval_end)]
        # Окно для замера MAE — «что было бы без стопа»: до цели, но не дальше
        # горизонта и не дальше закрытия недели (после него сделки уже нет).
        mae_end = bar_ts + timedelta(hours=horizon)
        if deadline is not None:
            mae_end = min(mae_end, deadline)
        mae_window = future[future.index <= mae_end]

        for name, kind, value in STOP_VARIANTS:
            settings = _variant_settings(base, kind, value, float(atr.iloc[i]))
            if settings is None:
                continue
            settings["WEEK_FILTER"] = week_filter
            # None отключает фильтр цены входа — прогон «как было», для сравнения.
            settings["MAX_RISK_ATR"] = config.MAX_RISK_ATR if risk_filter else None
            for detector in (pattern_detector.detect_spring, pattern_detector.detect_upthrust):
                signal = detector(window, levels, trend, settings)
                if signal is None:
                    continue
                key = (name, signal["pattern"], signal["direction"])
                prev = last_emit.get(key)
                if prev is not None and bar_ts - prev < timedelta(minutes=config.SIGNAL_DEDUP_MIN):
                    continue
                last_emit[key] = bar_ts

                candle = h1.iloc[i]
                signal["break_extreme"] = float(
                    candle["low"] if signal["direction"] == "long" else candle["high"]
                )
                signal["atr"] = float(atr.iloc[i])
                # Горизонт мы уже отмотали целиком (проверка выше), поэтому
                # 'pending' здесь означает ровно «не дошёл никуда» = истёк.
                res = pattern_detector.evaluate_signal_detailed(
                    signal, future, week_close=week_filter)
                signal["outcome"] = "expired" if res["status"] == "pending" else res["status"]
                signal["exit_price"] = res["exit_price"]
                signal["risk"] = abs(signal["entry_price"] - signal["stop_loss"])
                signal["rr"] = (abs(signal["take_profit"] - signal["entry_price"])
                                / signal["risk"] if signal["risk"] else 0.0)
                signal.update(measure_excursion(signal, mae_window))
                results[name].append(signal)
    return results


# ── Отчёт ───────────────────────────────────────────────────────────────────

def summarize(signals: list[dict]) -> dict:
    """Сводка по варианту: винрейт, итог в R, профит-фактор, «выбило и поехало».

    Итог в R: цель даёт +фактический R:R, стоп −1, истёкшие 0. Считаем в R, а не в
    деньгах, чтобы варианты с разной шириной стопа были сравнимы между собой.
    """
    def trade_r(s: dict) -> float:
        """Результат сделки в R. Закрытая по неделе считается по фактической цене
        выхода (её R — любой между −1 и +R:R), как и в /stats у бота."""
        if s["outcome"] == "hit_tp":
            return s["rr"]
        if s["outcome"] == "hit_sl":
            return -1.0
        if s["outcome"] == "closed_week" and s.get("exit_price") is not None:
            return trading_week.realized_r(s["direction"], s["entry_price"],
                                           s["stop_loss"], s["exit_price"])
        return 0.0

    per_trade = [trade_r(s) for s in signals]
    tp = [s for s in signals if trade_r(s) > 0]
    sl = [s for s in signals if trade_r(s) < 0]
    week = [s for s in signals if s["outcome"] == "closed_week"]
    other = [s for s in signals if s["outcome"] in ("expired", "pending")]
    plus = sum(r for r in per_trade if r > 0)
    minus = -sum(r for r in per_trade if r < 0)
    decided = len(tp) + len(sl)
    # Погрешность итога в R. Без неё таблицу легко переоценить: на сотне сделок
    # разброс результата — десятки R, и «лучший» вариант может отличаться от
    # соседнего просто по случайности. Если разница вариантов меньше ±, выбирать
    # между ними по этим цифрам нельзя.
    se = float(pd.Series(per_trade).std() * len(per_trade) ** 0.5) if len(per_trade) > 1 else 0.0
    # Главная метрика под наблюдение «стоп выбило, а потом пошло куда надо»:
    # сделка закрылась по стопу, хотя без стопа дошла бы до цели.
    swept = [s for s in sl if s["reached_tp"]]
    return {
        "n": len(signals), "tp": len(tp), "sl": len(sl), "other": len(other),
        "week": len(week),
        "winrate": len(tp) / decided if decided else 0.0,
        "total_r": plus - minus, "se": se,
        "pf": (plus / minus) if minus else (float("inf") if plus else 0.0),
        "avg_risk_pct": (sum(s["risk"] / s["entry_price"] for s in signals) / len(signals)
                         if signals else 0.0),
        "swept": len(swept),
        "swept_pct": len(swept) / len(signals) if signals else 0.0,
    }


def _q(values: list[float], p: float) -> float:
    return float(pd.Series(values).quantile(p)) if values else float("nan")


def report_mae(signals: list[dict]) -> None:
    """Замер MAE по сигналам, которые без стопа дошли бы до цели, и вывод:
    какой буфер сколько таких сигналов переживает."""
    savable = [s for s in signals if s["reached_tp"]]
    print("\n  ЗАМЕР MAE — насколько цена уходит против входа, прежде чем дойти до цели")
    print(f"  Набор сигналов — текущих настроек (буфер {config.STOP_SPREAD * 100:.2f}%); "
          f"из них без стопа дошли бы до цели: {len(savable)} из {len(signals)}")
    if not savable:
        print("  Таких сигналов нет — мерить нечего.")
        return

    pct = [s["mae_abs"] / s["entry_price"] * 100 for s in savable]
    in_atr = [s["mae_abs"] / s["atr"] for s in savable if s["atr"]]
    print(f"    ход ниже/выше экстремума свечи пробоя, % от цены: "
          f"медиана {_q(pct, .5):.3f}%  75% {_q(pct, .75):.3f}%  "
          f"90% {_q(pct, .90):.3f}%  95% {_q(pct, .95):.3f}%")
    if in_atr:
        print(f"    то же в ATR({ATR_PERIOD}) на H1:                        "
              f"медиана {_q(in_atr, .5):.2f}   75% {_q(in_atr, .75):.2f}   "
              f"90% {_q(in_atr, .90):.2f}   95% {_q(in_atr, .95):.2f}")

    cur = config.STOP_SPREAD * 100
    covered = sum(1 for p in pct if p <= cur) / len(pct)
    print(f"\n    Текущий буфер {cur:.2f}% переживает {covered:.0%} таких свипов.")
    print(f"    Чтобы пережить 90% — нужно {_q(pct, .90):.3f}%"
          + (f" (≈ ATR×{_q(in_atr, .90):.2f})" if in_atr else ""))


def report_variants(results: dict[str, list[dict]]) -> None:
    """Таблица сравнения вариантов стопа. Главные колонки — «итог R» (по нему и
    выбирают) и «выбило-и-поехало» (сколько стоп забрал сделок, которые были бы в плюс)."""
    print(f"\n  СРАВНЕНИЕ ВАРИАНТОВ СТОПА (полный прогон детектора на каждый вариант)")
    print(f"  {'буфер':<16}{'сигн.':>6}{'плюс':>6}{'минус':>6}{'нед.':>6}{'проч.':>6}"
          f"{'винрейт':>9}{'итог R':>16}{'ПФ':>7}{'ср.риск':>9}{'выбило-и-поехало':>19}")
    print("  " + "─" * 105)
    best = None
    for name, _, _ in STOP_VARIANTS:
        s = summarize(results.get(name, []))
        if s["n"] == 0:
            print(f"  {name:<16}{'—':>6}  (сигналов нет)")
            continue
        pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  {name:<16}{s['n']:>6}{s['tp']:>6}{s['sl']:>6}{s['week']:>6}{s['other']:>6}"
              f"{s['winrate']:>8.0%}{s['total_r']:>+11.1f} ±{s['se']:<4.0f}{pf:>7}"
              f"{s['avg_risk_pct'] * 100:>8.2f}%"
              f"{s['swept']:>13} ({s['swept_pct']:.0%})")
        if best is None or s["total_r"] > best[1]["total_r"]:
            best = (name, s)
    if best:
        print(f"\n    Лучший по итогу в R: «{best[0]}» → {best[1]['total_r']:+.1f}R "
              f"±{best[1]['se']:.0f} на {best[1]['n']} сигналах.")
        print("    ± — разброс от размера выборки. Варианты, чьи итоги отличаются "
              "меньше чем на ±, различить\n    на этих данных нельзя — выбирать между "
              "ними по таблице будет подгонкой под историю.")


def report_instrument(code: str, h1: pd.DataFrame, source: str, base: dict,
                      results: dict[str, list[dict]]) -> None:
    info = resolve(code)
    span = (h1.index[-1] - h1.index[0]).days
    print("\n" + "═" * 96)
    print(f"  {info['name']} ({code})")
    print(f"  источник: {source}, {len(h1)} часовых свечей, "
          f"{h1.index[0]:%Y-%m-%d} … {h1.index[-1]:%Y-%m-%d} ({span} дн.)")
    print(f"  пороги: VOL_MULT={base['VOL_MULT']}  "
          f"BREAK_PCT={base['BREAK_PCT'] * 100:.3f}%  MIN_RR={base['MIN_RR']}")
    print("═" * 96)
    baseline = results.get(STOP_VARIANTS[0][0], [])
    if not baseline:
        print("\n  Сигналов на этой истории не нашлось — снизь VOL_MULT/BREAK_PCT "
              "или возьми больше свечей.")
        return
    report_mae(baseline)
    report_variants(results)


def report_overall(per_instrument: dict[str, dict[str, list[dict]]]) -> None:
    """Итог в R по каждому варианту стопа на всех инструментах сразу.

    Смысл: вариант, выигравший на одном инструменте, но проигравший на трёх —
    это подгонка, а не находка. Ищем тот, что не разваливается нигде.
    """
    if len(per_instrument) < 2:
        return
    codes = list(per_instrument)
    print("\n" + "═" * 96)
    print("  СВОДНО ПО ВСЕМ ИНСТРУМЕНТАМ — итог в R")
    print("═" * 96)
    print(f"  {'буфер':<16}" + "".join(f"{c:>10}" for c in codes)
          + f"{'сумма':>10}{'сигналов':>11}{'выбило-и-поехало':>19}")
    print("  " + "─" * 94)
    for name, _, _ in STOP_VARIANTS:
        cells, total, n_all, swept_all = [], 0.0, 0, 0
        for code in codes:
            s = summarize(per_instrument[code].get(name, []))
            cells.append(f"{s['total_r']:>+10.1f}" if s["n"] else f"{'—':>10}")
            total += s["total_r"]
            n_all += s["n"]
            swept_all += s["swept"]
        share = swept_all / n_all if n_all else 0.0
        print(f"  {name:<16}" + "".join(cells)
              + f"{total:>+10.1f}{n_all:>11}{swept_all:>13} ({share:.0%})")
    print("\n    «Выбило-и-поехало» — сколько сделок стоп закрыл в минус, хотя без него\n"
          "    цена дошла бы до цели. Это и есть цена узкого стопа.")


def report_split(per_instrument: dict[str, dict[str, list[dict]]], mids: dict[str, pd.Timestamp]) -> None:
    """Проверка на устойчивость: калибровка на первой половине истории, проверка на второй.

    Смысл. Выбирать буфер по лучшему итогу на всей истории — это подгонка: перебор
    из десятка вариантов даст «победителя» просто по случайности. Честная
    проверка одна: выбрать по первой половине данных и посмотреть, что этот выбор
    дал на второй, которую он не видел. Вариант, выигрывающий только на своей
    половине, брать нельзя.
    """
    print("\n" + "═" * 96)
    print("  ПРОВЕРКА НА УСТОЙЧИВОСТЬ — калибровка на 1-й половине, проверка на 2-й")
    print("═" * 96)
    print(f"  {'буфер':<16}{'калибровка R':>16}{'сигн.':>7}"
          f"{'проверка R':>14}{'сигн.':>7}{'оба плюсовые':>16}")
    print("  " + "─" * 76)

    rows = []
    for name, _, _ in STOP_VARIANTS:
        first, second = [], []
        for code, results in per_instrument.items():
            mid = mids.get(code)
            for s in results.get(name, []):
                (first if pd.Timestamp(s["bar_time"]) < mid else second).append(s)
        a, b = summarize(first), summarize(second)
        ok = "да" if a["total_r"] > 0 and b["total_r"] > 0 else "нет"
        rows.append((name, a, b, ok))
        print(f"  {name:<16}{a['total_r']:>+12.1f} ±{a['se']:<3.0f}{a['n']:>7}"
              f"{b['total_r']:>+10.1f} ±{b['se']:<3.0f}{b['n']:>7}{ok:>14}")

    calibrated = max(rows, key=lambda r: r[1]["total_r"])
    print(f"\n    Лучший на калибровке: «{calibrated[0]}» ({calibrated[1]['total_r']:+.1f}R). "
          f"На проверке он дал {calibrated[2]['total_r']:+.1f}R.")
    survivors = [r for r in rows if r[3] == "да"]
    if survivors:
        pick = max(survivors, key=lambda r: min(r[1]["total_r"], r[2]["total_r"]))
        print(f"    Плюсовые на обеих половинах: {', '.join(r[0] for r in survivors)}.")
        print(f"    Самый ровный (лучший худший результат): «{pick[0]}» — "
              f"{pick[1]['total_r']:+.1f}R и {pick[2]['total_r']:+.1f}R.")
    else:
        print("    Ни один вариант не вышел в плюс на обеих половинах — "
              "устойчивого выбора эти данные не дают.")


def write_csv(path: str, rows: list[dict]) -> None:
    cols = ["instrument", "variant", "bar_time", "pattern", "direction", "priority",
            "entry_price", "stop_loss", "take_profit", "risk", "rr", "outcome",
            "exit_price", "reached_tp", "mae_abs", "mae_from_entry", "mfe_abs",
            "atr", "bars"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"\nПодробности по каждому сигналу: {path} ({len(rows)} строк)")


# ── CLI ─────────────────────────────────────────────────────────────────────

async def run(codes: list[str], bars: int, base: dict, horizon: int,
              csv_path: str | None, week_filter: bool = True,
              trend_d1: bool = False, use_cache: bool = True,
              risk_filter: bool = True) -> None:
    print("Недельное окно: " + ("ВКЛ — входы пн–чт, пятничное закрытие гасит сделку"
                                if week_filter else "ВЫКЛ — как было до окна"))
    print("Фильтр тренда:  " + ("по ДНЕВКЕ (D1) — как было до недельного горизонта"
                                if trend_d1 else "по ЧАСОВИКУ (H1) — как в бою"))
    days = "".join("пнвтсрчтптсбвс"[i * 2:i * 2 + 2] for i in range(7)
                   if i not in config.NO_ENTRY_WEEKDAYS)
    print(f"Окно входа:     {days or 'нет разрешённых дней'}")
    print("Фильтр входа:   " + (f"риск ≤ {config.MAX_RISK_ATR}×ATR — как в бою"
                                if risk_filter else "ВЫКЛ — как было до фильтра"))
    all_rows: list[dict] = []
    per_instrument: dict[str, dict[str, list[dict]]] = {}
    mids: dict[str, pd.Timestamp] = {}   # середина истории — граница калибровка/проверка
    for code in codes:
        try:
            h1, d1, source = await load_history(code, bars, use_cache)
        except Exception as e:
            print(f"\n{code}: не загрузить историю — {e}")
            continue
        if len(h1) < config.H1_LIMIT:
            print(f"\n{code}: слишком мало свечей ({len(h1)}) — пропускаем")
            continue
        print(f"\n{code}: прогоняю {len(h1)} свечей × {len(STOP_VARIANTS)} вариантов стопа…")
        results = replay(h1, d1, base, horizon, week_filter, trend_d1, risk_filter)
        per_instrument[code] = results
        mids[code] = h1.index[0] + (h1.index[-1] - h1.index[0]) / 2
        report_instrument(code, h1, source, base, results)
        for variant, signals in results.items():
            for s in signals:
                all_rows.append({**s, "instrument": code, "variant": variant})
    report_overall(per_instrument)
    report_split(per_instrument, mids)
    if csv_path and all_rows:
        write_csv(csv_path, all_rows)
    # Форекс грузится штатным data_fetcher — закрываем его соединения с биржей
    # (иначе ccxt ругается на незакрытый коннектор). Своя биржа _fetch_paged
    # закрывается сама.
    import data_fetcher
    await data_fetcher.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Бэктест сигналов движка: калибровка стопа по историческим свечам",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("codes", nargs="*", help=f"коды инструментов ({', '.join(engine_codes())})")
    p.add_argument("--all", action="store_true", help="все инструменты движка")
    p.add_argument("--bars", type=int, default=8760, help="сколько H1-свечей истории (по умолчанию 8760 ≈ год)")
    p.add_argument("--horizon", type=int, default=config.SIGNAL_EXPIRE_HOURS,
                   help=f"горизонт жизни сигнала в часах (по умолчанию {config.SIGNAL_EXPIRE_HOURS})")
    p.add_argument("--user", type=int, help="взять пороги из личных настроек этого пользователя")
    p.add_argument("--vol-mult", type=float, help="переопределить VOL_MULT")
    p.add_argument("--break-pct", type=float, help="переопределить BREAK_PCT")
    p.add_argument("--min-rr", type=float, help="переопределить MIN_RR")
    p.add_argument("--csv", help="выгрузить все сигналы в CSV")
    p.add_argument("--no-week", action="store_true",
                   help="отключить недельное окно (входы пн–чт + гашение в пятницу) — "
                        "прогон «как было», для сравнения")
    p.add_argument("--trend-d1", action="store_true",
                   help="фильтровать направление по дневному тренду вместо часового — "
                        "прогон «как было до недельного горизонта», для сравнения")
    p.add_argument("--entry-days",
                   help="дни недели, когда вход РАЗРЕШЁН (0=пн): «0,1,2» — только пн–ср. "
                        "По умолчанию из config.NO_ENTRY_WEEKDAYS (сейчас пн–чт). "
                        "Нужно, чтобы подобрать окно входа замером, а не на глаз")
    p.add_argument("--no-risk-filter", action="store_true",
                   help="отключить фильтр цены входа (риск ≤ MAX_RISK_ATR×ATR) — "
                        "прогон «как было», для сравнения")
    p.add_argument("--no-cache", action="store_true",
                   help="не использовать кеш истории в .backtest_cache/ — скачать заново")
    args = p.parse_args()

    # Окно входа подменяем в config: trading_week читает его на каждый вызов.
    if args.entry_days:
        allowed = {int(x) for x in args.entry_days.split(",")}
        config.NO_ENTRY_WEEKDAYS = tuple(d for d in range(7) if d not in allowed)

    codes = engine_codes() if args.all else [c.upper() for c in args.codes]
    if not codes:
        p.print_help()
        sys.exit(1)
    unknown = [c for c in codes if c not in INSTRUMENTS or not data_source(c)]
    if unknown:
        print(f"Не инструменты движка: {', '.join(unknown)}. Доступны: {', '.join(engine_codes())}")
        sys.exit(1)

    base = config.effective(database.get_user_settings(args.user) if args.user else None)
    for key, val in (("VOL_MULT", args.vol_mult), ("BREAK_PCT", args.break_pct),
                     ("MIN_RR", args.min_rr)):
        if val is not None:
            base[key] = val

    asyncio.run(run(codes, args.bars, base, args.horizon, args.csv,
                    not args.no_week, args.trend_d1, not args.no_cache,
                    not args.no_risk_filter))


if __name__ == "__main__":
    main()
