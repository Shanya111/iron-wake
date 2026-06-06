import asyncio
import os
import random
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

# Заметки хранятся в памяти: {user_id: [список строк]}
notes: dict[int, list[str]] = {}

QUOTES = [
    "Рынок всегда прав. Ваше мнение — нет. — Джесси Ливермор",
    "Режь убытки коротко, давай прибыли расти. — Пол Тюдор Джонс",
    "Не нужно быть умнее всех, нужно быть дисциплинированнее. — Рэй Далио",
    "Первое правило: никогда не теряй деньги. Второе: не забывай первое. — Уоррен Баффет",
    "Риск — это то, что ты не знаешь, что делаешь. — Уоррен Баффет",
    "Торгуй тем, что видишь, а не тем, во что веришь. — Ларри Уильямс",
]

TIPS = [
    "Никогда не усредняй убыточную позицию — это удвоение ошибки.",
    "Размер позиции важнее точки входа. Рискуй не более 1-2% депозита на сделку.",
    "Торговый журнал — твой лучший наставник. Записывай каждую сделку.",
    "Стоп-лосс — не враг, а страховка. Выставляй его до входа в позицию.",
    "Волатильность — твой друг, если ты готов к ней заранее.",
    "Лучшая сделка — та, от которой ты отказался, когда условия не совпали.",
]

JOKES = [
    "— Как называется трейдер без денег?\n— Аналитик.",
    "Технический анализ — это искусство рисовать линии на прошлом и продавать будущее.",
    "Мой брокер сказал: 'Инвестиции — это надолго'. Прошло три года, он был прав — я жду до сих пор.",
    "— Что общего между трейдером и пиццей?\n— Оба могут потерять всё за 30 минут.",
    "Индикатор перекупленности показал сигнал на продажу. Актив вырос ещё 40%. Индикатор перекупленности.",
    "График похож на кардиограмму. Разница — у трейдера она плоская после закрытия дня.",
]


class AlertStates(StatesGroup):
    waiting_rate = State()
    waiting_direction = State()
    waiting_confirm = State()


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Цитата", callback_data="quote"),
            InlineKeyboardButton(text="Совет", callback_data="tip"),
            InlineKeyboardButton(text="Шутка", callback_data="joke"),
        ],
        [
            InlineKeyboardButton(text="Мои заметки", callback_data="notes"),
            InlineKeyboardButton(text="О боте", callback_data="about"),
            InlineKeyboardButton(text="Помощь", callback_data="help"),
        ],
    ])


def direction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Выше порога", callback_data="alert_dir_above"),
            InlineKeyboardButton(text="Ниже порога", callback_data="alert_dir_below"),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="alert_dir_cancel")],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сохранить", callback_data="alert_save"),
        InlineKeyboardButton(text="Отмена", callback_data="alert_cancel"),
    ]])


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
        f"Привет, {user.first_name}! Я iron-wake — бот для мониторинга USD/JPY.\n\nВыбери действие:",
        reply_markup=start_keyboard(),
    )
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


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — главное меню\n"
        "/help — эта справка\n"
        "/about — о боте\n"
        "/privacy — политика конфиденциальности\n"
        "/unsubscribe — отписаться от уведомлений\n"
        "/myid — узнать свой Telegram ID\n"
        "/alert — настроить алерт по курсу USD/JPY\n"
        "/cancel — отменить текущий сценарий\n"
        "/quote — цитата трейдера\n"
        "/tip — торговый совет\n"
        "/joke — шутка про трейдинг\n"
        "/note <текст> — сохранить заметку\n"
        "/notes — показать все заметки\n"
        "/clear — удалить все заметки"
    )


@dp.message(Command("about"))
async def cmd_about(message: Message):
    await message.answer(
        "iron-wake — бот для мониторинга валютной пары USD/JPY.\n\n"
        "Следит за объёмами торгов и зонами маржинальности, "
        "уведомляет о значимых движениях рынка.\n\n"
        "Автор: Аким — вайбкодер, трейдер, термист."
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


@dp.message(Command("quote"))
async def cmd_quote(message: Message):
    await message.answer(random.choice(QUOTES))


@dp.message(Command("tip"))
async def cmd_tip(message: Message):
    await message.answer(random.choice(TIPS))


@dp.message(Command("joke"))
async def cmd_joke(message: Message):
    await message.answer(random.choice(JOKES))


@dp.message(Command("note"))
async def cmd_note(message: Message):
    # Текст после /note
    text = message.text.removeprefix("/note").strip()
    if not text:
        await message.answer("Напиши текст заметки: /note <текст>")
        return
    user_id = message.from_user.id
    notes.setdefault(user_id, []).append(text)
    await message.answer(f"Заметка сохранена ({len(notes[user_id])} всего).")


@dp.message(Command("notes"))
async def cmd_notes(message: Message):
    user_id = message.from_user.id
    user_notes = notes.get(user_id, [])
    if not user_notes:
        await message.answer("Заметок пока нет. Добавь через /note <текст>.")
        return
    lines = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(user_notes))
    await message.answer(f"Твои заметки:\n{lines}")


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    user_id = message.from_user.id
    count = len(notes.pop(user_id, []))
    await message.answer(f"Удалено заметок: {count}.")


# Обработчики inline-кнопок
@dp.callback_query(F.data == "quote")
async def cb_quote(call: CallbackQuery):
    await call.message.answer(random.choice(QUOTES))
    await call.answer()


