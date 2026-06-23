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
#     GBP/USD, AUD/USD, USD/CAD, USD/JPY) — поэтому одна биржа закрывает весь движок.
# Золото/нефть — фьючерсы, на спот-Kraken их нет → они только в простых алертах.
# Простые алерты «касание уровня» работают по всем парам через yfinance (ticker).
INSTRUMENTS = {
    "USDJPY": {"name": "USD/JPY",       "ticker": "USDJPY=X",     "decimals": 2,
               "ccxt": {"symbol": "USD/JPY", "exchange": "kraken"}},
    "EURUSD": {"name": "EUR/USD",       "ticker": "EURUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "EUR/USD", "exchange": "kraken"}},
    "GBPUSD": {"name": "GBP/USD",       "ticker": "GBPUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "GBP/USD", "exchange": "kraken"}},
    "AUDUSD": {"name": "AUD/USD",       "ticker": "AUDUSD=X",     "decimals": 4,
               "ccxt": {"symbol": "AUD/USD", "exchange": "kraken"}},
    "USDCAD": {"name": "USD/CAD",       "ticker": "USDCAD=X",     "decimals": 4,
               "ccxt": {"symbol": "USD/CAD", "exchange": "kraken"}},
    "GOLD":   {"name": "Золото",        "ticker": "GC=F",         "decimals": 2},
    "BRENT":  {"name": "Нефть Brent",   "ticker": "BZ=F",         "decimals": 2},
    "BTC":    {"name": "Bitcoin",       "ticker": "BTC-USD",      "decimals": 2,
               "ccxt": {"symbol": "BTC/USD", "exchange": "kraken"}},
    "SOL":    {"name": "Solana",        "ticker": "SOL-USD",      "decimals": 2,
               "ccxt": {"symbol": "SOL/USD", "exchange": "kraken"}},
    "ETH":    {"name": "Ethereum",      "ticker": "ETH-USD",      "decimals": 2,
               "ccxt": {"symbol": "ETH/USD", "exchange": "kraken"}},
    "TON":    {"name": "Toncoin (TON)", "ticker": "TON11419-USD", "decimals": 4,
               "ccxt": {"symbol": "TON/USD", "exchange": "kraken"}},
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
    """Коды инструментов с биржевым объёмом (CCXT) — для анализа и сигналов.
    Все на Kraken: крипта (BTC/ETH/SOL/TON) и форекс (EUR/USD, GBP/USD, AUD/USD,
    USD/CAD, USD/JPY) — у всех настоящий объём, в отличие от форекса у Yahoo."""
    return [code for code, info in INSTRUMENTS.items() if "ccxt" in info]


def ccxt_symbol(code: str) -> dict | None:
    """{'symbol': 'BTC/USD', 'exchange': 'kraken'} или None, если у пары нет
    биржевого объёма (золото/нефть/своя пара)."""
    info = INSTRUMENTS.get(code)
    return info.get("ccxt") if info else None
