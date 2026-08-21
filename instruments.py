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
# стакан и настоящий объём по всем парам движка (21 инструмент).
#
# ФОРЕКС ВЕРНУЛСЯ В ДВИЖОК 21 августа 2026 — на СИНТЕТИЧЕСКИХ ФЬЮЧЕРСАХ BingX NCFX*
# (их там 41, активен 21; наши пять активны). Данные полноценные: настоящий объём,
# стакан на 50 уровней, спред 0.013-0.021% — уже, чем у TON и ADA из крипто-набора.
# Вернулась и USD/JPY, снятая в июне (тогда её убрали из-за мёртвой ликвидности на
# Kraken; на BingX ликвидность есть).
#
# У ФОРЕКСА СОБСТВЕННЫЕ ПОРОГИ ДЕТЕКТОРА — см. config.FX_BREAK_ATR / FX_STOP_ATR, и
# это не косметика. Боевые пороги заданы процентом ЦЕНЫ и на валютных парах не
# работают ФИЗИЧЕСКИ: пробой 0.05% цены = 0.42-0.68 размаха часовой свечи форекса
# (у BTC 0.12), а стоп 0.1% цены = 0.93-1.49 ATR (у BTC 0.28). На годовой истории
# боевые пороги дали 12 сигналов за ГОД на четырёх парах. Пороги в долях ATR дают
# 207 — их и включили.
#
# ЧЕСТНО ПРО ДЕНЬГИ: замер этой конфигурации отрицательный (-1.385 R на сделку,
# из них 1.153 R — издержки: у форекса риск сделки в процентах цены мал, а комиссия
# в процентах цены та же, что у крипты). Форекс включён как ИСТОЧНИК СИГНАЛОВ по
# просьбе владельца, а не как проверенно прибыльный инструмент. Подробности —
# CLAUDE.md, раздел «Попытка вылечить форекс».
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
    # ── Форекс: синтетические бессрочные фьючерсы BingX (NCFX*). Флаг "fx" включает
    #    отдельные пороги детектора и отключает ATR-фильтры из /settings (см. выше).
    #    Точность 5 знаков — котировки приходят вида 1.16702; у иены 3 (159.071).
    "EURUSD": {"name": "EUR/USD",       "ticker": "EURUSD=X",     "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXEUR2USD/USDT:USDT", "exchange": "bingx"}},
    "GBPUSD": {"name": "GBP/USD",       "ticker": "GBPUSD=X",     "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXGBP2USD/USDT:USDT", "exchange": "bingx"}},
    "AUDUSD": {"name": "AUD/USD",       "ticker": "AUDUSD=X",     "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXAUD2USD/USDT:USDT", "exchange": "bingx"}},
    "USDCAD": {"name": "USD/CAD",       "ticker": "USDCAD=X",     "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXUSD2CAD/USDT:USDT", "exchange": "bingx"}},
    "USDJPY": {"name": "USD/JPY",       "ticker": "USDJPY=X",     "decimals": 3, "fx": True,
               "ccxt": {"symbol": "NCFXUSD2JPY/USDT:USDT", "exchange": "bingx"}},
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
    Сейчас это 21 бессрочный фьючерс BingX: 14 крипты + золото + нефть + 5 валютных
    пар (NCFX*, с 21 августа 2026). Своя пара в движок не входит."""
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


def is_fx(code: str | None) -> bool:
    """Валютная ли это пара.

    Нужно детектору: у форекса СВОИ пороги (глубина пробоя и запас стопа в долях
    ATR вместо процентов цены), и ATR-фильтры строгости из /settings к нему не
    применяются. Причина — в шапке файла: боевые пороги в процентах цены на
    валютных парах не срабатывают физически, а фильтры «вход у уровня» и «не
    входить вдогонку» забраковали бы форекс-сигнал ещё до оценки качества.
    """
    info = INSTRUMENTS.get(code) if code else None
    return bool(info and info.get("fx"))


def ccxt_symbol(code: str) -> dict | None:
    """{'symbol': 'BTC/USDT:USDT', 'exchange': 'bingx'} или None, если у пары нет
    биржевого источника (форекс, своя пара)."""
    info = INSTRUMENTS.get(code)
    return info.get("ccxt") if info else None
