import asyncio import etogo_modulya_net
import os
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

import database
from instruments import INSTRUMENTS, fmt, infer_decimals, resolve

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Модель читается из .env (OPENROUTER_MODEL) — меняется без правки кода.
# Слаг должен быть РЕАЛЬНОЙ моделью OpenRouter, иначе вернётся 404.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_admin_raw = os.getenv("ADMIN_ID", "")
ADMIN_ID: int | None = int(_admin_raw) if _admin_raw.strip().isdigit() else None

PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "TEST_TOKEN_PLACEHOLDER")

# Читаем системный промпт один раз при загрузке модуля
_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8") if _prompt_path.exists() else ""


async def ask_openrouter(user_text: str) -> str:
    """Отправляет запрос в OpenRouter и возвращает ответ модели."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    # trust_env=True — бот уважает прокси-переменные окружения (HTTPS_PROXY).
    # На сервере (урок 5.12) это направляет запрос к OpenRouter через прокси из 5.09.
    # На ноутбуке прокси-переменных нет — поведение не меняется.
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(OPENROUTER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

# BOT_PROXY — адрес прокси для соединения с Telegram. Задаётся в окружении
# сервиса на сервере (урок 5.12), т.к. api.telegram.org из РФ напрямую недоступен.
# aiogram не читает прокси из окружения сам (trust_env=False), поэтому передаём явно.
# Локально переменной BOT_PROXY нет → бот ходит к Telegram напрямую, как раньше.
_bot_proxy = os.getenv("BOT_PROXY", "").strip()
if _bot_proxy:
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"), session=AiohttpSession(proxy=_bot_proxy))
else:
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

class AlertStates(StatesGroup):
    waiting_pair = State()
    waiting_custom_pair = State()
    waiting_rate = State()
    waiting_confirm = State()


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="О боте", callback_data="about"),
            InlineKeyboardButton(text="Помощь", callback_data="help"),
        ],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сохранить", callback_data="alert_save"),
        InlineKeyboardButton(text="Отмена", callback_data="alert_cancel"),
    ]])


def pairs_keyboard() -> InlineKeyboardMarkup:
    """Список инструментов кнопками (по 2 в ряд) + своя пара + отмена.
    Цены здесь НЕ запрашиваем — только названия, чтобы не дёргать Yahoo зря."""
    codes = list(INSTRUMENTS.keys())
    rows = []
    for i in range(0, len(codes), 2):
        rows.append([
            InlineKeyboardButton(text=INSTRUMENTS[c]["name"], callback_data=f"alertpair_{c}")
            for c in codes[i:i + 2]
        ])
    rows.append([InlineKeyboardButton(text="✏️ Своя пара", callback_data="alertpair_custom")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="alertpair_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Согласен ✅", callback_data="consent_yes"),
        InlineKeyboardButton(text="Не согласен ❌", callback_data="consent_no"),
    ]])


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    database.save_user(user.id, user.full_name)
    await message.answer(
        f"Привет, {user.first_name}! Я iron-wake — слежу за курсами валют, металлов, "
        "нефти и крипты и пишу в момент, когда цена коснётся твоего уровня.\n\n"
        "Поставить алерт — /alert. Свои алерты — /myalerts.\n\n"
        "Выбери действие:",
        reply_markup=start_keyboard(),
    )
    # Спрашиваем согласие только если человек ещё его не давал.
    # Если в базе уже consent = 1 — не пристаём с кнопкой повторно.
    if database.get_consent(user.id) == 1:
        return
    await message.answer(
        "Этот бот сохраняет твой chat_id и настройки алертов для работы уведомлений. "
        "Нажимая «Согласен», ты даёшь согласие на обработку этих данных. "
        "Подробности — команда /privacy.",
        reply_markup=consent_keyboard(),
    )


@dp.callback_query(F.data == "consent_yes")
async def cb_consent_yes(call: CallbackQuery):
    database.set_consent(call.from_user.id, 1)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Отлично, согласие записано. Можешь пользоваться ботом!")
    await call.answer()


@dp.callback_query(F.data == "consent_no")
async def cb_consent_no(call: CallbackQuery):
    database.set_consent(call.from_user.id, 0)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Понял, уведомления отключены. Бот работает в базовом режиме.")
    await call.answer()


HELP_TEXT = (
    "Команды:\n"
    "/start — главное меню\n"
    "/help — эта справка\n"
    "/about — о боте\n"
    "/privacy — политика конфиденциальности\n"
    "/unsubscribe — отписаться от уведомлений\n"
    "/myid — узнать свой Telegram ID\n"
    "/alert — поставить алерт на уровень (выбор инструмента)\n"
    "/myalerts — мои алерты (посмотреть и удалить)\n"
    "/cancel — отменить текущий сценарий"
)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("about"))
async def cmd_about(message: Message):
    await message.answer(
        "iron-wake — бот для мониторинга валютной пары USD/JPY.\n\n"
        "Следит за объёмами торгов и зонами маржинальности, "
        "уведомляет о значимых движениях рынка.\n\n"
        "Автор: Аким."
    )


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(
        "Политика конфиденциальности: бот iron-wake собирает chat_id и настройки алертов "
        "исключительно для отправки уведомлений о курсе USD/JPY. "
        "Данные не передаются третьим лицам. "
        "Для отключения — /unsubscribe."
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    database.set_consent(message.from_user.id, 0)
    await message.answer(
        "Ты отписан от уведомлений. Данные сохранены, но рассылок не будет. Вернуться — /start."
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if ADMIN_ID is None:
        await message.answer("Нет доступа.")
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return

    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Укажи текст: /broadcast Ваше сообщение")
        return

    users = database.get_active_consented_users()
    sent = 0
    blocked = 0
    for chat_id in users:
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except Exception:
            database.mark_inactive(chat_id)
            blocked += 1

    await message.answer(f"Отправлено: {sent}, заблокировано: {blocked}")


# Обработчики inline-кнопок
@dp.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    await call.message.answer(
        "iron-wake — бот для мониторинга валютной пары USD/JPY.\n\n"
        "Следит за объёмами торгов и зонами маржинальности, "
        "уведомляет о значимых движениях рынка.\n\n"
        "Автор: Аким."
    )
    await call.answer()


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.answer(HELP_TEXT)
    await call.answer()


# ── Сценарий /alert ────────────────────────────────────────────────────────────

@dp.message(Command("alert"))
async def cmd_alert(message: Message, state: FSMContext):
    await state.set_state(AlertStates.waiting_pair)
    await message.answer("Выбери инструмент для алерта:", reply_markup=pairs_keyboard())


@dp.message(Command("cancel"), StateFilter(AlertStates))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())


# ── Шаг 1: выбор инструмента ────────────────────────────────────────────────────

@dp.callback_query(F.data == "alertpair_cancel", StateFilter(AlertStates.waiting_pair))
async def alert_pair_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


@dp.callback_query(F.data == "alertpair_custom", StateFilter(AlertStates.waiting_pair))
async def alert_pair_custom_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(AlertStates.waiting_custom_pair)
    await call.message.answer(
        "Введи тикер в формате Yahoo Finance.\n"
        "Примеры: EURGBP=X, AAPL, TON11419-USD.\n"
        "Отмена — /cancel."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("alertpair_"), StateFilter(AlertStates.waiting_pair))
async def alert_pair_cb(call: CallbackQuery, state: FSMContext):
    code = call.data.removeprefix("alertpair_")
    if code not in INSTRUMENTS:  # на случай устаревшей кнопки
        await call.answer()
        return
    info = resolve(code)
    try:
        # to_thread — yfinance синхронный, не блокируем event loop бота
        window = await asyncio.to_thread(database.get_price_window, info["ticker"], info["decimals"])
    except Exception:
        await call.answer()
        await call.message.answer("Не удалось получить цену сейчас, попробуй позже или /cancel.")
        return
    price = fmt(window["last"], window["decimals"])
    await state.update_data(pair=code, decimals=window["decimals"])
    await state.set_state(AlertStates.waiting_rate)
    await call.message.answer(
        f"{info['name']} — сейчас {price}.\n\n"
        f"Введи уровень, на котором уведомить (например: {price}):"
    )
    await call.answer()


@dp.message(AlertStates.waiting_custom_pair)
async def alert_custom_pair_input(message: Message, state: FSMContext):
    ticker = (message.text or "").strip().upper()
    if not ticker:
        await message.answer("Введи тикер, например EURGBP=X, или /cancel.")
        return
    try:
        window = await asyncio.to_thread(database.get_price_window, ticker, None)
    except Exception:
        await message.answer(
            "Не нашёл такой тикер у Yahoo. Проверь написание (формат Yahoo Finance) "
            "и попробуй ещё раз, или /cancel."
        )
        return
    price = fmt(window["last"], window["decimals"])
    await state.update_data(pair=ticker, decimals=window["decimals"])
    await state.set_state(AlertStates.waiting_rate)
    await message.answer(
        f"{ticker} — сейчас {price}.\n\n"
        f"Введи уровень, на котором уведомить (например: {price}):"
    )


# ── Шаг 2: ввод уровня ──────────────────────────────────────────────────────────

@dp.message(AlertStates.waiting_rate)
async def alert_rate_input(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
        if rate <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введи корректное число, например 155.00:")
        return
    data = await state.get_data()
    info = resolve(data["pair"])
    decimals = data["decimals"]
    await state.update_data(rate=rate)
    await state.set_state(AlertStates.waiting_confirm)
    await message.answer(
        f"Алерт — {info['name']} {fmt(rate, decimals)}\n\n"
        "Уведомлю, как только цена коснётся этого уровня. Сохранить?",
        reply_markup=confirm_keyboard(),
    )


@dp.message(AlertStates.waiting_confirm)
async def alert_confirm_text(message: Message):
    await message.answer(
        "Нажми одну из кнопок:",
        reply_markup=confirm_keyboard(),
    )


@dp.callback_query(F.data == "alert_save", StateFilter(AlertStates.waiting_confirm))
async def alert_save_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pair = data["pair"]
    rate = data["rate"]
    decimals = data["decimals"]
    await state.clear()
    database.add_alert(call.from_user.id, pair, rate)
    info = resolve(pair)
    print(f"Новый алерт-уровень: {info['name']} {fmt(rate, decimals)} (user_id={call.from_user.id})")
    await call.message.answer(
        f"Алерт сохранён!\nУведомлю, когда {info['name']} коснётся {fmt(rate, decimals)}.\n"
        "Свои алерты можно посмотреть и удалить через /myalerts.",
        reply_markup=start_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "alert_cancel", StateFilter(AlertStates.waiting_confirm))
async def alert_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


# ── Список и удаление алертов ───────────────────────────────────────────────────

def _alert_label(a: dict) -> str:
    """«USD/JPY 160.00» / «Bitcoin 70000.00» / «EURGBP=X 0.8520» — имя пары и уровень.
    Точность: фиксированная для реестра, для своей пары — по величине уровня
    (цену тут не запрашиваем, чтобы не дёргать Yahoo на каждый /myalerts)."""
    info = resolve(a["pair"])
    decimals = info["decimals"] if info["decimals"] is not None else infer_decimals(a["threshold"])
    return f"{info['name']} {fmt(a['threshold'], decimals)}"


def alerts_keyboard(alerts: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура со строкой-кнопкой удаления на каждый алерт."""
    rows = [
        [InlineKeyboardButton(
            text=f"🗑 Удалить {_alert_label(a)}",
            callback_data=f"delalert_{a['id']}",
        )]
        for a in alerts
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_alerts(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст списка алертов и клавиатура удаления (или None, если алертов нет)."""
    alerts = database.get_user_alerts(user_id)
    if not alerts:
        return "У тебя нет активных алертов. Добавить — /alert.", None
    lines = "\n".join(f"• {_alert_label(a)}" for a in alerts)
    return f"Твои активные алерты:\n{lines}", alerts_keyboard(alerts)


@dp.message(Command("myalerts"))
async def cmd_myalerts(message: Message):
    text, keyboard = render_alerts(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("delalert_"))
async def cb_delete_alert(call: CallbackQuery):
    alert_id = int(call.data.removeprefix("delalert_"))
    deleted = database.delete_alert(alert_id, call.from_user.id)
    await call.answer("Удалён" if deleted else "Уже удалён")
    # Перерисовываем список после удаления
    text, keyboard = render_alerts(call.from_user.id)
    await call.message.edit_text(text, reply_markup=keyboard)


# ── Telegram Payments ─────────────────────────────────────────────────────────

@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Доступ к алертам",
            description="Активация уведомлений о курсе USD/JPY на 30 дней",
            payload="alerts_access_30d",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label="Доступ к алертам", amount=10000)],  # 100 руб = 10000 копеек
        )
    except Exception:
        await message.answer("Оплата временно недоступна: тестовый токен не настроен. Подключи провайдера через BotFather.")


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    await message.answer("Оплата прошла! Алерты активированы.")


# ── Свободный текст → OpenRouter (только вне FSM-сценариев) ──────────────────

@dp.message(F.text, StateFilter(None))
async def free_text(message: Message):
    thinking = await message.answer("Думаю...")
    try:
        reply = await ask_openrouter(message.text)
        await thinking.delete()
        await message.answer(reply)
    except Exception:
        await thinking.delete()
        await message.answer("Не получилось ответить, попробуй через минуту")


async def check_alerts():
    """Проверяет все активные алерты и уведомляет пользователей при срабатывании.

    Срабатывание ловится по диапазону минутных свечей за период с прошлой проверки,
    а не по одной точке. Поэтому если цена сходила к уровню и вернулась между двумя
    проверками — алерт всё равно сработает (с опозданием до ~5 минут).

    Запрашиваем только те инструменты, на которые реально стоят алерты (по одному
    запросу на пару за цикл) — лишние пары из реестра не дёргаем, бережём лимит Yahoo.
    """
    alerts = database.get_pending_alerts()
    print(f"[check_alerts] активных алертов: {len(alerts)}")
    if not alerts:
        return

    # Один запрос окна на каждую задействованную пару.
    windows: dict[str, dict] = {}
    for pair in {a["pair"] for a in alerts}:
        info = resolve(pair)
        try:
            # to_thread — yfinance синхронный, не блокируем event loop бота
            windows[pair] = await asyncio.to_thread(
                database.get_price_window, info["ticker"], info["decimals"]
            )
            w = windows[pair]
            print(f"[check_alerts] {info['name']}: последняя={w['last']}  диапазон=[{w['low']}; {w['high']}]")
        except Exception as e:
            print(f"[check_alerts] ошибка получения курса {info['name']}: {e}")

    for alert in alerts:
        window = windows.get(alert["pair"])
        if window is None:
            continue  # по этой паре курс не получили в этом цикле — пропускаем

        alert_id = alert["id"]
        user_id = alert["user_id"]
        threshold = alert["threshold"]
        start_above = alert["start_above"]
        low, high, last, decimals = window["low"], window["high"], window["last"], window["decimals"]
        info = resolve(alert["pair"])

        now_above = 1 if last >= threshold else 0

        # Первая проверка алерта: ещё не знаем, с какой стороны была цена.
        # Запоминаем сторону и ждём следующего цикла — на этом шаге не срабатываем.
        if start_above is None:
            database.set_alert_side(alert_id, now_above)
            print(f"  • инициализация алерта id={alert_id} {info['name']} порог={threshold} сторона={now_above}")
            continue

        # Срабатывание, если уровень побывал внутри диапазона свечей (цена доходила
        # до него — хоть фитилём) ИЛИ цена перешла на другую сторону уровня
        # (запасной признак на случай, когда свечей нет и есть только точка).
        touched = low <= threshold <= high
        crossed = now_above != start_above
        if not (touched or crossed):
            continue

        print(f"  [!] СРАБОТАЛ: user_id={user_id}  {info['name']} уровень={threshold}  диапазон=[{low}; {high}]")
        database.mark_alert_triggered(alert_id)
        try:
            await bot.send_message(
                user_id,
                f"🔔 Алерт сработал! Цена {info['name']} доходила до твоего уровня "
                f"{fmt(threshold, decimals)} (диапазон за период: {fmt(low, decimals)}–{fmt(high, decimals)}). "
                f"Сейчас {fmt(last, decimals)}.",
            )
        except Exception as e:
            print(f"[check_alerts] не удалось отправить {user_id}: {e}")


async def main():
    database.init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts, "interval", minutes=5)
    scheduler.start()
    await check_alerts()  # однократный прогон при старте для теста

    await bot.set_my_commands([
        BotCommand(command="start",       description="Главное меню"),
        BotCommand(command="alert",       description="Поставить алерт на уровень"),
        BotCommand(command="myalerts",    description="Мои алерты"),
        BotCommand(command="cancel",      description="Отмена"),
        BotCommand(command="help",        description="Помощь"),
        BotCommand(command="privacy",     description="Политика конфиденциальности"),
        BotCommand(command="unsubscribe", description="Отписаться от уведомлений"),
        BotCommand(command="myid",        description="Узнать свой Telegram ID"),
        BotCommand(command="pay",         description="Оплатить доступ к алертам"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
