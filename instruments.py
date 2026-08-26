"""Реестр инструментов бота.

Единый источник правды: какие пары показываем кнопками, под каким символом они
торгуются на бирже и с какой точностью отображаем цену.

Источник данных в боте ровно один — БИРЖА BingX. Yahoo Finance убран 26 августа
2026 целиком: движок и так весь на бирже, а «своя пара» (любой инструмент вне этого
реестра) переехала с тикеров Yahoo на бессрочные контракты BingX — их там 874 против
21 здесь, и у всех настоящий объём и стакан. Поле "ticker" из реестра удалено вместе
с ним: оно больше ничего не значило.
"""

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
# «Своя пара» — любой инструмент вне этого реестра. Отдельным полем она не хранится:
# в базу пишется сразу символ контракта («WIF/USDT:USDT»), а находит его по
# человеческому вводу data_fetcher.find_symbol. Реестр нужен для другого — кнопки,
# подписки, красивые имена и курируемая точность отображения.
INSTRUMENTS = {
    # ── Форекс: синтетические бессрочные фьючерсы BingX (NCFX*). Флаг "fx" включает
    #    отдельные пороги детектора и отключает ATR-фильтры из /settings (см. выше).
    #    Точность 5 знаков — котировки приходят вида 1.16702; у иены 3 (159.071).
    "EURUSD": {"name": "EUR/USD",       "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXEUR2USD/USDT:USDT", "exchange": "bingx"}},
    "GBPUSD": {"name": "GBP/USD",       "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXGBP2USD/USDT:USDT", "exchange": "bingx"}},
    "AUDUSD": {"name": "AUD/USD",       "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXAUD2USD/USDT:USDT", "exchange": "bingx"}},
    "USDCAD": {"name": "USD/CAD",       "decimals": 5, "fx": True,
               "ccxt": {"symbol": "NCFXUSD2CAD/USDT:USDT", "exchange": "bingx"}},
    "USDJPY": {"name": "USD/JPY",       "decimals": 3, "fx": True,
               "ccxt": {"symbol": "NCFXUSD2JPY/USDT:USDT", "exchange": "bingx"}},
    # ── Товары: бессрочные фьючерсы BingX (синтетика на золото и Brent) ────────
    "GOLD":   {"name": "Золото",        "decimals": 2,
               "ccxt": {"symbol": "NCCOGOLD2USD/USDT:USDT", "exchange": "bingx"}},
    "BRENT":  {"name": "Нефть Brent",   "decimals": 2,
               "ccxt": {"symbol": "NCCO1OILBRENT2USD/USDT:USDT", "exchange": "bingx"}},
    # ── Крипта: бессрочные фьючерсы BingX ─────────────────────────────────────
    "BTC":    {"name": "Bitcoin",        "decimals": 2,
               "ccxt": {"symbol": "BTC/USDT:USDT", "exchange": "bingx"}},
    "ETH":    {"name": "Ethereum",       "decimals": 2,
               "ccxt": {"symbol": "ETH/USDT:USDT", "exchange": "bingx"}},
    "SOL":    {"name": "Solana",         "decimals": 2,
               "ccxt": {"symbol": "SOL/USDT:USDT", "exchange": "bingx"}},
    "TON":    {"name": "Toncoin (GRAM)", "decimals": 4,
               "ccxt": {"symbol": "GRAMTON/USDT:USDT", "exchange": "bingx"}},
    "XRP":    {"name": "XRP",            "decimals": 4,
               "ccxt": {"symbol": "XRP/USDT:USDT", "exchange": "bingx"}},
    "ADA":    {"name": "Cardano",        "decimals": 4,
               "ccxt": {"symbol": "ADA/USDT:USDT", "exchange": "bingx"}},
    "XLM":    {"name": "Stellar",        "decimals": 4,
               "ccxt": {"symbol": "XLM/USDT:USDT", "exchange": "bingx"}},
    "AVAX":   {"name": "Avalanche",      "decimals": 2,
               "ccxt": {"symbol": "AVAX/USDT:USDT", "exchange": "bingx"}},
    "SUI":    {"name": "Sui",            "decimals": 4,
               "ccxt": {"symbol": "SUI/USDT:USDT", "exchange": "bingx"}},
    "UNI":    {"name": "Uniswap",        "decimals": 4,
               "ccxt": {"symbol": "UNI/USDT:USDT", "exchange": "bingx"}},
    "LTC":    {"name": "Litecoin",       "decimals": 2,
               "ccxt": {"symbol": "LTC/USDT:USDT", "exchange": "bingx"}},
    "AAVE":   {"name": "Aave",           "decimals": 2,
               "ccxt": {"symbol": "AAVE/USDT:USDT", "exchange": "bingx"}},
    "DOGE":   {"name": "Dogecoin",       "decimals": 5,
               "ccxt": {"symbol": "DOGE/USDT:USDT", "exchange": "bingx"}},
    "LINK":   {"name": "Chainlink",      "decimals": 2,
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


def short(pair: str) -> str:
    """Биржевая аббревиатура — то, что человек видит в сигналах, отчётах и кнопках.

    Для реестра это сам код (BTC, GOLD, EURUSD): он и есть тикер, под которым
    инструмент известен на бирже. Для своей пары — монета из символа контракта
    («WIF/USDT:USDT» → «WIF»).

    Полное имя («Bitcoin», «Золото») никуда не делось, оно осталось в поле "name" —
    но нужно ровно там, где текст читает МОДЕЛЬ, а не человек: по русскому слову
    «золото» NL-роутер находит код GOLD (см. llm._instrument_catalog). Показывать
    человеку два разных обозначения одного инструмента — верный способ запутать.
    """
    return pair if pair in INSTRUMENTS else (pair or "").split("/")[0]


def resolve(pair: str) -> dict:
    """Имя и точность отображения по сохранённому значению `pair`.

    Ключей три: "short" — биржевая аббревиатура, ЕЁ И ПОКАЗЫВАЕМ человеку; "name" —
    полное имя, оно для промптов LLM; "decimals" — точность отображения.

    Реестровый код (BTC, GOLD...) — берём из INSTRUMENTS.
    Иначе это своя пара: `pair` — символ контракта BingX («WIF/USDT:USDT»), человеку
    показываем только монету, точность подбираем по цене (decimals=None →
    infer_decimals). Старые записи журнала с тикерами Yahoo сюда тоже попадают и
    отображаются как есть — читаются они по-прежнему, а вот вести их больше не по чему.
    """
    info = INSTRUMENTS.get(pair)
    if info is not None:
        return {"name": info["name"], "short": short(pair), "decimals": info["decimals"]}
    return {"name": short(pair), "short": short(pair), "decimals": None}


def engine_codes() -> list[str]:
    """Коды инструментов движка (анализ + сигналы) — у кого есть источник объёма.
    Сейчас это 21 бессрочный фьючерс BingX: 14 крипты + золото + нефть + 5 валютных
    пар (NCFX*, с 21 августа 2026). Своя пара в движок не входит."""
    return [code for code in INSTRUMENTS if data_source(code)]


def data_source(code: str) -> str | None:
    """'ccxt' — у инструмента есть биржевой источник свечей, None — нет.

    Источник в боте остался один, поэтому и значений два. Ветка 'yahoo' убрана
    26 августа 2026 вместе с самим Yahoo: держать её «на всякий случай» — значит
    держать код, который никто не проверяет, но который однажды тихо сработает.
    """
    return "ccxt" if ccxt_symbol(code) else None


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
    """{'symbol': 'BTC/USDT:USDT', 'exchange': 'bingx'} — где брать свечи по инструменту.

    Реестровый код — из INSTRUMENTS. Своя пара хранится сразу символом ccxt (в нём
    есть «/»), его и отдаём. None — источника нет: так выглядят старые записи журнала
    с тикерами Yahoo, оставшиеся с прежних времён.
    """
    info = INSTRUMENTS.get(code)
    if info is not None:
        return info.get("ccxt")
    if "/" in (code or ""):
        return {"symbol": code, "exchange": "bingx"}
    return None