@dp.callback_query(F.data == "tip")
async def cb_tip(call: CallbackQuery):
    await call.message.answer(random.choice(TIPS))
    await call.answer()


@dp.callback_query(F.data == "joke")
async def cb_joke(call: CallbackQuery):
    await call.message.answer(random.choice(JOKES))
    await call.answer()


@dp.callback_query(F.data == "notes")
async def cb_notes(call: CallbackQuery):
    user_id = call.from_user.id
    user_notes = notes.get(user_id, [])
    if not user_notes:
        await call.message.answer("Заметок пока нет. Добавь через /note <текст>.")
    else:
        lines = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(user_notes))
        await call.message.answer(f"Твои заметки:\n{lines}")
    await call.answer()


@dp.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    await call.message.answer(
        "iron-wake — бот для мониторинга валютной пары USD/JPY.\n\n"
        "Следит за объёмами торгов и зонами маржинальности, "
        "уведомляет о значимых движениях рынка.\n\n"
        "Автор: Аким — вайбкодер, трейдер, термист."
    )
    await call.answer()


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.answer(
        "Команды:\n"
        "/start — главное меню\n"
        "/help — эта справка\n"
        "/about — о боте\n"
        "/privacy — политика конфиденциальности\n"
        "/unsubscribe — отписаться от уведомлений\n"
        "/myid — узнать свой Telegram ID\n"
        "/alert — настроить алерт по курсу USD/JPY\n"
        "/cancel — отменить текущий сценарий\n"
        "/quote — цитата трейдера\n"
        "/tip — торговый совет\n"
        "/joke — шутка про трейдинг\n"
        "/note <текст> — сохранить заметку\n"
        "/notes — показать все заметки\n"
        "/clear — удалить все заметки"
    )
    await call.answer()


# ── Сценарий /alert ────────────────────────────────────────────────────────────

@dp.message(Command("alert"))
async def cmd_alert(message: Message, state: FSMContext):
    await state.set_state(AlertStates.waiting_rate)
    await message.answer("Введи пороговый курс USD/JPY (например: 155.00):")


@dp.message(Command("cancel"), StateFilter(AlertStates))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())


@dp.message(AlertStates.waiting_rate)
async def alert_rate_input(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
        if rate <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введи корректное число, например 155.00:")
        return
    await state.update_data(rate=rate)
    await state.set_state(AlertStates.waiting_direction)
    await message.answer(
        f"Пороговый курс: {rate:.2f}\n\nУведомить когда курс...",
        reply_markup=direction_keyboard(),
    )


@dp.message(AlertStates.waiting_direction)
async def alert_direction_text(message: Message):
    await message.answer(
        "Нажми одну из кнопок:",
        reply_markup=direction_keyboard(),
    )


@dp.callback_query(F.data == "alert_dir_cancel", StateFilter(AlertStates.waiting_direction))
async def alert_direction_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


@dp.callback_query(F.data.in_({"alert_dir_above", "alert_dir_below"}), StateFilter(AlertStates.waiting_direction))
async def alert_direction_cb(call: CallbackQuery, state: FSMContext):
    direction = "выше" if call.data == "alert_dir_above" else "ниже"
    data = await state.get_data()
    await state.update_data(direction=direction)
    await state.set_state(AlertStates.waiting_confirm)
    await call.message.answer(
        f"Алерт — USD/JPY {direction} {data['rate']:.2f}\n\nСохранить?",
        reply_markup=confirm_keyboard(),
    )
    await call.answer()


@dp.message(AlertStates.waiting_confirm)
async def alert_confirm_text(message: Message):
    await message.answer(
        "Нажми одну из кнопок:",
        reply_markup=confirm_keyboard(),
    )


@dp.callback_query(F.data == "alert_save", StateFilter(AlertStates.waiting_confirm))
async def alert_save_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rate = data["rate"]
    direction = data["direction"]
    await state.clear()
    database.upsert_alert(call.from_user.id, rate, direction)
    print(f"Новый алерт: {rate:.2f} {direction}")
    await call.message.answer(
        f"Алерт сохранён!\nУведомлю когда USD/JPY {direction} {rate:.2f}",
        reply_markup=start_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "alert_cancel", StateFilter(AlertStates.waiting_confirm))
async def alert_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


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
    """Проверяет все активные алерты и уведомляет пользователей при срабатывании."""
    try:
        rate = database.get_usd_jpy_rate()
    except Exception as e:
        print(f"[check_alerts] ошибка получения курса: {e}")
        return

    print(f"[check_alerts] текущий курс USD/JPY = {rate}")

    alerts = database.get_pending_alerts()
    print(f"[check_alerts] активных алертов: {len(alerts)}")
    for a in alerts:
        print(f"  • user_id={a['user_id']}  порог={a['threshold']}  направление={a['direction']}")

    for alert in alerts:
        user_id = alert["user_id"]
        threshold = alert["threshold"]
        direction = alert["direction"]

        triggered = (
            (direction == "выше" and rate >= threshold) or
            (direction == "ниже" and rate <= threshold)
        )
        if not triggered:
            continue

        print(f"  [!] СРАБОТАЛ: user_id={user_id}  {direction} {threshold}  курс={rate}")
        database.mark_alert_triggered(user_id)
        try:
            await bot.send_message(
                user_id,
                f"🔔 Алерт сработал! USD/JPY = {rate}. "
                f"Твой порог {threshold} ({direction}) пробит.",
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
        BotCommand(command="alert",       description="Настроить алерт"),
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
