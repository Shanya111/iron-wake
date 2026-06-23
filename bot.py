import asyncio
import os
import re
from pathlib import Path

import aiohttp
from aiogram import Bot, BaseMiddleware, Dispatcher, F
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
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

import analyzer
import config
import data_fetcher
import database
import scheduler as engine
from instruments import (
    INSTRUMENTS,
    ccxt_symbol,
    engine_codes,
    fmt,
    infer_decimals,
    resolve,
)

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


async def ask_openrouter(user_text: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    """Отправляет запрос в OpenRouter и возвращает ответ модели.

    system_prompt по умолчанию — основной промпт бота (свободный текст). Для
    гибридного разбора в /analyze передаётся отдельный «аналитический» промпт.
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


class AccessMiddleware(BaseMiddleware):
    """Гейт доступа: пускает в бот только одобренных админом пользователей.

    Неодобренному разрешена единственная команда — /start (отправить заявку).
    Всё остальное (команды, кнопки, свободный текст) блокируется вежливым
    сообщением. Админ проходит всегда, минуя проверку.
    """

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if ADMIN_ID is not None and user.id == ADMIN_ID:
            return await handler(event, data)

        access = database.get_access(user.id)
        if access == "approved":
            return await handler(event, data)

        # /start пропускаем — это вход и подача заявки на доступ.
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)

        # Доступа нет — блокируем, хендлер не вызываем.
        if access == "denied":
            note = "Администратор отклонил доступ к боту."
        else:
            note = ("⏳ Доступ к боту ещё не подтверждён администратором. "
                    "Отправь /start и дождись подтверждения.")
        if isinstance(event, Message):
            await event.answer(note)
        elif isinstance(event, CallbackQuery):
            await event.answer(note, show_alert=True)
        return


dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


class AlertStates(StatesGroup):
    waiting_pair = State()
    waiting_custom_pair = State()
    waiting_rate = State()
    waiting_confirm = State()


class ContactStates(StatesGroup):
    waiting_message = State()


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


def access_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки админу для решения по заявке на доступ."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"deny_{user_id}"),
    ]])


async def notify_admin_new_request(user) -> None:
    """Шлёт админу заявку на доступ с кнопками одобрения/отклонения."""
    if ADMIN_ID is None:
        return
    username = f"@{user.username}" if user.username else "—"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 Новая заявка на доступ:\n{user.full_name} {username} (id {user.id})",
            reply_markup=access_keyboard(user.id),
        )
    except Exception as e:
        print(f"notify_admin_new_request: не удалось уведомить админа: {e}")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    prev = database.get_access(user.id)  # None — пользователь пришёл впервые
    database.save_user(user.id, user.full_name)
    if ADMIN_ID is not None and user.id == ADMIN_ID:
        database.set_access(user.id, "approved")  # админ одобрен всегда
    access = database.get_access(user.id)

    if access != "approved":
        if access == "denied":
            await message.answer("К сожалению, администратор отклонил доступ к боту.")
            return
        await message.answer(
            f"👋 Привет, {user.first_name}! Доступ к боту выдаётся по подтверждению "
            "администратора. Я отправил ему твою заявку — как только одобрит, напишу тебе."
        )
        # Уведомляем админа только о новой заявке (чтобы повторный /start не спамил).
        if prev is None:
            await notify_admin_new_request(user)
        return

    await message.answer(
        f"Привет, {user.first_name}! Я iron-wake — слежу за курсами валют, металлов, "
        "нефти и крипты и пишу в момент, когда цена коснётся твоего уровня.\n\n"
        "Поставить алерт — /alert. Свои алерты — /myalerts. Написать админу — /write.\n\n"
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


@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: CallbackQuery):
    if ADMIN_ID is None or call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор", show_alert=True)
        return
    uid = int(call.data.removeprefix("approve_"))
    database.set_access(uid, "approved")
    await call.answer("Одобрен")
    try:
        await call.message.edit_text((call.message.text or "") + "\n\n✅ Одобрен")
    except Exception:
        pass
    try:
        await bot.send_message(uid, "✅ Доступ к боту подтверждён! Нажми /start, чтобы начать.")
    except Exception as e:
        print(f"cb_approve: не удалось уведомить {uid}: {e}")


@dp.callback_query(F.data.startswith("deny_"))
async def cb_deny(call: CallbackQuery):
    if ADMIN_ID is None or call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор", show_alert=True)
        return
    uid = int(call.data.removeprefix("deny_"))
    database.set_access(uid, "denied")
    await call.answer("Отклонён")
    try:
        await call.message.edit_text((call.message.text or "") + "\n\n❌ Отклонён")
    except Exception:
        pass
    try:
        await bot.send_message(uid, "К сожалению, администратор отклонил доступ к боту.")
    except Exception as e:
        print(f"cb_deny: не удалось уведомить {uid}: {e}")


@dp.message(Command("requests"))
async def cmd_requests(message: Message):
    """Список ожидающих заявок на доступ — на случай, если уведомление потерялось."""
    if ADMIN_ID is None or message.from_user.id != ADMIN_ID:
        await message.answer("Команда доступна только администратору.")
        return
    pending = database.get_pending_users()
    if not pending:
        await message.answer("Заявок на доступ нет.")
        return
    await message.answer(f"Ожидают подтверждения: {len(pending)}")
    for u in pending:
        await message.answer(
            f"🆕 {u['user_name']} (id {u['chat_id']})",
            reply_markup=access_keyboard(u["chat_id"]),
        )


ACCESS_LABEL = {"approved": "✅ одобрен", "pending": "⏳ ждёт", "denied": "🚫 отклонён"}


def users_text_and_kb() -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст списка всех пользователей со статусами + кнопка переключения доступа
    на каждого (бан одобренному / выдать доступ остальным). Себя (админа) не трогаем."""
    users = database.get_all_users()
    if not users:
        return "Пользователей нет.", None
    lines = ["👥 Пользователи бота:"]
    rows = []
    for u in users:
        uid = u["chat_id"]
        label = ACCESS_LABEL.get(u["access"], u["access"])
        blocked = " 🔇" if not u["is_active"] else ""
        if uid == ADMIN_ID:
            lines.append(f"• {u['user_name']} (id {uid}) — 👑 админ")
            continue
        lines.append(f"• {u['user_name']} (id {uid}) — {label}{blocked}")
        if u["access"] == "approved":
            rows.append([InlineKeyboardButton(
                text=f"🚫 Бан {u['user_name']}", callback_data=f"usr:ban:{uid}")])
        else:
            rows.append([InlineKeyboardButton(
                text=f"✅ Доступ {u['user_name']}", callback_data=f"usr:ok:{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return "\n".join(lines), kb


@dp.message(Command("users"))
async def cmd_users(message: Message):
    if ADMIN_ID is None or message.from_user.id != ADMIN_ID:
        await message.answer("Команда доступна только администратору.")
        return
    text, kb = users_text_and_kb()
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("usr:"))
async def cb_user_toggle(call: CallbackQuery):
    if ADMIN_ID is None or call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор", show_alert=True)
        return
    try:
        _, action, uid_s = call.data.split(":")
        uid = int(uid_s)
    except ValueError:
        await call.answer()
        return
    if uid == ADMIN_ID:
        await call.answer("Себя нельзя", show_alert=True)
        return
    if action == "ban":
        database.set_access(uid, "denied")
        await call.answer("Доступ снят")
        try:
            await bot.send_message(uid, "🚫 Администратор отозвал доступ к боту.")
        except Exception:
            pass
    else:
        database.set_access(uid, "approved")
        await call.answer("Доступ выдан")
        try:
            await bot.send_message(uid, "✅ Доступ к боту выдан! Нажми /start, чтобы начать.")
        except Exception:
            pass
    text, kb = users_text_and_kb()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


async def _set_access_by_command(message: Message, value: str, ok_note: str, user_note: str):
    """Общая логика /ban и /unban: разбор id из текста, смена доступа, уведомления."""
    if ADMIN_ID is None or message.from_user.id != ADMIN_ID:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Укажи id: например /ban 123456789 (id виден в /users).")
        return
    uid = int(parts[1])
    if uid == ADMIN_ID:
        await message.answer("Себя трогать нельзя.")
        return
    if database.get_access(uid) is None:
        await message.answer("Такого пользователя нет в базе.")
        return
    database.set_access(uid, value)
    await message.answer(ok_note.format(uid=uid))
    try:
        await bot.send_message(uid, user_note)
    except Exception:
        pass


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    await _set_access_by_command(
        message, "denied",
        ok_note="Доступ снят у id {uid}.",
        user_note="🚫 Администратор отозвал доступ к боту.",
    )


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    await _set_access_by_command(
        message, "approved",
        ok_note="Доступ выдан id {uid}.",
        user_note="✅ Доступ к боту выдан! Нажми /start, чтобы начать.",
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
    "/write — написать администратору\n"
    "/alert — поставить алерт на уровень (выбор инструмента)\n"
    "/myalerts — мои алерты (посмотреть и удалить)\n"
    "/analyze — анализ инструмента: тренд D1 + уровни + зоны ликвидности\n"
    "/subscribe — подписка на торговые сигналы (Spring/Upthrust)\n"
    "/signals — последние сигналы\n"
    "/settings — настройки порогов сигналов\n"
    "/cancel — отменить текущий сценарий"
)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("about"))
async def cmd_about(message: Message):
    await message.answer(
        "iron-wake — бот для мониторинга валютных пар.\n\n"
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

    # Себе (админу) рассылку не шлём — он автор, ему достаётся только отчёт.
    # Иначе твой же текст дублируется в твой чат рядом с отчётом.
    users = [uid for uid in database.get_active_consented_users() if uid != ADMIN_ID]
    if not users:
        await message.answer("Нет подписчиков для рассылки.")
        return

    sent = 0
    blocked = 0
    errors = 0
    for chat_id in users:
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота / удалил аккаунт — выключаем навсегда.
            database.mark_inactive(chat_id)
            blocked += 1
        except TelegramRetryAfter as e:
            # Флуд-лимит Telegram — ждём положенное и пробуем ещё раз. Подписчика
            # НЕ выключаем: он доступен, просто слишком быстро шлём.
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id, text)
                sent += 1
            except Exception as e2:
                print(f"broadcast: повтор для {chat_id} не удался: {e2}")
                errors += 1
        except Exception as e:
            # Временный сбой (сеть и т.п.) — НЕ выключаем подписчика, чтобы он не
            # выпал из всех будущих рассылок из-за одной разовой ошибки.
            print(f"broadcast: ошибка отправки {chat_id}: {e}")
            errors += 1
        await asyncio.sleep(0.05)  # бережём флуд-лимит Telegram между отправками

    report = f"Отправлено: {sent}, заблокировано: {blocked}"
    if errors:
        report += f", ошибок (повторим в след. раз): {errors}"
    await message.answer(report)


# Обработчики inline-кнопок
@dp.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    await call.message.answer(
        "iron-wake — бот для мониторинга валютных пар.\n\n"
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


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=start_keyboard())


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


# ── Торговый движок: анализ, подписки, сигналы, настройки ───────────────────────

ANALYST_PROMPT = (
    "Ты — трейдинг-ассистент. Тебе дают ГОТОВЫЕ числа анализа одного инструмента "
    "(название, тренд D1, уровни дневки и часовика, зоны ликвидности, стакан). "
    "Начни ответ с названия инструмента. Объясни простыми словами по-русски: куда "
    "смотреть, какие уровни приоритетны ПО ТРЕНДУ (в нисходящем — шорты от "
    "сопротивлений, в восходящем — лонги от поддержек), где вероятна пружина (Spring) "
    "или ложный пробой, и что говорит стакан. Когда называешь уровень — уточняй, "
    "дневной он или часовой. Не выдумывай числа — используй только данные. "
    "4–6 коротких предложений, по делу, без дисклеймеров."
)


def engine_keyboard(prefix: str, subscribed: set[str] | None = None) -> InlineKeyboardMarkup:
    """Кнопки инструментов движка — крипта + форекс (по 2 в ряд). prefix — начало
    callback_data. Если передан subscribed — отмечает галочкой подписанные (для /subscribe)."""
    codes = engine_codes()
    rows = []
    for i in range(0, len(codes), 2):
        row = []
        for c in codes[i:i + 2]:
            mark = "✅ " if subscribed and c in subscribed else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{INSTRUMENTS[c]['name']}", callback_data=f"{prefix}{c}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_orderbook(ob: dict, d: int) -> list[str]:
    """Блок «Стакан заявок» человеческим языком. ob — сводка analyzer.analyze_order_book."""
    pressure_ru = {
        "buyers":  "перевес покупателей 🟢 — заявок на покупку больше",
        "sellers": "перевес продавцов 🔴 — заявок на продажу больше",
        "balance": "силы примерно равны → — ни одна сторона не давит",
    }
    # Широкий спред = стакан тонкий (например, форекс на Kraken по ночам/выходным):
    # давление и стены по такому стакану недостоверны — честно об этом предупреждаем.
    thin = ob["spread_pct"] > 0.005
    if ob["spread_pct"] < 0.001:
        spread_word = "узкий (рынок ликвидный)"
    elif not thin:
        spread_word = "заметный"
    else:
        spread_word = "очень широкий (стакан по инструменту тонкий, доверять DOM не стоит)"
    lines = [
        "",
        "📖 Стакан заявок (что стоит в очереди прямо сейчас):",
        f"  • {pressure_ru[ob['pressure']]} ({ob['imbalance'] * 100:+.0f}%)",
        f"  • Спред (разрыв покупки и продажи): {fmt(ob['spread'], d)} ({ob['spread_pct'] * 100:.2g}%) — {spread_word}",
    ]
    if thin:
        return lines  # стены из тонкого стакана не показываем — это шум
    if ob["bid_wall"]:
        lines.append(
            f"  • 🧱 Крупная заявка на покупку у {fmt(ob['bid_wall']['price'], d)} "
            f"— может держать цену снизу (объём {ob['bid_wall']['amount']:.4g})"
        )
    if ob["ask_wall"]:
        lines.append(
            f"  • 🧱 Крупная заявка на продажу у {fmt(ob['ask_wall']['price'], d)} "
            f"— может тормозить рост (объём {ob['ask_wall']['amount']:.4g})"
        )
    return lines


def _render_levels(items: list[dict], d: int, last: float, limit: int) -> str:
    """Уровни строками: эмодзи + слово + цена, сверху вниз (дороже — выше, как на графике).
    Берём `limit` ближайших к цене (они важнее), близкие сливаем (чтобы «160.14, 160.14,
    160.13» не засоряли список), ⭐ — сильный уровень."""
    if not items:
        return "  —"
    kept: list[dict] = []
    for l in sorted(items, key=lambda x: abs(x["price"] - last)):
        dup = next((k for k in kept if k["type"] == l["type"]
                    and abs(k["price"] - l["price"]) <= l["price"] * 0.0015), None)
        if dup is None:
            kept.append(dict(l))
            if len(kept) >= limit:
                break
        elif l.get("strength") == "strong":
            dup["strength"] = "strong"  # сильный уровень важнее — поднимаем приоритет
    rows = []
    for l in sorted(kept, key=lambda x: x["price"], reverse=True):
        emoji = "🟥" if l["type"] == "resistance" else "🟩"
        word = "сопротивление" if l["type"] == "resistance" else "поддержка"
        star = " ⭐" if l.get("strength") == "strong" else ""
        rows.append(f"  {emoji} {word} {fmt(l['price'], d)}{star}")
    return "\n".join(rows)


def _format_analysis(info: dict, df, trend: str, levels: list[dict], zones: list[dict],
                     ob: dict | None = None) -> str:
    """Человеко-читаемый отчёт по числам анализа (без AI). Уровни сгруппированы по
    таймфреймам (дневка/часовик) с эмодзи — чтобы было видно, что старшее, что ближнее."""
    last = float(df["close"].iloc[-1])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(last)
    trend_ru = {
        "up": "восходящий ↑ (цена растёт)",
        "down": "нисходящий ↓ (цена падает)",
        "sideways": "боковик → (без чёткого направления)",
    }[trend]
    d1 = [l for l in levels if l.get("timeframe") == "D1" and l["type"] in ("support", "resistance")]
    h1 = [l for l in levels if l.get("timeframe") == "H1" and l["type"] in ("support", "resistance")]

    lines = [
        f"📊 {info['name']} — анализ",
        f"Цена сейчас: {fmt(last, d)}",
        f"Тренд на дневке (D1): {trend_ru}",
        "",
        "🔵 Дневка (D1) — крупные уровни (главные ориентиры):",
        _render_levels(d1, d, last, limit=6),
        "",
        "🟡 Часовик (H1) — ближние уровни (для входа):",
        _render_levels(h1, d, last, limit=8),
    ]
    if any(l.get("strength") == "strong" for l in h1):
        lines.append("  ⭐ — сильный: часовой уровень совпал с дневным")
    if zones:
        near = sorted(zones, key=lambda z: abs(z["price"] - last))[:6]
        zlines = []
        for z in sorted(near, key=lambda x: x["price"], reverse=True):
            tag = " (рядом с ценой)" if abs(z["price"] - last) <= last * 0.01 else ""
            zlines.append(f"  💰 {fmt(z['price'], d)}{tag}")
        lines += ["", "💰 Зоны ликвидности (где стояли крупные объёмы — магнит для цены):",
                  "\n".join(zlines)]
    if ob:
        lines += _format_orderbook(ob, d)
    return "\n".join(lines)


def _analysis_prompt(info: dict, last: float, trend: str, levels: list[dict],
                     zones: list[dict], ob: dict | None = None) -> str:
    """Компактная сводка чисел для AI-разбора (гибрид). Уровни разнесены по
    таймфреймам — чтобы AI в ответе уточнял, дневной уровень или часовой."""
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(last)
    d1 = [fmt(l["price"], d) for l in levels
          if l.get("timeframe") == "D1" and l["type"] in ("support", "resistance")]
    h1_strong = [fmt(l["price"], d) for l in levels
                 if l.get("timeframe") == "H1" and l.get("strength") == "strong"]
    zone_prices = [fmt(z["price"], d) for z in zones[:6]]
    dom = ""
    if ob:
        pressure_ru = {"buyers": "перевес покупателей", "sellers": "перевес продавцов",
                       "balance": "баланс сил"}
        dom = f"Стакан: {pressure_ru[ob['pressure']]} (дисбаланс {ob['imbalance'] * 100:+.0f}%)"
        if ob.get("bid_wall"):
            dom += f", крупная покупка у {fmt(ob['bid_wall']['price'], d)}"
        if ob.get("ask_wall"):
            dom += f", крупная продажа у {fmt(ob['ask_wall']['price'], d)}"
        dom += "\n"
    return (
        f"Инструмент: {info['name']}\n"
        f"Цена сейчас: {fmt(last, d)}\n"
        f"Тренд D1: {trend}\n"
        f"Уровни дневки (D1): {', '.join(d1) or 'нет'}\n"
        f"Сильные уровни часовика (H1): {', '.join(h1_strong) or 'нет'}\n"
        f"Зоны ликвидности: {', '.join(zone_prices) or 'нет'}\n"
        f"{dom}"
        "Дай короткий разбор."
    )


async def _do_analyze(message: Message, code: str):
    info = resolve(code)
    sym = ccxt_symbol(code)
    waiting = await message.answer(f"Анализирую {info['name']}...")
    try:
        d1 = await data_fetcher.get_candles(sym["symbol"], config.D1_TIMEFRAME, config.D1_LIMIT, sym["exchange"])
        h1 = await data_fetcher.get_candles(sym["symbol"], config.H1_TIMEFRAME, config.H1_LIMIT, sym["exchange"])
    except Exception:
        await waiting.delete()
        await message.answer("Не удалось получить данные сейчас, попробуй позже.")
        return

    trend = analyzer.get_trend(d1)
    levels = engine.analyze_and_store(code, d1, h1)  # считает и сохраняет уровни в БД
    zones = analyzer.find_liquidity_zones(d1)

    # Стакан (DOM) — доп. контекст по крипте. Ошибка стакана не критична для анализа.
    ob = None
    try:
        raw_ob = await data_fetcher.get_order_book(sym["symbol"], exchange=sym["exchange"])
        ob = analyzer.analyze_order_book(raw_ob)
    except Exception:
        ob = None

    await waiting.delete()
    await message.answer(_format_analysis(info, d1, trend, levels, zones, ob))

    # Гибрид: AI пишет человеческий разбор поверх чисел. Ошибка LLM не критична.
    try:
        last = float(d1["close"].iloc[-1])
        comment = await ask_openrouter(
            _analysis_prompt(info, last, trend, levels, zones, ob), system_prompt=ANALYST_PROMPT
        )
        # Подписываем, по какому инструменту разбор — сообщения в ленте отрываются
        # от заголовка, и без имени непонятно, о чём речь.
        await message.answer(f"🤖 {info['name']} — разбор:\n\n{comment}")
    except Exception:
        pass


@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].upper() in engine_codes():
        await _do_analyze(message, parts[1].upper())
        return
    await message.answer("Выбери инструмент для анализа:", reply_markup=engine_keyboard("analyze_"))


@dp.callback_query(F.data.startswith("analyze_"))
async def cb_analyze(call: CallbackQuery):
    code = call.data.removeprefix("analyze_")
    if code not in engine_codes():
        await call.answer()
        return
    await call.answer()
    await _do_analyze(call.message, code)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    subs = set(database.get_user_subscriptions(message.from_user.id))
    await message.answer(
        "Подписка на торговые сигналы (Spring/Upthrust). Нажми инструмент, чтобы "
        "включить/выключить уведомления:",
        reply_markup=engine_keyboard("subtoggle_", subs),
    )


@dp.callback_query(F.data.startswith("subtoggle_"))
async def cb_subtoggle(call: CallbackQuery):
    code = call.data.removeprefix("subtoggle_")
    if code not in engine_codes():
        await call.answer()
        return
    subs = set(database.get_user_subscriptions(call.from_user.id))
    if code in subs:
        database.remove_subscription(call.from_user.id, code)
        subs.discard(code)
        await call.answer("Отписка")
    else:
        database.add_subscription(call.from_user.id, code)
        subs.add(code)
        await call.answer("Подписка оформлена")
    await call.message.edit_reply_markup(reply_markup=engine_keyboard("subtoggle_", subs))


@dp.message(Command("signals"))
async def cmd_signals(message: Message):
    signals = database.get_recent_signals(10)
    if not signals:
        await message.answer("Сигналов пока нет. Подписаться на инструменты — /subscribe.")
        return
    status_label = {
        "pending": "⏳ ждём",
        "hit_tp": "✅ цель",
        "hit_sl": "🛑 стоп",
        "expired": "⌛ истёк",
    }
    lines = []
    for s in signals:
        info = resolve(s["instrument"])
        d = info["decimals"] if info["decimals"] is not None else infer_decimals(s["entry_price"])
        arrow = "🟢" if s["direction"] == "long" else "🔴"
        pat = "Spring" if s["pattern"] == "spring" else "Upthrust"
        star = "⭐" if s["priority"] == "high" else ""
        label = status_label.get(s["status"], s["status"])
        lines.append(
            f"{arrow}{star} {info['name']} {pat} — вход {fmt(s['entry_price'], d)}, "
            f"стоп {fmt(s['stop_loss'], d)}, цель {fmt(s['take_profit'], d)} [{label}]"
        )
    await message.answer("Последние сигналы:\n" + "\n".join(lines))


def settings_text() -> str:
    return (
        "⚙️ Настройки движка сигналов:\n"
        f"• Аномальный объём: × {config.get('VOL_MULT')}\n"
        f"• Глубина ложного пробоя: {config.get('BREAK_PCT') * 100:.3g}%\n"
        f"• Объём зоны ликвидности: × {config.get('LIQUIDITY_MULT')}\n\n"
        "Меняй пороги кнопками ниже (применяется сразу для всех сигналов):"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Объём ×1.3", callback_data="set:VOL_MULT:1.3"),
            InlineKeyboardButton(text="×1.5", callback_data="set:VOL_MULT:1.5"),
            InlineKeyboardButton(text="×2.0", callback_data="set:VOL_MULT:2.0"),
        ],
        [
            InlineKeyboardButton(text="Пробой 0.03%", callback_data="set:BREAK_PCT:0.0003"),
            InlineKeyboardButton(text="0.05%", callback_data="set:BREAK_PCT:0.0005"),
            InlineKeyboardButton(text="0.1%", callback_data="set:BREAK_PCT:0.001"),
        ],
    ])


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    # Пороги общие для всех сигналов → меняет только администратор (если задан).
    if ADMIN_ID is not None and message.from_user.id != ADMIN_ID:
        await message.answer("Настройки порогов доступны только администратору.")
        return
    await message.answer(settings_text(), reply_markup=settings_keyboard())


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(call: CallbackQuery):
    if ADMIN_ID is not None and call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор")
        return
    try:
        _, key, value = call.data.split(":")
        config.set_value(key, float(value))
    except (ValueError, KeyError):
        await call.answer("Не понял настройку")
        return
    await call.answer("Готово")
    await call.message.edit_text(settings_text(), reply_markup=settings_keyboard())


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


