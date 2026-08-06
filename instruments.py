"""Реестр инструментов для алертов.

Единый источник правды: какие пары показываем кнопками, какой у них тикер Yahoo
и с какой точностью отображаем цену. Импортируется и в bot.py (UI/клавиатуры),
и в database.py (получение котировок).
"""

# Код пары → отображаемое имя, тикер Yahoo Finance, число знаков после запятой.
# Тикеры проверены на живых данных (все отдают минутные свечи для логики касания).
#
# Поле "ccxt" — это {символ, биржа} для торгового движка (анализ VSA + Spring): у пары
# есть настоящий биржевой объём. ВСЕ инструменты движка на ОДНОЙ бирже — Kraken:
#   • Kraken не блокирует РФ (работает без прокси, в отличие от Binance 451 / Bybit 403);
#   • у Kraken есть и крипта (BTC/ETH/SOL/TON), и форекс с реальным объёмом (EUR/USD,
#     GBP/USD, AUD/USD, USD/CAD) — поэтому одна биржа закрывает весь движок.
# USD/JPY намеренно НЕ добавляем: на Kraken пара почти не торгуется (мёртвая ликвидность —
# застрявшая цена ~163 вместо реальных ~161.7, спред ~3%, больше половины часовых свечей
# плоские), а у Yahoo по форекс-споту нет объёма для VSA. Поэтому йены в боте нет совсем.
# Золото/нефть — фьючерсы, на спот-Kraken их нет, поэтому биржевого объёма (ccxt) у них
# нет. Но Yahoo отдаёт по ним почасовой объём (частичный, но для VSA важен относительный
# всплеск) — поэтому они входят в движок с источником "yahoo": анализ и сигналы считаются
# по свечам Yahoo. Стакана (DOM) у Yahoo нет → этот блок для них пропускается.
# Простые алерты «касание уровня» работают по всем парам через yfinance (ticker).
INSTRUMENTS = {
    "EURUSD": {"name": "EUR/USD",       "ticker": "EURUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "EUR/USD", "exchange": "kraken"}},
    "GBPUSD": {"name": "GBP/USD",       "ticker": "GBPUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "GBP/USD", "exchange": "kraken"}},
    "AUDUSD": {"name": "AUD/USD",       "ticker": "AUDUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "AUD/USD", "exchange": "kraken"}},
    "USDCAD": {"name": "USD/CAD",       "ticker": "USDCAD=X",     "decimals": 4,
               "ccxt": {"symbol": "USD/CAD", "exchange": "kraken"}},
    "GOLD":   {"name": "Золото",        "ticker": "GC=F",         "decimals": 2,
               "source": "yahoo"},
    "BRENT":  {"name": "Нефть Brent",   "ticker": "BZ=F",         "decimals": 2,
               "source": "yahoo"},
    "BTC":    {"name": "Bitcoin",       "ticker": "BTC-USD",      "decimals": 2,
               "ccxt": {"symbol": "BTC/USD", "exchange": "kraken"}},
    "SOL":    {"name": "Solana",        "ticker": "SOL-USD",      "decimals": 2,
               "ccxt": {"symbol": "SOL/USD", "exchange": "kraken"}},
    "ETH":    {"name": "Ethereum",      "ticker": "ETH-USD",      "decimals": 2,
               "ccxt": {"symbol": "ETH/USD", "exchange": "kraken"}},
    "TON":    {"name": "Toncoin (TON)", "ticker": "TON11419-USD", "decimals": 4,
               "ccxt": {"symbol": "TON/USD", "exchange": "kraken"}},
    # ── Расширение крипто-набора (6 августа 2026) ───────────────────────────────
    # Зачем: владельцу нужно больше сигналов, а ослаблять фильтры отбора нельзя —
    # каждое ослабление измерено и каждое уводит счёт вниз (см. «Возврат к частым
    # сигналам» в CLAUDE.md). Единственный честный способ — расширить поле поиска:
    # больше инструментов при той же планке качества.
    #
    # Отбор пар — по ДВУМ условиям сразу, оба обязательны:
    #   1. пара есть на Kraken (боевой источник свечей и стакана);
    #   2. пара есть на Bitfinex с годовой почасовой историей — иначе её нечем
    #      проверить, а брать инструмент в бой непроверенным мы не будем.
    # Отсеялись по второму условию: ATOM, OP, INJ, TIA (на Bitfinex их нет).
    #
    # Порог ликвидности — оборот Kraken не ниже TON (~1.1 млн $/сутки), самой тонкой
    # из уже принятых пар. Это не вкусовщина: на USD/JPY мы уже обожглись — там
    # мёртвая ликвидность давала застрявшую цену и плоские свечи, а VSA по плоским
    # свечам не работает вовсе. Не прошли порог: TRX (0.70), NEAR (0.50), BCH (0.37),
    # DOT (0.28), POL (0.28), ALGO (0.28), FIL (0.28), ARB (0.12), SEI (0.11),
    # ETC (0.04), APT (0.03) — все в млн $/сутки.
    #
    # Тикеры Yahoo проверены на живых данных. У UNI и SUI обычные имена заняты
    # акциями, поэтому числовой суффикс — как у TON.
    "XRP":    {"name": "XRP",           "ticker": "XRP-USD",      "decimals": 4,
               "ccxt": {"symbol": "XRP/USD", "exchange": "kraken"}},
    "ADA":    {"name": "Cardano",       "ticker": "ADA-USD",      "decimals": 4,
               "ccxt": {"symbol": "ADA/USD", "exchange": "kraken"}},
    "XLM":    {"name": "Stellar",       "ticker": "XLM-USD",      "decimals": 4,
               "ccxt": {"symbol": "XLM/USD", "exchange": "kraken"}},
    "AVAX":   {"name": "Avalanche",     "ticker": "AVAX-USD",     "decimals": 2,
               "ccxt": {"symbol": "AVAX/USD", "exchange": "kraken"}},
    "SUI":    {"name": "Sui",           "ticker": "SUI20947-USD", "decimals": 4,
               "ccxt": {"symbol": "SUI/USD", "exchange": "kraken"}},
    "UNI":    {"name": "Uniswap",       "ticker": "UNI7083-USD",  "decimals": 4,
               "ccxt": {"symbol": "UNI/USD", "exchange": "kraken"}},
    "LTC":    {"name": "Litecoin",      "ticker": "LTC-USD",      "decimals": 2,
               "ccxt": {"symbol": "LTC/USD", "exchange": "kraken"}},
    "AAVE":   {"name": "Aave",          "ticker": "AAVE-USD",     "decimals": 2,
               "ccxt": {"symbol": "AAVE/USD", "exchange": "kraken"}},
    "DOGE":   {"name": "Dogecoin",      "ticker": "DOGE-USD",     "decimals": 5,
               "ccxt": {"symbol": "DOGE/USD", "exchange": "kraken"}},
    "LINK":   {"name": "Chainlink",     "ticker": "LINK-USD",     "decimals": 2,
               "ccxt": {"symbol": "LINK/USD", "exchange": "kraken"}},
}


def fmt(value: float, decimals: int) -> str:
    """Цена/уровень в строку с нужным числом знаков после запятой."""
    return f"{value:.{decimals}f}"


def infer_decimals(price: float) -> int:
    """Точность для своей пары — подбираем по величине цены,
    т.к. заранее число знаков неизвестно."""
    if price >= 100:
        return 2
    if price >= 1:
        return 4
    return 6


def resolve(pair: str) -> dict:
    """Данные инструмента по сохранённому значению `pair`.

    Реестровый код (USDJPY, BTC...) — берём из INSTRUMENTS.
    Иначе это своя пара: `pair` — сырой тикер Yahoo, имя = тикер,
    точность не фиксирована (decimals=None → подбор по цене через infer_decimals).
    """
    info = INSTRUMENTS.get(pair)
    if info is not None:
        return {"name": info["name"], "ticker": info["ticker"], "decimals": info["decimals"]}
    return {"name": pair, "ticker": pair, "decimals": None}


def engine_codes() -> list[str]:
    """Коды инструментов движка (анализ + сигналы) — у кого есть источник объёма.
    Крипта/форекс — биржевой объём Kraken (ccxt). Золото/нефть — почасовой объём
    Yahoo (source='yahoo'). Своя пара и инструменты без объёма в движок не входят."""
    return [code for code in INSTRUMENTS if data_source(code)]


def data_source(code: str) -> str | None:
    """Откуда движок берёт свечи с объёмом по инструменту:
    'ccxt' — биржа Kraken (крипта/форекс), 'yahoo' — свечи Yahoo (золото/нефть),
    None — инструмент в движок не входит (своя пара и т.п.)."""
    info = INSTRUMENTS.get(code)
    if not info:
        return None
    if "ccxt" in info:
        return "ccxt"
    if info.get("source") == "yahoo":
        return "yahoo"
    return None


def ccxt_symbol(code: str) -> dict | None:
    """{'symbol': 'BTC/USD', 'exchange': 'kraken'} или None, если у пары нет
    биржевого объёма (золото/нефть — у них источник Yahoo; своя пара)."""
    info = INSTRUMENTS.get(code)
    return info.get("ccxt") if info else None
