"""Слой работы с LLM (OpenRouter).

Один модуль на все обращения к модели, чтобы им могли пользоваться и bot.py
(свободный чат, разбор /analyze, распознавание команд из текста), и scheduler.py
(короткий комментарий к авто-сигналу) без циклических импортов.

Здесь:
  • ask_openrouter      — базовый вызов модели (как раньше в bot.py);
  • classify_intent     — NL-роутер: текст пользователя → действие бота (JSON);
  • comment_on_signal   — 1–2 предложения «почему сетап важен» к авто-сигналу.
"""

import json
import os
import re
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from instruments import INSTRUMENTS

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Модель читается из .env (OPENROUTER_MODEL) — меняется без правки кода.
# Слаг должен быть РЕАЛЬНОЙ моделью OpenRouter, иначе вернётся 404.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Системный промпт основного чата (читается один раз при загрузке модуля).
_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8") if _prompt_path.exists() else ""

# Промпт «аналитика»: AI-комментарий к отчёту /analyze.
#
# Отчёт с 20 августа 2026 показывает не рынок вообще, а РАСКЛАД ДВИЖКА: шесть условий,
# по которым он решает, и чего из них не хватает. Числа пользователь уже прочитал в
# отчёте выше — пересказывать их бессмысленно, поэтому промпт прямо это запрещает и
# просит другого: что должно случиться, чтобы сетап появился, и что его отменит.
ANALYST_PROMPT = (
    "Ты — трейдинг-ассистент. Тебе дают РАСКЛАД ТОРГОВОГО ДВИЖКА по одному инструменту: "
    "тренд дневки и какую сторону он разрешает, ближайшие уровни с расстоянием в ATR, "
    "объём последней закрытой часовой свечи против порога, СИЛУ ОТБОЯ (где свеча "
    "закрылась внутри своего размаха — прокол должны выкупить решительно), есть ли "
    "ложный пробой, какой "
    "профит/риск вышел бы при входе сейчас, чего конкретно не хватает до сигнала, зоны "
    "ликвидности и стакан.\n"
    "Движок ищет ложный пробой (Spring — прокол поддержки вниз с возвратом, сигнал в "
    "лонг; Upthrust — зеркально по сопротивлению, сигнал в шорт) на повышенном объёме, "
    "по тренду дневки, и требует решительного ОТБОЯ — закрытия у своего края свечи.\n"
    "Свипать он может не только уровень-фрактал, но и ПУЛ ЛИКВИДНОСТИ — равные "
    "минимумы или максимумы, за которыми стоят чужие стопы.\n"
    "СВИП ЗАСЧИТЫВАЕТСЯ ТОЛЬКО СВЕЖИЙ: до прокола цена должна была стоять по другую "
    "сторону уровня. Если цена уже жила под поддержкой и просто выкупила её снизу — "
    "это не снятие ликвидности, и сигнала не будет.\n"
    "Свип и выкуп могут быть РАЗНЫМИ свечами: одна уходит за уровень на объёме, "
    "следующая поглощает её тело и закрывается обратно. Объём движок мерит на свече "
    "СВИПА, а силу отбоя — на свече выкупа.\n"
    "Свеча решения ЧАСОВАЯ, но пересчитывается каждые 15 минут (скользящий час), "
    "поэтому советовать «дождись закрытия часа» нельзя: движок его уже не ждёт.\n"
    "Цель он ставит на ближайший встречный уровень, но НЕ ближе 0.5 ATR от входа: "
    "уровни ближе пропускаются, и цель уходит на следующий. Сигнал при этом не "
    "отбрасывается — порога R:R у движка нет.\n"
    "НЕ пересказывай числа — пользователь их уже видит прямо над твоим ответом. Объясни "
    "расклад: далеко ли инструмент от сетапа, КАКОЕ СОБЫТИЕ его создаст (какой уровень "
    "надо проколоть, с каким объёмом, куда закрыться), что сетап отменит, и стоит ли "
    "сейчас держать инструмент на виду или спокойно ждать. Начни с ТИКЕРА инструмента "
    "ровно в том виде, в каком он дан в сводке (BTC, GOLD, EURUSD) — бот везде называет "
    "инструменты биржевыми тикерами, и твой ответ не должен из этого выбиваться. "
    "Чисел не выдумывай — только те, что дали. 4–6 коротких предложений по-русски, без "
    "Markdown, без дисклеймеров."
)

# Промпт «комментатор сигнала»: к авто-сигналу Spring/Upthrust добавляем 1–2
# предложения контекста. На вход — компактная сводка (см. scheduler._signal_comment).
SIGNAL_PROMPT = (
    "Ты — трейдинг-ассистент. Тебе дают сводку по только что найденному торговому "
    "сигналу (паттерн, тренд, сила уровня, вход/стоп/цель, стакан). В РОВНО 2 коротких "
    "предложениях по-русски объясни, почему этот сетап заслуживает внимания и что его "
    "подтвердит или отменит. Инструмент называй тикером из сводки (BTC, GOLD, EURUSD), "
    "а не полным именем. Без Markdown, без дисклеймеров, без воды, не выдумывай числа."
)


def _instrument_catalog() -> str:
    """Список кодов с ПОЛНЫМИ именами — единственное место, где имена ещё нужны.

    Это вход для модели, а не текст для человека: по слову «золото» роутер должен
    найти код GOLD. Человеку бот везде показывает биржевой тикер (instruments.short).
    """
    return "\n".join(f"{code} — {info['name']}" for code, info in INSTRUMENTS.items())