# ── Связь с администратором ─────────────────────────────────────────────────

@dp.message(Command("write"))
async def cmd_write(message: Message, state: FSMContext):
    if ADMIN_ID is None:
        await message.answer("Связь с администратором сейчас недоступна.")
        return
    await state.set_state(ContactStates.waiting_message)
    await message.answer(
        "✍️ Напиши одним сообщением, что передать администратору. Отмена — /cancel."
    )


@dp.message(ContactStates.waiting_message)
async def contact_message(message: Message, state: FSMContext):
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение не отправил. Попробуй ещё раз — /write.")
        return
    try:
        # (id ...) в тексте — якорь: по нему ответ админа reply'ем находит адресата.
        await bot.send_message(
            ADMIN_ID,
            f"✉️ Сообщение от {message.from_user.full_name} (id {message.from_user.id}):\n\n{text}",
        )
        await message.answer("Отправил администратору ✅. Ответ придёт сюда же.")
    except Exception as e:
        print(f"contact_message: не удалось доставить админу: {e}")
        await message.answer("Не получилось отправить сейчас, попробуй позже.")


# Ответ админа: reply'ем на пересланное сообщение пользователя → летит автору.
# Регистрируется ПЕРЕД free_text, чтобы перехватить ответы до отправки в LLM.
@dp.message(StateFilter(None), F.reply_to_message, F.text)
async def admin_reply(message: Message):
    src = message.reply_to_message
    is_user_msg = bool(src and src.text and src.text.startswith("✉️"))
    if ADMIN_ID is None or message.from_user.id != ADMIN_ID or not is_user_msg:
        # Не ответ админа на сообщение пользователя — обычный свободный текст.
        await free_text(message)
        return
    m = re.search(r"\(id (\d+)\)", src.text)
    if not m:
        await message.answer("Не нашёл, кому ответить.")
        return
    target = int(m.group(1))
    try:
        await bot.send_message(target, f"💬 Ответ администратора:\n\n{message.text}")
        await message.answer("Ответ отправлен ✅")
    except Exception as e:
        print(f"admin_reply: не удалось доставить {target}: {e}")
        await message.answer("Не удалось доставить — пользователь, видимо, заблокировал бота.")


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

    # Планировщик простых алертов «касание уровня» (как было).
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts, "interval", minutes=5)
    scheduler.start()
    await check_alerts()  # однократный прогон при старте для теста

    # Планировщик торгового движка: контекстный анализ (1ч) + мониторинг сигналов (5м).
    engine.setup(bot)
    await engine.run_analysis(bot)  # первичный анализ при старте

    await bot.set_my_commands([
        BotCommand(command="start",       description="Главное меню"),
        BotCommand(command="alert",       description="Поставить алерт на уровень"),
        BotCommand(command="myalerts",    description="Мои алерты"),
        BotCommand(command="analyze",     description="Анализ инструмента (тренд + уровни)"),
        BotCommand(command="subscribe",   description="Подписка на торговые сигналы"),
        BotCommand(command="signals",     description="Последние сигналы"),
        BotCommand(command="settings",    description="Настройки порогов сигналов"),
        BotCommand(command="cancel",      description="Отмена"),
        BotCommand(command="help",        description="Помощь"),
        BotCommand(command="privacy",     description="Политика конфиденциальности"),
        BotCommand(command="unsubscribe", description="Отписаться от уведомлений"),
        BotCommand(command="myid",        description="Узнать свой Telegram ID"),
        BotCommand(command="write",       description="Написать администратору"),
        BotCommand(command="pay",         description="Оплатить доступ к алертам"),
    ])
    try:
        await dp.start_polling(bot)
    finally:
        await data_fetcher.close()  # закрываем соединения бирж при остановке


if __name__ == "__main__":
    asyncio.run(main())
