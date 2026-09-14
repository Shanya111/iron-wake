"""Стратегия №4 — ТРЕНД ПО НЕДЕЛЬНОМУ КАНАЛУ (14 сентября 2026).

Вторая торговая стратегия бота, отдельная от Spring/Upthrust и устроенная наоборот:
не ловит ЛОЖНЫЙ пробой уровня, а идёт ЗА настоящим пробоем недели.

Правила — ровно те, что мерились в лаборатории (eth_quant.py, eth_robust.py):
  • ВХОД: часовая свеча ЗАКРЫЛАСЬ выше максимума предыдущих 168 часов → лонг;
    ниже минимума предыдущих 168 часов → шорт. Вход по рынку на открытии
    следующего часа.
  • СТОП: 3 ATR от цены входа (ATR — среднее истинного диапазона за 24 часа на
    момент сигнала). Стоп не двигается. Если свеча открылась уже за стопом —
    закрытие по цене открытия, а не по стопу.
  • ВЫХОД: часовая свеча закрылась ниже минимума предыдущих 42 часов (лонг) или
    выше максимума 42 часов (шорт) → закрытие на открытии следующего часа.
    Цели нет: тренд ведётся, пока идёт.
  • Одна позиция на инструмент. После СТОПА новый вход возможен на закрытии той
    же свечи; после ВЫХОДА ПО КАНАЛУ — не раньше следующей. Так было в замере.

У стратегии нет личных настроек, поэтому позиция ОДНА на инструмент и общая для
всех — её ведёт scheduler.monitor_trend, а сообщения получают подписчики.

Модуль — чистые функции без сети и базы: step() берёт свечи и состояние, отдаёт
новое состояние и события. Поэтому его можно проверить тестами на синтетике и
сверить с замером сделка в сделку.
"""

import math

import pandas as pd

import config

# С какой свечи правила применимы: до неё нет ни полного канала, ни полного ATR.
WARM = max(config.TREND_CHANNEL_BARS, config.TREND_ATR_BARS) + 1

# Минимальный риск сделки в долях цены. Стоп ближе — это не сетап, а сбой данных;
# лаборатория отбрасывала такие сигналы тем же порогом.
MIN_RISK = 0.0005


def channels(df: pd.DataFrame) -> dict:
    """Каналы и ATR на каждую свечу, в виде массивов по индексу свечей.

    hi / lo   — максимум / минимум 168 часов ДО свечи (сама свеча не входит):
                её закрытие за этой границей и есть пробой недели;
    xhi / xlo — то же за 42 часа до свечи: встречный канал выхода;
    atr       — среднее истинного диапазона за 24 часа ВКЛЮЧАЯ свечу, как в замере.
    """
    n, m = config.TREND_CHANNEL_BARS, config.TREND_EXIT_BARS
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return {
        "hi": high.shift(1).rolling(n).max().to_numpy(),
        "lo": low.shift(1).rolling(n).min().to_numpy(),
        "xhi": high.shift(1).rolling(m).max().to_numpy(),
        "xlo": low.shift(1).rolling(m).min().to_numpy(),
        "atr": tr.rolling(config.TREND_ATR_BARS).mean().to_numpy(),
    }


def _sign(position: dict) -> int:
    return 1 if position["direction"] == "long" else -1


def _closed(position: dict, kind: str, price: float, when, level: float | None) -> dict:
    """Событие закрытия позиции: 'stop' или 'exit'. result_r — итог в рисках (без издержек)."""
    d = _sign(position)
    risk = abs(position["entry_price"] - position["stop_loss"])
    return {
        "type": kind,
        "direction": position["direction"],
        "entry_price": position["entry_price"],
        "stop_loss": position["stop_loss"],
        "entry_time": position["entry_time"],
        "exit_price": float(price),
        "exit_time": str(when),
        "level": None if level is None else float(level),
        "result_r": (float(price) - position["entry_price"]) * d / risk if risk else 0.0,
    }


