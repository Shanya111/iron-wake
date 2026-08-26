"""Загрузка биржевых свечей (OHLCV с объёмом) и стакана через CCXT + кеш в памяти.

Зачем CCXT, а не yfinance: VSA и паттерн Spring критически зависят от объёма, а у
Yahoo объём либо отсутствует (форекс-спот), либо неполный (золото/нефть). На бирже
объём настоящий — поэтому торговые стратегии строим на биржевых данных.

Биржа одна — **BingX, бессрочные фьючерсы** (тип swap). Переезд с Kraken-спота
19 августа 2026, причина в комиссии: у мейкера на фьючерсах 0.02% против 0.25% на
Kraken-споте, а при среднем риске сделки ~0.5% от цены эта разница решает, прибылен
движок или нет. BingX из РФ работает напрямую, отдаёт 1000 свечей за запрос (Kraken
отдавал 720) и даёт стакан по всем 21 инструменту движка, включая синтетику на золото и
нефть. Bybit проверен и закрыт: все три его домена отдают 403 по стране.

Свечи кешируются на config.CACHE_TTL[timeframe], чтобы мониторинг (каждые 5 минут)
не делал лишних сетевых запросов. Биржи создаются лениво и переиспользуются.
"""

import os
import time

import ccxt.async_support as ccxt
import pandas as pd

import config

# Прокси для бирж. BingX (наша единственная биржа движка) РФ не блокирует, поэтому
# обычно прокси НЕ нужен. CCXT_PROXY оставлен на случай, если когда-нибудь вернём
# биржу с гео-блоком (Binance даёт 451, Bybit 403) — тогда задаём прокси в
# разрешённой стране. Поддерживает http(s):// и socks5://. Нет переменной — напрямую.
_PROXY = os.getenv("CCXT_PROXY", "").strip()

# Биржи: одна на имя (bingx, ...), создаём при первом обращении.
_exchanges: dict[str, "ccxt.Exchange"] = {}
# Кеш свечей: (биржа, символ, таймфрейм) → (время_загрузки, DataFrame).
_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
# Кеш стакана: (биржа, символ) → (время_загрузки, order_book). Стакан меняется
# быстро, поэтому отдельный короткий TTL (config.ORDERBOOK_TTL).
_ob_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _get_exchange(name: str):
    ex = _exchanges.get(name)
    if ex is None:
        opts = {"enableRateLimit": True, "timeout": 15000}
        if name == "bingx":
            # Бессрочные фьючерсы у BingX лежат в типе swap. Без этого ccxt ищет
            # символ на СПОТЕ: половина наших символов там просто не существует
            # (GRAMTON, NCCOGOLD2USD), а у существующих другие цена и объём.
            opts["options"] = {"defaultType": "swap"}
        if _PROXY:
            # ccxt различает socks и http(s) прокси разными полями.
            opts["socksProxy" if _PROXY.startswith("socks") else "httpsProxy"] = _PROXY
        ex = getattr(ccxt, name)(opts)
        _exchanges[name] = ex
    return ex


async def find_symbol(query: str, exchange: str = "bingx") -> str | None:
    """Ищет бессрочный контракт по тому, что человек написал: «wif» → «WIF/USDT:USDT».

    Нужно для «своей пары». В реестре бота 21 инструмент, а на бирже их 874, и человек
    вправе назвать любой — раньше эту роль играл произвольный тикер Yahoo, теперь её
    играет биржа. Возвращает символ ccxt или None, если такого контракта нет.

    Порядок попыток фиксированный, чтобы результат был предсказуем и объясним:
      1. написали символ целиком («WIF/USDT:USDT») — просто проверяем, что он живой;
      2. монета к USDT — так торгуется основная масса контрактов (825 из 874);
      3. монета к USDC — их полсотни, и часть монет есть только там;
      4. мем-коины с множителем («PEPE» → «1000PEPE/USDT:USDT»): на бирже они идут
         пачками по 1000/10000/1000000 монет, и человек об этом знать не обязан.

    Берём только active swap: спот нам не нужен (там другие цена и объём), а мёртвые
    записи в списке рынков у BingX встречаются — см. историю с TON/TONCOIN/GRAM.
    """
    q = (query or "").strip().upper()
    if not q:
        return None
    ex = _get_exchange(exchange)
    markets = await ex.load_markets()   # ccxt держит их в памяти после первой загрузки
    live = {sym for sym, m in markets.items() if m.get("swap") and m.get("active")}
    if q in live:
        return q
    variants = [f"{q}/USDT:USDT", f"{q}/USDC:USDC"]
    variants += [f"{mult}{q}/USDT:USDT" for mult in ("1000", "10000", "1000000")]
    for variant in variants:
        if variant in live:
            return variant
    return None


async def get_candles(
    symbol: str, timeframe: str, limit: int, exchange: str = "bingx"
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


async def get_order_book(
    symbol: str, limit: int | None = None, exchange: str = "bingx"
) -> dict:
    """Стакан заявок биржи: {'bids': [[цена, объём], ...], 'asks': [...]}.

    Кеш на config.ORDERBOOK_TTL секунд (короткий — стакан живой). Бросает
    исключение при сетевой ошибке / отсутствии символа — вызывающий код ловит.

    ВАЖНО про limit: BingX принимает НЕ любое число уровней. Работают 5, 10, 20,
    50, 100, 500, 1000; на 25 биржа отвечает «Invalid parameters, err:limit».
    Наш config.ORDERBOOK_LIMIT = 50 проверен живым запросом и проходит — но если
    будешь его менять, бери значение из этого списка, иначе стакан молча отвалится.
    """
    if limit is None:
        limit = config.ORDERBOOK_LIMIT
    key = (exchange, symbol)
    cached = _ob_cache.get(key)
    if cached is not None and time.time() - cached[0] < config.ORDERBOOK_TTL:
        return cached[1]
    ex = _get_exchange(exchange)
    ob = await ex.fetch_order_book(symbol, limit=limit)
    _ob_cache[key] = (time.time(), ob)
    return ob


async def close() -> None:
    """Закрыть соединения всех бирж. Вызывать при остановке бота."""
    for ex in _exchanges.values():
        try:
            await ex.close()
        except Exception:
            pass