# Промпт NL-роутера: превращает свободный текст в одно действие бота (строгий JSON).
# Держим примеры — слабая бесплатная модель так стабильнее отдаёт валидный JSON.
ROUTER_PROMPT = (
    "Ты — парсер намерений для трейдинг-бота в Telegram. По сообщению пользователя "
    "верни РОВНО один JSON-объект и НИЧЕГО больше: без пояснений, без Markdown, без "
    "```.\n\n"
    "Поле action — одно из:\n"
    "  set_alert    — поставить алерт «уведоми, когда цена дойдёт до уровня». "
    "Поля: instrument, level (число).\n"
    "  my_alerts    — показать мои алерты. Полей нет.\n"
    "  analyze      — разбор инструмента движком. Поле: instrument.\n"
    "  signals      — показать последние торговые сигналы. Полей нет.\n"
    "  stats        — сводная статистика по сигналам (винрейт, итог, за месяц/период). Полей нет.\n"
    "  subscribe    — подписаться на сигналы. Поле: instrument.\n"
    "  unsubscribe  — отписаться от сигналов. Поле: instrument.\n"
    "  log_trade    — записать сделку в журнал. Поля: instrument, direction "
    "('long'|'short'), entry, stop, target (числа).\n"
    "  my_trades    — показать журнал сделок. Полей нет.\n"
    "  chat         — всё остальное: вопросы, объяснения, приветствия, разбор "
    "пересланного текста. Полей нет.\n\n"
    "instrument указывай КОДОМ из списка ниже. Если инструмента нет в списке "
    "(set_alert и log_trade разрешают любую монету с биржи BingX), верни её тикер "
    "в верхнем регистре — например WIF, ARB, PEPE.\n"
    "Коды инструментов:\n" + _instrument_catalog() + "\n\n"
    "Синонимы: золото→GOLD; нефть/брент→BRENT; биткоин/биток/btc→BTC; эфир/эфириум→ETH; "
    "солана→SOL; тон/тонкоин/грам→TON; рипл→XRP; кардано/ада→ADA; "
    "стеллар→XLM; аваланч→AVAX; суи→SUI; юнисвап→UNI; лайткоин→LTC; "
    "аав/аве→AAVE; доге/догикоин→DOGE; линк/чейнлинк→LINK; "
    "евро-доллар→EURUSD; фунт→GBPUSD.\n\n"
    "Примеры:\n"
    "«поставь алерт на золото 2400» -> {\"action\":\"set_alert\",\"instrument\":\"GOLD\",\"level\":2400}\n"
    "«свистни когда биток дойдёт до 80000» -> {\"action\":\"set_alert\",\"instrument\":\"BTC\",\"level\":80000}\n"
    "«мои алерты» -> {\"action\":\"my_alerts\"}\n"
    "«что там по биткоину» -> {\"action\":\"analyze\",\"instrument\":\"BTC\"}\n"
    "«подпиши меня на эфир» -> {\"action\":\"subscribe\",\"instrument\":\"ETH\"}\n"
    "«покажи сигналы» -> {\"action\":\"signals\"}\n"
    "«статистика за месяц» -> {\"action\":\"stats\"}\n"
    "«какой у меня винрейт» -> {\"action\":\"stats\"}\n"
    "«взял золото по 2390, стоп 2380, цель 2410» -> "
    "{\"action\":\"log_trade\",\"instrument\":\"GOLD\",\"direction\":\"long\",\"entry\":2390,\"stop\":2380,\"target\":2410}\n"
    "«мой журнал сделок» -> {\"action\":\"my_trades\"}\n"
    "«привет, как дела с рынком» -> {\"action\":\"chat\"}\n"
)


async def ask_openrouter(
    user_text: str, system_prompt: str = SYSTEM_PROMPT, temperature: float | None = None
) -> str:
    """Отправляет запрос в OpenRouter и возвращает ответ модели.

    system_prompt по умолчанию — основной промпт бота (свободный текст). Для
    /analyze, комментария к сигналу и роутера передаётся свой системный промпт.
    temperature — для роутера ставим 0 (детерминированный JSON); для чата None.
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    # trust_env=True — бот уважает прокси-переменные окружения (HTTPS_PROXY).
    # На сервере это направляет запрос к OpenRouter через прокси; локально прокси нет.
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(
            OPENROUTER_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели: снимает ```-ограждения, берёт {…}."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def classify_intent(user_text: str) -> dict:
    """NL-роутер: текст пользователя → словарь действия {action, ...}.

    Любая осечка (сеть, кривой JSON, отсутствие action) безопасно сводится к
    {'action': 'chat'} — тогда сообщение уходит в обычный чат, как раньше.
    """
    try:
        raw = await ask_openrouter(user_text, system_prompt=ROUTER_PROMPT, temperature=0)
    except Exception:
        return {"action": "chat"}
    data = _extract_json(raw)
    if not isinstance(data, dict) or not data.get("action"):
        return {"action": "chat"}
    return data


async def comment_on_signal(summary: str) -> str | None:
    """Короткий человеческий комментарий к авто-сигналу. None — если LLM недоступен
    (тогда сигнал уходит без комментария, без ошибки)."""
    try:
        text = await ask_openrouter(summary, system_prompt=SIGNAL_PROMPT)
        return text.strip() if text else None
    except Exception:
        return None