def step(df: pd.DataFrame, last_bar: str | None,
         position: dict | None) -> tuple[str | None, dict | None, list[dict]]:
    """Прогоняет правила по ЗАКРЫТЫМ свечам, которых модель ещё не видела.

    df       — часовые свечи биржи; последняя строка считается формирующейся;
    last_bar — время последней обработанной свечи (строка) или None при первом запуске;
    position — открытая позиция {direction, entry_price, stop_loss, entry_time, ...}
               или None.

    Возвращает (новый last_bar, позиция после шага, события). События:
      entry — открыта позиция (цена входа = открытие следующего часа);
      stop  — сработал стоп; exit — выход по встречному каналу.

    ПЕРВЫЙ ЗАПУСК начинает с чистого листа: запоминаем последнюю закрытую свечу и
    ничего не открываем. Иначе бот объявил бы вход по пробою, случившемуся до его
    запуска, по цене, которой на рынке давно нет.

    Свечи обрабатываются по одной и строго по порядку, поэтому неважно, сколько
    новых свечей пришло за раз: одна за пять минут или сорок после простоя —
    результат тот же, что при прогоне всей истории одним куском (на это есть тест).
    """
    if len(df) < 2:
        return last_bar, position, []
    ts = df.index
    last_closed = len(df) - 2
    if last_bar is None:
        return str(ts[last_closed]), position, []

    start = max(int(ts.searchsorted(pd.Timestamp(last_bar), side="right")), WARM)
    if start > last_closed:
        return last_bar, position, []

    ind = channels(df)
    o, h, lo_, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    m = config.TREND_EXIT_BARS
    events: list[dict] = []

    for i in range(start, last_closed + 1):
        exited_by_channel = False
        if position is not None:
            d = _sign(position)
            stop = position["stop_loss"]
            # Стоп проверяется только на свечах, где позиция уже была.
            if pd.Timestamp(position["entry_time"]) <= ts[i]:
                hit = lo_[i] <= stop if d > 0 else h[i] >= stop
                if hit:
                    price = min(stop, o[i]) if d > 0 else max(stop, o[i])
                    events.append(_closed(position, "stop", price, ts[i], None))
                    position = None
            if position is not None:
                level = ind["xlo"][i] if d > 0 else ind["xhi"][i]
                if (c[i] < level) if d > 0 else (c[i] > level):
                    events.append(_closed(position, "exit", o[i + 1], ts[i + 1], level))
                    position = None
                    exited_by_channel = True

        if position is None and not exited_by_channel:
            atr, hi, lo = ind["atr"][i], ind["hi"][i], ind["lo"][i]
            if atr > 0 and not math.isnan(hi) and not math.isnan(lo):
                d = 1 if c[i] > hi else -1 if c[i] < lo else 0
                if d:
                    fill = float(o[i + 1])
                    stop = fill - d * config.TREND_STOP_ATR * atr
                    if (fill - stop) * d > fill * MIN_RISK:
                        position = {
                            "direction": "long" if d > 0 else "short",
                            "entry_price": fill,
                            "stop_loss": float(stop),
                            "entry_time": str(ts[i + 1]),
                            "signal_bar": str(ts[i]),
                        }
                        # Граница выхода, которую проверит закрытие первого часа в позиции.
                        window = df["low" if d > 0 else "high"].iloc[i + 1 - m: i + 1]
                        events.append({
                            "type": "entry", **position,
                            "level": float(hi if d > 0 else lo),
                            "exit_level": float(window.min() if d > 0 else window.max()),
                        })

    return str(ts[last_closed]), position, events


def snapshot(df: pd.DataFrame, position: dict | None) -> dict:
    """Что модель видит прямо сейчас — для /trend.

    Каналы берутся для ФОРМИРУЮЩЕЙСЯ свечи, то есть по закрытым свечам до неё: это
    ровно те границы, которые проверит её закрытие. open_r — незакрытый итог позиции
    по последней цене, в рисках и без издержек.
    """
    ind = channels(df)
    i = len(df) - 1
    price = float(df["close"].iloc[-1])
    snap = {"price": price, "hi": float(ind["hi"][i]), "lo": float(ind["lo"][i]),
            "xhi": float(ind["xhi"][i]), "xlo": float(ind["xlo"][i])}
    if position:
        d = _sign(position)
        risk = abs(position["entry_price"] - position["stop_loss"])
        snap["open_r"] = (price - position["entry_price"]) * d / risk if risk else 0.0
        snap["exit_level"] = snap["xlo"] if d > 0 else snap["xhi"]
    return snap
