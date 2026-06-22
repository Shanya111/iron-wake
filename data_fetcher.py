"""Загрузка биржевых свечей (OHLCV с объёмом) через CCXT + кеш в памяти.

Зачем CCXT, а не yfinance: VSA и паттерн Spring критически зависят от объёма, а у
Yahoo по форекс-парам объём = 0/пусто. На крипте биржевой объём настоящий — поэтому
торговые стратегии строим на данных бирж (Binance, Bybit) через CCXT.

Свечи кешируются на config.CACHE_TTL[timeframe], чтобы мониторинг (каждые 5 минут)
не делал лишних сетевых запросов. Биржи создаются лениво и переиспользуются.
"""

import os
import time

import ccxt.async_support as ccxt
import pandas as pd

import config

# Прокси для бирж. Многие биржи (Binance) блокируют запросы из РФ (HTTP 451),
# поэтому на сервере задаём CCXT_PROXY — прокси в разрешённой стране. Поддерживает
# http(s):// и socks5:// (socks работает через уже установленный aiohttp-socks).
# Если переменной нет — ходим напрямую (как на разрешённой локали / в CI).
_PROXY = os.getenv("CCXT_PROXY", "").strip()

# Биржи: одна на имя (binance, bybit, ...), создаём при первом обращении.
_exchanges: dict[str, "ccxt.Exchange"] = {}
# Кеш свечей: (биржа, символ, таймфрейм) → (время_загрузки, DataFrame).
_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}


def _get_exchange(name: str):
    ex = _exchanges.get(name)
    if ex is None:
        opts = {"enableRateLimit": True, "timeout": 15000}
        if _PROXY:
            # ccxt различает socks и http(s) прокси разными полями.
            opts["socksProxy" if _PROXY.startswith("socks") else "httpsProxy"] = _PROXY
        ex = getattr(ccxt, name)(opts)
        _exchanges[name] = ex
    return ex


async def get_candles(
    symbol: str, timeframe: str, limit: int, exchange: str = "binance"
) -> pd.DataFrame:
    """OHLCV-свечи как DataFrame со столбцами open/high/low/close/volume (индекс — время).

    Кеш на config.CACHE_TTL[timeframe]. Бросает исключение, если биржа не отдала
    данных (нет такого символа / сеть) — вызывающий код это ловит и пропускает пару.
    """
    key = (exchange, symbol, timeframe)
    ttl = config.CACHE_TTL.get(timeframe, 300)
    cached = _cache.get(key)
    if cached is not None and time.time() - cached[0] < ttl:
        return cached[1]

    ex = _get_exchange(exchange)
    raw = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not raw:
        raise ValueError(f"нет данных по {symbol} ({exchange})")
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"])
    _cache[key] = (time.time(), df)
    return df


async def close() -> None:
    """Закрыть соединения всех бирж. Вызывать при остановке бота."""
    for ex in _exchanges.values():
        try:
            await ex.close()
        except Exception:
            pass
