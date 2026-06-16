"""Реестр инструментов для алертов.

Единый источник правды: какие пары показываем кнопками, какой у них тикер Yahoo
и с какой точностью отображаем цену. Импортируется и в bot.py (UI/клавиатуры),
и в database.py (получение котировок).
"""

# Код пары → отображаемое имя, тикер Yahoo Finance, число знаков после запятой.
# Тикеры проверены на живых данных (все отдают минутные свечи для логики касания).
INSTRUMENTS = {
    "USDJPY": {"name": "USD/JPY",       "ticker": "USDJPY=X",     "decimals": 2},
    "EURUSD": {"name": "EUR/USD",       "ticker": "EURUSD=X",     "decimals": 4},
    "GBPUSD": {"name": "GBP/USD",       "ticker": "GBPUSD=X",     "decimals": 4},
    "USDCAD": {"name": "USD/CAD",       "ticker": "USDCAD=X",     "decimals": 4},
    "GOLD":   {"name": "Золото",        "ticker": "GC=F",         "decimals": 2},
    "BRENT":  {"name": "Нефть Brent",   "ticker": "BZ=F",         "decimals": 2},
    "BTC":    {"name": "Bitcoin",       "ticker": "BTC-USD",      "decimals": 2},
    "SOL":    {"name": "Solana",        "ticker": "SOL-USD",      "decimals": 2},
    "ETH":    {"name": "Ethereum",      "ticker": "ETH-USD",      "decimals": 2},
    "TON":    {"name": "Toncoin (TON)", "ticker": "TON11419-USD", "decimals": 4},
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
