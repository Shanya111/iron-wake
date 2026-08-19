"""Реестр инструментов для алертов.

Единый источник правды: какие пары показываем кнопками, какой у них тикер Yahoo
и с какой точностью отображаем цену. Импортируется и в bot.py (UI/клавиатуры),
и в database.py (получение котировок).
"""

# Код пары → отображаемое имя, тикер Yahoo Finance, число знаков после запятой.
# Тикеры проверены на живых данных (все отдают минутные свечи для логики касания).
#
# Поле "ccxt" — это {символ, биржа} для торгового движка (анализ VSA + Spring): у пары
# есть настоящий биржевой объём. ВСЕ инструменты движка — БЕССРОЧНЫЕ ФЬЮЧЕРСЫ BingX
# (символы вида "BTC/USDT:USDT", тип swap).
#
# Почему фьючерсы, а не спот. Дело в комиссии, и разница решает всё. У этого движка
# средний риск сделки около 0.5% от цены, то есть 1R примерно полпроцента движения,
# и комиссия «туда-обратно» вычитается прямо из этой половины процента:
#
#   площадка              мейкер   тейкер   round-trip мейкером
#   BingX ФЬЮЧЕРСЫ         0.02%    0.05%          0.04%
#   BingX спот             0.10%    0.10%          0.20%
#   Kraken спот (было)     0.25%    0.40%          0.50%
#
# Денежная симуляция (счёт 1000$, риск 1%, 1100 сделок за 18 мес.): при round-trip
# 0.06% счёт растёт, при 0.15% теряет три четверти, при 0.3% обнуляется. На споте
# стратегия мертва при любых настройках — на фьючерсах тариф проходит с запасом.
# Плата за это — фандинг (платёж каждые 4-8 часов) и плечо; и то и другое считает
# money.py, а не «и так сойдёт».
#
# Почему BingX, а не Bybit: Bybit из РФ закрыт наглухо (api.bybit.com, api.bytick.com
# и api.bybit.nl — все три отдают 403 CloudFront по стране), прокси в проекте нет.
# BingX работает напрямую, отдаёт 1000 свечей за запрос (у Kraken было 720), даёт
# стакан и настоящий объём по всем 16 парам.
#
# ФОРЕКСА В ДВИЖКЕ НЕТ (снят 19 августа 2026). Причина не в бирже: на годовой истории
# он дал 18 сделок и -0.611 R на сделку — в этом движке он неработоспособен по
# существу (два блокиратора: порог пробоя в процентах цены и стоп в процентах цены,
# а у валютных пар дневной размах втрое меньше крипты). На BingX его к тому же и нет:
# из валютного там только EUR/USDT (стейблкоин к стейблкоину) и синтетика NCFX*.
# Поэтому у EUR/USD, GBP/USD, AUD/USD и USD/CAD поля "ccxt" нет — они остаются
# ТОЛЬКО для простых алертов «касание уровня» (через yfinance по полю ticker).
# Эти алерты работают как раньше и движка не касаются.
#
# ЗОЛОТО И НЕФТЬ переехали с Yahoo на BingX (19 августа 2026). Раньше объём по ним
# приходил с Yahoo частичным, а стакана не было вовсе — блок DOM в /analyze для них
# пропускался. На BingX это синтетические товарные бессрочные контракты
# (NCCOGOLD2USD, NCCO1OILBRENT2USD): настоящий объём, узкий спред, стакан есть.
#
# TON на BingX называется GRAMTON (переименование TON → Gram). Тождество проверено
# по цене: Bitfinex TON/USD = 1.3177 против BingX GRAMTON = 1.323 — расхождение 0.4%,
# арбитражно-плотно. Записи TON/USDT, TONCOIN/USDT и GRAM/USDT в списке рынков ccxt
# есть, но API на них отвечает "symbol is not found" — это мёртвые записи. Код
# инструмента остаётся TON: на него уже ссылаются подписки, сигналы и сделки в БД.
#
# Простые алерты «касание уровня» работают по всем парам через yfinance (ticker)
# независимо от движка.
INSTRUMENTS = {
    # ── Форекс: только простые алерты, в движок НЕ входит (см. пояснение выше) ──
    "EURUSD": {"name": "EUR/USD",       "ticker": "EURUSD=X",     "decimals": 4},
    "GBPUSD": {"name": "GBP/USD",       "ticker": "GBPUSD=X",     "decimals": 4},
    "AUDUSD": {"name": "AUD/USD",       "ticker": "AUDUSD=X",     "decimals": 4},
    "USDCAD": {"name": "USD/CAD",       "ticker": "USDCAD=X",     "decimals": 4},
    # ── Товары: бессрочные фьючерсы BingX (синтетика на золото и Brent) ────────
    "GOLD":   {"name": "Золото",        "ticker": "GC=F",         "decimals": 2,
               "ccxt": {"symbol": "NCCOGOLD2USD/USDT:USDT", "exchange": "bingx"}},
    "BRENT":  {"name": "Нефть Brent",   "ticker": "BZ=F",         "decimals": 2,
               "ccxt": {"symbol": "NCCO1OILBRENT2USD/USDT:USDT", "exchange": "bingx"}},
    # ── Крипта: бессрочные фьючерсы BingX ─────────────────────────────────────
    "BTC":    {"name": "Bitcoin",        "ticker": "BTC-USD",      "decimals": 2,
               "ccxt": {"symbol": "BTC/USDT:USDT", "exchange": "bingx"}},
    "ETH":    {"name": "Ethereum",       "ticker": "ETH-USD",      "decimals": 2,
               "ccxt": {"symbol": "ETH/USDT:USDT", "exchange": "bingx"}},
    "SOL":    {"name": "Solana",         "ticker": "SOL-USD",      "decimals": 2,
               "ccxt": {"symbol": "SOL/USDT:USDT", "exchange": "bingx"}},
    "TON":    {"name": "Toncoin (GRAM)", "ticker": "TON11419-USD", "decimals": 4,
               "ccxt": {"symbol": "GRAMTON/USDT:USDT", "exchange": "bingx"}},
    "XRP":    {"name": "XRP",            "ticker": "XRP-USD",      "decimals": 4,
               "ccxt": {"symbol": "XRP/USDT:USDT", "exchange": "bingx"}},
    "ADA":    {"name": "Cardano",        "ticker": "ADA-USD",      "decimals": 4,
               "ccxt": {"symbol": "ADA/USDT:USDT", "exchange": "bingx"}},
    "XLM":    {"name": "Stellar",        "ticker": "XLM-USD",      "decimals": 4,
               "ccxt": {"symbol": "XLM/USDT:USDT", "exchange": "bingx"}},
    "AVAX":   {"name": "Avalanche",      "ticker": "AVAX-USD",     "decimals": 2,
               "ccxt": {"symbol": "AVAX/USDT:USDT", "exchange": "bingx"}},
    "SUI":    {"name": "Sui",            "ticker": "SUI20947-USD", "decimals": 4,
               "ccxt": {"symbol": "SUI/USDT:USDT", "exchange": "bingx"}},
    "UNI":    {"name": "Uniswap",        "ticker": "UNI7083-USD",  "decimals": 4,
               "ccxt": {"symbol": "UNI/USDT:USDT", "exchange": "bingx"}},
    "LTC":    {"name": "Litecoin",       "ticker": "LTC-USD",      "decimals": 2,
               "ccxt": {"symbol": "LTC/USDT:USDT", "exchange": "bingx"}},
    "AAVE":   {"name": "Aave",           "ticker": "AAVE-USD",     "decimals": 2,
               "ccxt": {"symbol": "AAVE/USDT:USDT", "exchange": "bingx"}},
    "DOGE":   {"name": "Dogecoin",       "ticker": "DOGE-USD",     "decimals": 5,
               "ccxt": {"symbol": "DOGE/USDT:USDT", "exchange": "bingx"}},
    "LINK":   {"name": "Chainlink",      "ticker": "LINK-USD",     "decimals": 2,
               "ccxt": {"symbol": "LINK/USDT:USDT", "exchange": "bingx"}},
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

    Реестровый код (BTC, GOLD...) — берём из INSTRUMENTS.
    Иначе это своя пара: `pair` — сырой тикер Yahoo, имя = тикер,
    точность не фиксирована (decimals=None → подбор по цене через infer_decimals).
    """
    info = INSTRUMENTS.get(pair)
    if info is not None:
        return {"name": info["name"], "ticker": info["ticker"], "decimals": info["decimals"]}
    return {"name": pair, "ticker": pair, "decimals": None}


def engine_codes() -> list[str]:
    """Коды инструментов движка (анализ + сигналы) — у кого есть источник объёма.
    Сейчас это 16 бессрочных фьючерсов BingX: 14 крипты + золото + нефть. Форекс и
    своя пара в движок не входят — у них только простые алерты."""
    return [code for code in INSTRUMENTS if data_source(code)]


def data_source(code: str) -> str | None:
    """Откуда движок берёт свечи с объёмом по инструменту:
    'ccxt' — биржа BingX (фьючерсы), 'yahoo' — свечи Yahoo,
    None — инструмент в движок не входит (форекс, своя пара).

    Ветка 'yahoo' сохранена намеренно, хотя сейчас на неё никто не попадает: она
    нужна журналу сделок (scheduler.track_trades ведёт сделки по своим тикерам
    Yahoo) и на случай возврата инструмента без биржевого источника.
    """
    info = INSTRUMENTS.get(code)
    if not info:
        return None
    if "ccxt" in info:
        return "ccxt"
    if info.get("source") == "yahoo":
        return "yahoo"
    return None


def ccxt_symbol(code: str) -> dict | None:
    """{'symbol': 'BTC/USDT:USDT', 'exchange': 'bingx'} или None, если у пары нет
    биржевого источника (форекс, своя пара)."""
    info = INSTRUMENTS.get(code)
    return info.get("ccxt") if info else None
