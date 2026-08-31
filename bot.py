import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

from aiogram import Bot, BaseMiddleware, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
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
import pattern_detector
import scheduler as engine
from llm import ANALYST_PROMPT, ask_openrouter, classify_intent
from instruments import (
    INSTRUMENTS,
    ccxt_symbol,
    engine_codes,
    fmt,
    infer_decimals,
    resolve,
    short,
)

load_dotenv()

_admin_raw = os.getenv("ADMIN_ID", "")
ADMIN_ID: int | None = int(_admin_raw) if _admin_raw.strip().isdigit() else None

PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "TEST_TOKEN_PLACEHOLDER")

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


class ContactStates(StatesGroup):
    waiting_message = State()


class NLConfirm(StatesGroup):
    """Подтверждение действия, распознанного из свободного текста (запись сделки)."""
    waiting = State()


class AlertStates(StatesGroup):
    """Ручная постановка алерта: инструмент → (своя монета с биржи) → уровень → подтверждение."""
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
        f"Привет, {user.first_name}! Я iron-wake — торговый ассистент по 16 фьючерсам "
        "BingX (14 крипто-пар, золото, нефть). Разбираю расклад по методике VSA "
        "и присылаю сигналы ложного пробоя (Spring/Upthrust) с ценой заявки, стопом и "
        "целью.\n\n"
        "Разбор инструмента — /analyze. Подписка на сигналы — /subscribe. "
        "Уведомить при касании уровня — /alert. Написать админу — /write.\n\n"
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
    "/alert — алерт: уведомлю, когда цена коснётся уровня\n"
    "/myalerts — мои алерты (посмотреть и удалить)\n"
    "/analyze — разбор инструмента глазами движка: что видит, чего не хватает\n"
    "/subscribe — подписка на торговые сигналы (Spring/Upthrust)\n"
    "/signals — последние сигналы\n"
    "/stats — статистика сигналов (винрейт, итог в R) за 30 дней / всё время\n"
    "/trades — журнал сделок (статус цель/стоп, закрытие)\n"
    "/settings — строгость отбора сигналов: реже, но качественнее\n"
    "/cancel — отменить текущий сценарий\n\n"
    "Можно просто писать словами — я пойму:\n"
    "• «что по биткоину» — сделаю разбор\n"
    "• «подпиши на эфир» / «мои сигналы» — подписка и список\n"
    "• «статистика за месяц» — сводка по сигналам (винрейт, итог в R)\n"
    "• «взял золото по 2390, стоп 2380, цель 2410» — запишу сделку в журнал\n"
    "• «алерт золото 2400» / «мои алерты» — поставлю алерт и покажу список\n"
    "Остальное (вопросы, разбор пересланного анализа) — отвечу как ассистент."
)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


ABOUT_TEXT = (
    "iron-wake — торговый ассистент по 16 бессрочным фьючерсам BingX "
    "(14 крипто-пар, золото, нефть Brent).\n\n"
    "Разбирает рынок по методике VSA: тренд дневки, уровни, объём, стакан заявок. "
    "Ищет ложные пробои Spring и Upthrust и присылает сигнал с ценой лимитной "
    "заявки, стопом и целью — а потом сам доводит его до исхода.\n\n"
    "Умеет алерты: скажет, когда цена дойдёт до уровня, который выбрал ты.\n\n"
    "Это подсказка, а не авто-торговля. Решение и риск — на трейдере.\n\n"
    "Автор: Аким."
)


@dp.message(Command("about"))
async def cmd_about(message: Message):
    await message.answer(ABOUT_TEXT)


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(
        "Политика конфиденциальности: бот iron-wake хранит chat_id, подписки на "
        "инструменты, личные пороги отбора сигналов, поставленные алерты и "
        "записанные тобой сделки — исключительно чтобы присылать сигналы и алерты "
        "и вести исход сделок. "
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
    await call.message.answer(ABOUT_TEXT)
    await call.answer()


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.answer(HELP_TEXT)
    await call.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=start_keyboard())


# ── Алерты «касание уровня» ──────────────────────────────────────────────────
#
# Алерт — это «скажи, когда цена дойдёт сюда». Он ничего не советует и ни во что не
# вмешивается: движок со своими сигналами живёт отдельно. Поставить можно двумя
# путями — кнопкой-колокольчиком прямо в отчёте /analyze (уровни движок уже посчитал,
# вводить число руками не надо) и вручную через /alert (любой уровень, любая пара —
# хоть из 21 кнопки реестра, хоть любой из 874 бессрочных контрактов BingX). Проверяет
# их scheduler.check_alerts по биржевой цене, правило срабатывания — в alerts.py.


def alert_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data="alert_save"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="alert_cancel"),
    ]])


def alert_pairs_keyboard() -> InlineKeyboardMarkup:
    """Инструменты кнопками (по 2 в ряд) + своя пара + отмена. Цены здесь НЕ
    запрашиваем — только названия, чтобы не дёргать биржу зря."""
    codes = list(INSTRUMENTS.keys())
    rows = [
        [InlineKeyboardButton(text=short(c), callback_data=f"alertpair_{c}")
         for c in codes[i:i + 2]]
        for i in range(0, len(codes), 2)
    ]
    rows.append([InlineKeyboardButton(text="✏️ Своя пара", callback_data="alertpair_custom")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="alertpair_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _same_price(a: float, b: float) -> bool:
    """Один и тот же уровень? Сравниваем с относительным допуском: цена теряет
    последние знаки при переносе через callback_data, и точное равенство float'ов
    на таком пути ловить нельзя."""
    return abs(a - b) <= max(abs(a), abs(b), 1.0) * 1e-9


async def _ask_alert_level(target: Message, state: FSMContext, pair: str) -> None:
    """Шаг «введи уровень»: показываем текущую цену и переводим FSM дальше.

    Цену берём тем же путём, что и проверка алертов (engine.alert_window) — с биржи.
    Показать одну цену, а сработать по другой было бы враньём.
    """
    info = resolve(pair)
    window = await engine.alert_window(pair)
    price = fmt(window["last"], window["decimals"])
    await state.update_data(pair=pair, decimals=window["decimals"])
    await state.set_state(AlertStates.waiting_rate)
    await target.answer(
        f"{info['short']} — сейчас {price}.\n\n"
        f"Введи уровень, на котором уведомить (например: {price}):"
    )


@dp.message(Command("alert"))
async def cmd_alert(message: Message, state: FSMContext):
    await state.set_state(AlertStates.waiting_pair)
    await message.answer("Выбери инструмент для алерта:", reply_markup=alert_pairs_keyboard())


@dp.callback_query(F.data == "alertpair_cancel", StateFilter(AlertStates.waiting_pair))
async def alert_pair_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


@dp.callback_query(F.data == "alertpair_custom", StateFilter(AlertStates.waiting_pair))
async def alert_pair_custom_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(AlertStates.waiting_custom_pair)
    await call.message.answer(
        "Введи монету или символ контракта BingX.\n"
        "Примеры: WIF, ARB, PEPE, ETH/USDC:USDC.\n"
        "Подойдёт любой из 874 бессрочных контрактов биржи — не только те 21, что "
        "на кнопках. Отмена — /cancel."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("alertpair_"), StateFilter(AlertStates.waiting_pair))
async def alert_pair_cb(call: CallbackQuery, state: FSMContext):
    code = call.data.removeprefix("alertpair_")
    if code not in INSTRUMENTS:  # на случай устаревшей кнопки из старого сообщения
        await call.answer()
        return
    try:
        await _ask_alert_level(call.message, state, code)
    except Exception:
        await call.message.answer("Не удалось получить цену сейчас, попробуй позже или /cancel.")
    await call.answer()


@dp.message(AlertStates.waiting_custom_pair)
async def alert_custom_pair_input(message: Message, state: FSMContext):
    """Своя пара: ищем контракт на бирже и запоминаем ЕГО символ, а не то, что ввели.

    Так алерт хранится в том же виде, в каком его потом будет читать проверка, и по
    нему сразу видно, откуда берётся цена."""
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введи монету, например WIF, или /cancel.")
        return
    symbol = await data_fetcher.find_symbol(query)
    if symbol is None:
        await message.answer(
            f"Не нашёл «{query}» среди бессрочных контрактов BingX. Проверь написание "
            "или попробуй другую монету, например WIF или ARB. Отмена — /cancel."
        )
        return
    try:
        await _ask_alert_level(message, state, symbol)
    except Exception:
        await message.answer("Не удалось получить цену сейчас, попробуй позже или /cancel.")


@dp.message(AlertStates.waiting_rate)
async def alert_rate_input(message: Message, state: FSMContext):
    try:
        rate = float((message.text or "").replace(",", "."))
        if rate <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введи корректное число, например 155.00:")
        return
    data = await state.get_data()
    info = resolve(data["pair"])
    await state.update_data(rate=rate)
    await state.set_state(AlertStates.waiting_confirm)
    await message.answer(
        f"Алерт — {info['short']} {fmt(rate, data['decimals'])}\n\n"
        "Уведомлю, как только цена коснётся этого уровня. Сохранить?",
        reply_markup=alert_confirm_kb(),
    )


@dp.message(AlertStates.waiting_confirm)
async def alert_confirm_text(message: Message):
    await message.answer("Нажми одну из кнопок:", reply_markup=alert_confirm_kb())


@dp.callback_query(F.data == "alert_save", StateFilter(AlertStates.waiting_confirm))
async def alert_save_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    pair, rate, d = data["pair"], data["rate"], data["decimals"]
    database.add_alert(call.from_user.id, pair, rate)
    info = resolve(pair)
    print(f"Новый алерт: {info['short']} {fmt(rate, d)} (user_id={call.from_user.id})")
    await call.message.answer(
        f"🔔 Алерт сохранён.\nУведомлю, когда {info['short']} коснётся {fmt(rate, d)}.\n"
        "Свои алерты — /myalerts.",
        reply_markup=start_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "alert_cancel", StateFilter(AlertStates.waiting_confirm))
async def alert_cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Настройка алерта отменена.", reply_markup=start_keyboard())
    await call.answer()


# ── Список и удаление алертов ───────────────────────────────────────────────

def _alert_label(a: dict) -> str:
    """«Bitcoin 79200.00» — имя пары и уровень. Точность: фиксированная для реестра,
    для своей пары — по величине уровня (цену тут не запрашиваем, чтобы /myalerts не
    ходил в сеть на каждую строку)."""
    info = resolve(a["pair"])
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(a["threshold"])
    return f"{info['short']} {fmt(a['threshold'], d)}"


def alerts_keyboard(rows: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Удалить {_alert_label(a)}",
                              callback_data=f"delalert_{a['id']}")]
        for a in rows
    ])


def render_alerts(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст списка алертов и клавиатура удаления (или None, если алертов нет)."""
    rows = database.get_user_alerts(user_id)
    if not rows:
        return ("У тебя нет активных алертов.\n"
                "Поставить: /alert или колокольчиком в отчёте /analyze."), None
    lines = "\n".join(f"• {_alert_label(a)}" for a in rows)
    return (f"🔔 Твои активные алерты:\n{lines}\n\n"
            "Сработавший алерт снимается сам."), alerts_keyboard(rows)


@dp.message(Command("myalerts"))
async def cmd_myalerts(message: Message):
    text, keyboard = render_alerts(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("delalert_"))
async def cb_delete_alert(call: CallbackQuery):
    alert_id = int(call.data.removeprefix("delalert_"))
    deleted = database.delete_alert(alert_id, call.from_user.id)
    await call.answer("Удалён" if deleted else "Уже удалён")
    text, keyboard = render_alerts(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass


# ── Журнал сделок ────────────────────────────────────────────────────────────

TRADE_STATUS_LABEL = {
    "open": "⏳ открыта", "hit_tp": "✅ цель", "hit_sl": "🛑 стоп", "closed": "☑️ закрыта",
}


def render_trades(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст журнала сделок + кнопки «закрыть» на каждую открытую (или None)."""
    trades = database.get_user_trades(user_id)
    if not trades:
        return ("Журнал сделок пуст. Запиши сделку свободным текстом, например: "
                "«взял золото по 2390, стоп 2380, цель 2410».", None)
    lines = ["📒 Журнал сделок:"]
    rows = []
    for t in trades:
        info = resolve(t["instrument"])
        d = info["decimals"] if info["decimals"] is not None else infer_decimals(t["entry_price"])
        arrow = "🟢" if t["direction"] == "long" else "🔴"
        label = TRADE_STATUS_LABEL.get(t["status"], t["status"])
        lines.append(
            f"{arrow} {info['short']} — вход {fmt(t['entry_price'], d)}, "
            f"стоп {fmt(t['stop_loss'], d)}, цель {fmt(t['take_profit'], d)} [{label}]"
        )
        if t["status"] == "open":
            rows.append([InlineKeyboardButton(
                text=f"☑️ Закрыть {info['short']} {fmt(t['entry_price'], d)}",
                callback_data=f"closetrade_{t['id']}",
            )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return "\n".join(lines), kb


@dp.message(Command("trades"))
async def cmd_trades(message: Message):
    text, keyboard = render_trades(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("closetrade_"))
async def cb_close_trade(call: CallbackQuery):
    trade_id = int(call.data.removeprefix("closetrade_"))
    closed = database.close_trade(trade_id, call.from_user.id)
    await call.answer("Закрыта" if closed else "Уже закрыта")
    text, keyboard = render_trades(call.from_user.id)
    await call.message.edit_text(text, reply_markup=keyboard)


# ── Торговый движок: анализ, подписки, сигналы, настройки ───────────────────────

def engine_keyboard(prefix: str, subscribed: set[str] | None = None) -> InlineKeyboardMarkup:
    """Кнопки инструментов движка — 21 фьючерс BingX (по 2 в ряд). prefix — начало
    callback_data. Если передан subscribed — отмечает галочкой подписанные (для /subscribe)."""
    codes = engine_codes()
    rows = []
    for i in range(0, len(codes), 2):
        row = []
        for c in codes[i:i + 2]:
            mark = "✅ " if subscribed and c in subscribed else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{short(c)}", callback_data=f"{prefix}{c}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _amount(value: float) -> str:
    """Объём заявки человеческим видом: 18660 → «18.7 тыс.», 272000 → «272 тыс.».

    Нужно из-за перехода на BingX: у контрактов на нефть и мелкие монеты объёмы
    в стакане шестизначные, и формат «:.4g» печатал их как 1.866e+04 — в телеграме
    это выглядит как ошибка, а не как число.
    """
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн"
    if value >= 1_000:
        return f"{value / 1_000:.1f} тыс."
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.4g}"


def _format_orderbook(ob: dict, d: int) -> list[str]:
    """Блок «Стакан заявок» человеческим языком. ob — сводка analyzer.analyze_order_book."""
    pressure_ru = {
        "buyers":  "перевес покупателей 🟢 — заявок на покупку больше",
        "sellers": "перевес продавцов 🔴 — заявок на продажу больше",
        "balance": "силы примерно равны → — ни одна сторона не давит",
    }
    # Широкий спред = стакан тонкий (у тонких пар набора — TON, ADA — это штатно):
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
            f"— может держать цену снизу (объём {_amount(ob['bid_wall']['amount'])})"
        )
    if ob["ask_wall"]:
        lines.append(
            f"  • 🧱 Крупная заявка на продажу у {fmt(ob['ask_wall']['price'], d)} "
            f"— может тормозить рост (объём {_amount(ob['ask_wall']['amount'])})"
        )
    return lines


def _alert_levels(ex: dict) -> list[tuple[dict, bool]]:
    """Уровни отчёта в порядке показа: (уровень, выше ли он цены).

    Один список и на текст, и на кнопки-колокольчики — чтобы строка в отчёте и
    кнопка под ним не разъехались: пользователь ставит алерт на то число, которое
    только что прочитал.
    """
    return ([(lvl, True) for lvl in reversed(ex["resistances"])]
            + [(lvl, False) for lvl in ex["supports"]])


def _level_row(lvl: dict, d: int, above: bool, bell: str = "") -> str:
    """Строка уровня в отчёте: цена, сила, таймфрейм и расстояние в ATR.

    bell — пометка перед ценой: 🔔 «на этот уровень можно поставить алерт кнопкой
    ниже», ✅ «алерт уже стоит». Стоит именно у числа, потому что алерт ставится
    ровно на это число.
    """
    emoji = "🟥" if above else "🟩"
    word = "сопротивление" if above else "поддержка"
    star = " ⭐" if lvl["strength"] == "strong" else ""
    tf = {"D1": "дневной", "H1": "часовой"}.get(lvl.get("timeframe", ""), "")
    tf = f" ({tf})" if tf else ""
    side = "выше" if above else "ниже"
    dist = (f"{lvl['dist_atr']:.2f} ATR" if lvl.get("dist_atr") is not None
            else fmt(lvl["dist"], d))
    return (f"  {emoji} {word} {bell}{fmt(lvl['price'], d)}{star}{tf} — {side} на {dist} "
            f"({lvl['dist_pct'] * 100:.2f}%)")


def alert_levels_kb(code: str, prices: list[float], user_id: int,
                    d: int) -> InlineKeyboardMarkup | None:
    """Колокольчики под отчётом /analyze — по кнопке на каждый показанный уровень.

    На кнопке только число: подпись должна оставаться короткой (урок /settings —
    длинная обрезается на телефоне посреди цифр), а что это за уровень, написано
    строкой выше в самом отчёте. ✅ — алерт уже стоит, нажатие его снимет.
    """
    if not prices:
        return None
    have = [a["threshold"] for a in database.get_user_alerts(user_id, code)]
    buttons = [
        InlineKeyboardButton(
            text=("✅ " if any(_same_price(p, t) for t in have) else "🔔 ") + fmt(p, d),
            callback_data=f"alrt:{code}:{p:.10g}",
        )
        for p in prices
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


SIDE_WORD = {"long": "🟢 лонг", "short": "🔴 шорт"}


def _format_engine_view(info: dict, ex: dict, zones: list[dict], ob: dict | None,
                        signals: dict, alerted: list[float] | None = None) -> str:
    """Отчёт /analyze: те же пять условий, по которым движок принимает решение.

    Смысл не в описании рынка вообще, а в ответе на вопрос «что здесь происходит
    глазами движка и чего не хватает до сигнала». Порядок пунктов совпадает с
    порядком проверок в pattern_detector (тренд → уровни → объём → пробой → R:R).
    """
    c = ex["close"]
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(c)
    trend_ru = {
        "up": "восходящий ↑ — движок берёт только ЛОНГИ",
        "down": "нисходящий ↓ — движок берёт только ШОРТЫ",
        "sideways": "боковик → — движок берёт обе стороны",
    }[ex["trend"]]
    # Стороны, которые тренд вообще разрешает: по ним и разбираем пробой и R:R.
    live = [s for s in ("long", "short") if ex["sides"][s]["trend_ok"]]

    lines = [
        f"📊 {info['short']} — что видит движок",
        f"Цена (закрытие часовой свечи {ex['bar_time'][:16]} UTC): {fmt(c, d)}",
        f"Средний размах свечи (ATR): {fmt(ex['atr'], d)} — это {ex['atr_pct'] * 100:.2f}% цены",
        "",
        f"1. Тренд дневки: {trend_ru}",
        "",
        "2. Ближайшие уровни (расстояние — в ATR, единой мерке для всех инструментов):",
    ]
    # Колокольчик у каждой цены — на неё можно повесить алерт кнопкой под отчётом.
    have = alerted or []
    rows = [
        _level_row(lvl, d, above=above,
                   bell=("✅ " if any(_same_price(t, lvl["price"]) for t in have)
                         else "🔔 "))
        for lvl, above in _alert_levels(ex)
    ]
    lines += rows or ["  — уровней рядом нет"]
    if rows:
        lines.append("  🔔 — кнопкой под отчётом поставлю алерт на этот уровень "
                     "(✅ — уже стоит, нажатие снимет).")

    mark = "✅" if ex["sides"][live[0]]["vol_ok"] else "❌"
    lines += [
        "",
        f"3. Объём последней закрытой свечи: {ex['vol_ratio']:.1f}× среднего "
        f"(нужно ×{ex['vol_mult']:g}) {mark}",
        "",
        "4. Ложный пробой (прокол уровня с возвратом обратно):",
    ]
    for side in live:
        s = ex["sides"][side]
        br = s["broken_level"]
        if br:
            star = " ⭐" if br["strength"] == "strong" else ""
            what = (f"ЕСТЬ — проколот уровень {fmt(br['price'], d)}{star}, "
                    "закрылись обратно ✅")
        else:
            what = (s["break_note"] or "нет") + " ❌"
        lines.append(f"  {SIDE_WORD[side]}: {what}")

    lines += ["", "5. Профит/риск, если бы входили прямо сейчас (справка, не порог):"]
    for side in live:
        s = ex["sides"][side]
        if s["rr"] is None:
            lines.append(f"  {SIDE_WORD[side]}: не посчитать (свеча без размаха)")
            continue
        # Ни галочки, ни слова «нужно»: порога R:R больше нет, сигнал берётся с
        # любым встречным уровнем. Число оставлено как справка — по нему видно,
        # тесно впереди или просторно.
        tgt = (f"цель {fmt(s['target'], d)}" if s["target"] is not None
               else f"цели впереди нет — ставится на {config.FALLBACK_RR:g} риска")
        lines.append(
            f"  {SIDE_WORD[side]}: 1:{s['rr']:.1f} — "
            f"риск {fmt(s['risk'], d)} ({s['risk_atr']:.2f} ATR), {tgt}")

    # Итог: что мешает — по каждой разрешённой трендом стороне.
    lines += ["", "🎯 Итог:"]
    fired = False
    for side in live:
        sig = signals.get(side)
        if sig:
            fired = True
            lines.append(
                f"  {SIDE_WORD[side]}: СИГНАЛ ЕСТЬ — заявка {fmt(sig['entry_price'], d)}, "
                f"стоп {fmt(sig['stop_loss'], d)}, цель {fmt(sig['take_profit'], d)}")
        else:
            blockers = ex["sides"][side]["blockers"] or ["условия сложились"]
            lines.append(f"  {SIDE_WORD[side]}: сигнала нет. Не хватает:")
            lines += [f"     • {b}" for b in blockers]
    if not fired:
        lines.append("  Ждём: сигнал родится на той свече, которая закроет все пункты выше.")

    f = ex["filters"]
    fl = ["вход у уровня " + (f"≤ {f['MAX_ENTRY_DIST_ATR']:g} ATR"
                              if f["MAX_ENTRY_DIST_ATR"] else "выкл"),
          "вдогонку " + (f"≤ {f['MAX_RISK_ATR']:g} ATR" if f["MAX_RISK_ATR"] else "выкл")]
    # Правило стопа называем словами: он уходит за экстремум ПАРЫ свечей, поэтому
    # бывает заметно дальше, чем «под свечой сигнала», и это удивляет.
    stop_rule = ("за дальним экстремумом свечи сигнала и предыдущей"
                 if config.STOP_STRUCT_BARS else "за экстремумом свечи сигнала")
    lines += ["", f"⚙️ Твои фильтры отбора: {', '.join(fl)} (меняются в /settings)",
              f"     Пороги движка: прокол ≥ {config.BREAK_ATR:g} ATR, объём "
              f"×{config.VOL_MULT:g}",
              f"     Стоп — {stop_rule}, плюс запас {config.STOP_ATR:g} ATR"]

    if zones:
        near = sorted(zones, key=lambda z: abs(z["price"] - c))[:6]
        zlines = []
        for z in sorted(near, key=lambda x: x["price"], reverse=True):
            tag = " (рядом с ценой)" if abs(z["price"] - c) <= c * 0.01 else ""
            zlines.append(f"  💰 {fmt(z['price'], d)}{tag}")
        lines += ["", "💰 Зоны ликвидности (где стояли крупные объёмы — магнит для цены):",
                  "\n".join(zlines)]
    if ob:
        lines += _format_orderbook(ob, d)
    return "\n".join(lines)


def _analysis_prompt(info: dict, ex: dict, zones: list[dict],
                     ob: dict | None, signals: dict) -> str:
    """Компактная сводка РАСКЛАДА ДВИЖКА для AI-разбора (а не сырых чисел рынка).

    Модели даём ровно то, что решает движок, — тогда и комментарий получается про
    сетап, а не пересказ цифр, которые пользователь уже прочитал выше.
    """
    c = ex["close"]
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(c)
    live = [s for s in ("long", "short") if ex["sides"][s]["trend_ok"]]

    def lvls(items, word):
        if not items:
            return f"{word}: нет"
        parts = [f"{fmt(l['price'], d)} ({l['dist_atr']:.2f} ATR"
                 f"{', сильный' if l['strength'] == 'strong' else ''})" for l in items]
        return f"{word}: " + ", ".join(parts)

    out = [
        f"Инструмент: {info['short']}",
        f"Цена (закрытие H1): {fmt(c, d)}; ATR {fmt(ex['atr'], d)} "
        f"({ex['atr_pct'] * 100:.2f}% цены)",
        f"Тренд D1: {ex['trend']} (разрешает: {', '.join(live)})",
        lvls(ex["resistances"], "Сопротивления сверху"),
        lvls(ex["supports"], "Поддержки снизу"),
        f"Объём последней закрытой свечи: {ex['vol_ratio']:.1f}× среднего, "
        f"порог ×{ex['vol_mult']:g}",
    ]
    for side in live:
        s = ex["sides"][side]
        if signals.get(side):
            out.append(f"{side}: СИГНАЛ ЕСТЬ (ложный пробой подтверждён)")
        else:
            out.append(f"{side}: сигнала нет, мешает — " + "; ".join(s["blockers"]))
        if s["rr"] is not None:
            # Порога R:R у движка нет — модели даём число без слова «порог», иначе
            # она начнёт объяснять отбраковку, которой не происходит.
            out.append(f"{side}: профит/риск при входе сейчас 1:{s['rr']:.1f}")
    if zones:
        out.append("Зоны ликвидности: " + ", ".join(
            fmt(z["price"], d)
            for z in sorted(zones, key=lambda z: abs(z["price"] - c))[:5]))
    if ob:
        pressure_ru = {"buyers": "перевес покупателей", "sellers": "перевес продавцов",
                       "balance": "баланс сил"}
        dom = f"Стакан: {pressure_ru[ob['pressure']]} (дисбаланс {ob['imbalance'] * 100:+.0f}%)"
        if ob.get("bid_wall"):
            dom += f", крупная покупка у {fmt(ob['bid_wall']['price'], d)}"
        if ob.get("ask_wall"):
            dom += f", крупная продажа у {fmt(ob['ask_wall']['price'], d)}"
        out.append(dom)
    out.append("Прокомментируй расклад.")
    return "\n".join(out)


async def _do_analyze(message: Message, code: str, user_id: int):
    info = resolve(code)
    waiting = await message.answer(f"Анализирую {info['short']}...")
    try:
        # Свечи берём из источника инструмента — для всего движка это фьючерсы BingX.
        d1 = await engine.fetch_candles(code, config.D1_TIMEFRAME, config.D1_LIMIT)
        h1 = await engine.fetch_candles(code, config.H1_TIMEFRAME, config.H1_LIMIT)
    except Exception:
        await waiting.delete()
        await message.answer("Не удалось получить данные сейчас, попробуй позже.")
        return

    trend = analyzer.get_trend(d1)
    levels = engine.analyze_and_store(code, d1, h1)  # считает и сохраняет уровни в БД
    zones = analyzer.find_liquidity_zones(d1)

    # Разбор — по ЛИЧНЫМ фильтрам пользователя: он должен видеть свой отбор, а не чужой.
    settings = config.effective(database.get_user_settings(user_id))
    ex = pattern_detector.explain(h1, levels, trend, settings)
    if not ex.get("enough_history"):
        await waiting.delete()
        await message.answer("Слишком мало часовых свечей для разбора, попробуй позже.")
        return
    # Сам детектор гоняем тоже: если сигнал есть, показываем ЕГО числа, а не свои
    # пересчёты. Заодно это страховка от расхождения explain() и _detect.
    signals = {
        "long": pattern_detector.detect_spring(h1, levels, trend, settings),
        "short": pattern_detector.detect_upthrust(h1, levels, trend, settings),
    }

    # Стакан (DOM) есть у всех инструментов движка (BingX), включая золото и нефть —
    # на Yahoo его не было вовсе. Ошибка стакана анализ не валит (ob=None ниже).
    ob = None
    sym = ccxt_symbol(code)
    if sym:
        try:
            raw_ob = await data_fetcher.get_order_book(sym["symbol"], exchange=sym["exchange"])
            ob = analyzer.analyze_order_book(raw_ob)
        except Exception:
            ob = None

    await waiting.delete()
    # Колокольчики: уровни отчёта кнопками. Уже поставленные алерты по этому
    # инструменту отмечаем и в тексте, и на кнопке — одним запросом к базе.
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(ex["close"])
    have = [a["threshold"] for a in database.get_user_alerts(user_id, code)]
    prices = [lvl["price"] for lvl, _ in _alert_levels(ex)]
    await message.answer(
        _format_engine_view(info, ex, zones, ob, signals, alerted=have),
        reply_markup=alert_levels_kb(code, prices, user_id, d),
    )

    # Гибрид: AI комментирует РАСКЛАД, а не пересказывает числа. Ошибка LLM не критична.
    try:
        comment = await ask_openrouter(
            _analysis_prompt(info, ex, zones, ob, signals), system_prompt=ANALYST_PROMPT
        )
        # Подписываем, по какому инструменту разбор — сообщения в ленте отрываются
        # от заголовка, и без имени непонятно, о чём речь.
        await message.answer(f"🤖 {info['short']} — разбор:\n\n{comment}")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("alrt:"))
async def cb_alert_level(call: CallbackQuery):
    """Колокольчик под /analyze: ставит алерт на уровень, повторное нажатие — снимает.

    Уровень движок уже посчитал, поэтому число не вводится руками — оно едет в
    callback_data кнопки. Оттуда же берутся цены для перерисовки клавиатуры: считать
    весь отчёт заново ради одной галочки незачем.
    """
    try:
        _, code, raw = call.data.split(":")
        price = float(raw)
    except ValueError:
        await call.answer("Кнопка устарела — открой /analyze заново", show_alert=True)
        return
    user_id = call.from_user.id
    info = resolve(code)
    d = info["decimals"] if info["decimals"] is not None else infer_decimals(price)

    same = [a for a in database.get_user_alerts(user_id, code)
            if _same_price(a["threshold"], price)]
    if same:
        for a in same:
            database.delete_alert(a["id"], user_id)
        await call.answer(f"Алерт на {fmt(price, d)} снят")
    else:
        database.add_alert(user_id, code, price)
        await call.answer(f"🔔 {fmt(price, d)} — напишу, когда цена дойдёт")

    markup = call.message.reply_markup
    prices = [float(b.callback_data.split(":")[2])
              for row in (markup.inline_keyboard if markup else [])
              for b in row if (b.callback_data or "").startswith("alrt:")]
    keyboard = alert_levels_kb(code, prices, user_id, d)

    # Пометку в самом отчёте переставляем тоже: колокольчик стоит у числа, и если
    # оставить его прежним, текст начнёт спорить с кнопкой. Правим ровно одну строку —
    # ту, где стоит эта цена, — вместо пересчёта всего отчёта ради одной галочки.
    # Строка-пояснение («🔔 — кнопкой под отчётом…») не заденется: ищем пометку
    # вместе с числом.
    was, now = ("✅ ", "🔔 ") if same else ("🔔 ", "✅ ")
    label = fmt(price, d)
    text = call.message.text or ""
    try:
        if was + label in text:
            await call.message.edit_text(text.replace(was + label, now + label, 1),
                                         reply_markup=keyboard)
        else:
            await call.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].upper() in engine_codes():
        await _do_analyze(message, parts[1].upper(), message.from_user.id)
        return
    await message.answer("Выбери инструмент для анализа:", reply_markup=engine_keyboard("analyze_"))


@dp.callback_query(F.data.startswith("analyze_"))
async def cb_analyze(call: CallbackQuery):
    code = call.data.removeprefix("analyze_")
    if code not in engine_codes():
        await call.answer()
        return
    await call.answer()
    # call.message.from_user — это бот, поэтому id пользователя берём из call.from_user.
    await _do_analyze(call.message, code, call.from_user.id)


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
    # Сигналы персональные → показываем свои (плюс старые «общие», если были).
    signals = database.get_recent_signals(message.from_user.id, 10)
    if not signals:
        await message.answer("Сигналов пока нет. Подписаться на инструменты — /subscribe.")
        return
    # Статусы: вход теперь лимитной заявкой, поэтому у сигнала есть состояние ДО
    # сделки. «Заявка снята» — сделки не было вовсе, и в винрейт она не идёт.
    status_label = {
        "waiting_fill": "📥 заявка стоит",
        "filled": "⏳ в сделке",
        "expired_unfilled": "⏹ заявка снята",
        "pending": "⏳ в сделке",   # старые сигналы рыночного входа
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
        risk = abs(s["entry_price"] - s["stop_loss"])
        rr = abs(s["take_profit"] - s["entry_price"]) / risk if risk else 0
        word = "заявка" if s["status"] == "waiting_fill" else "вход"
        lines.append(
            f"{arrow}{star} {info['short']} {pat} — {word} {fmt(s['entry_price'], d)}, "
            f"стоп {fmt(s['stop_loss'], d)}, цель {fmt(s['take_profit'], d)} "
            f"(1:{rr:.1f}) [{label}]"
        )
    await message.answer("Последние сигналы:\n" + "\n".join(lines))


# ── Сводная статистика по сигналам (/stats) ────────────────────────────────────

def compute_signal_stats(rows: list[dict]) -> dict:
    """Считает агрегаты по списку сигналов. Денег не храним → меряем в R
    (риск на сделку = 1R): цель дала +R:R, стоп = −1R.

    В винрейт и профит-фактор не входят три состояния, и по разным причинам:
      • ждём (заявка стоит / сделка открыта) и истёк — исход ещё не определён;
      • НЕ ИСПОЛНЕНА (expired_unfilled) — сделки не было вообще. Это не ноль в
        статистике, а отсутствие сделки: цена до лимитной заявки не дошла. Считать
        её нулевым результатом значило бы разбавлять винрейт событиями, которых не
        было. Показываем отдельным счётчиком — по нему видно, не слишком ли далеко
        от рынка стоят заявки.
    Разбивка по инструментам — только по закрытым (цель/стоп)."""
    tp = sl = pending = expired = unfilled = 0
    gross_profit = 0.0            # сумма плюсов в R (по факт. R:R достигших цели)
    gross_loss = 0.0             # сумма минусов в R (каждый стоп = 1R)
    by_instrument: dict[str, dict] = {}

    for s in rows:
        status = s["status"]
        # 'pending' — старые сигналы рыночного входа (до перехода на лимитный).
        if status in ("waiting_fill", "filled", "pending"):
            pending += 1
            continue
        if status == "expired_unfilled":
            unfilled += 1
            continue
        if status == "expired":
            expired += 1
            continue
        # закрытые: hit_tp / hit_sl
        risk = abs(s["entry_price"] - s["stop_loss"])
        rr = abs(s["take_profit"] - s["entry_price"]) / risk if risk else 0.0
        inst = by_instrument.setdefault(
            s["instrument"], {"tp": 0, "sl": 0, "net": 0.0}
        )
        if status == "hit_tp":
            tp += 1
            gross_profit += rr
            inst["tp"] += 1
            inst["net"] += rr
        elif status == "hit_sl":
            sl += 1
            gross_loss += 1.0
            inst["sl"] += 1
            inst["net"] -= 1.0

    decided = tp + sl
    return {
        "total": len(rows),
        "tp": tp, "sl": sl, "pending": pending, "expired": expired,
        "unfilled": unfilled,
        "decided": decided,
        "winrate": (tp / decided) if decided else None,
        "net_r": gross_profit - gross_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        # профит-фактор: плюсы ÷ минусы; нет стопов при наличии плюсов → бесконечность
        "profit_factor": (gross_profit / gross_loss) if gross_loss else
                         (float("inf") if gross_profit > 0 else None),
        "by_instrument": by_instrument,
    }


def render_stats(user_id: int, period: str) -> tuple[str, InlineKeyboardMarkup]:
    """Текст + кнопка-переключатель периода для /stats. period: '30' | 'all'."""
    since = None
    if period == "30":
        since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    rows = database.get_signals_since(user_id, since)
    st = compute_signal_stats(rows)

    head = "за 30 дней" if period == "30" else "за всё время"
    # Кнопка ведёт на противоположный период.
    if period == "30":
        btn = InlineKeyboardButton(text="📅 За всё время", callback_data="stats:all")
    else:
        btn = InlineKeyboardButton(text="📅 За 30 дней", callback_data="stats:30")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[btn]])

    if st["total"] == 0:
        return (
            f"📊 Статистика сигналов — {head}\n\n"
            "За период сигналов не было. Подписаться на инструменты — /subscribe.",
            keyboard,
        )

    lines = [
        f"📊 Статистика сигналов — {head}\n",
        f"Всего: {st['total']}",
        f"✅ Цель: {st['tp']}   🛑 Стоп: {st['sl']}   "
        f"⏳ Ждём: {st['pending']}   ⌛ Истекло: {st['expired']}",
    ]
    if st["unfilled"]:
        share = st["unfilled"] / st["total"]
        lines.append(f"⏹ Заявка не исполнилась: {st['unfilled']} ({share:.0%}) — "
                     "сделки не было, в винрейт не входит")
    lines.append("")

    if st["decided"]:
        lines.append(f"Винрейт: {st['winrate'] * 100:.0f}% "
                     f"({st['tp']} из {st['decided']} закрытых)")
        lines.append(f"Итог: {st['net_r']:+.1f}R")
        pf = st["profit_factor"]
        if pf is None:
            pf_str = "—"
        elif pf == float("inf"):
            pf_str = "∞ (без стопов)"
        else:
            pf_str = f"{pf:.2f}"
        lines.append(f"Профит-фактор: {pf_str}")
    else:
        lines.append("Закрытых сигналов пока нет — винрейт посчитаю, когда "
                     "сработают цель/стоп.")

    # Разбивка по инструментам (по закрытым), сильнейшие сверху.
    if st["by_instrument"]:
        lines.append("\nПо инструментам:")
        for code, d in sorted(st["by_instrument"].items(),
                              key=lambda kv: kv[1]["net"], reverse=True):
            info = resolve(code)
            lines.append(f"  • {info['short']}: {d['net']:+.1f}R "
                         f"({d['tp']}✅/{d['sl']}🛑)")

    return "\n".join(lines), keyboard


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    text, keyboard = render_stats(message.from_user.id, "30")
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery):
    period = call.data.removeprefix("stats:")   # '30' | 'all'
    text, keyboard = render_stats(call.from_user.id, period)
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer()


# Кнопки /settings — это ЦЕЛЫЕ измеренные конфигурации, а не отдельные ползунки.
# Поля: (MAX_ENTRY_DIST_ATR, MAX_RISK_ATR, подпись, сигналов в месяц, R на сделку,
#        нетто на свежей половине истории).
#
# Почему конфигурациями, а не двумя независимыми ползунками. Во-первых, так на кнопке
# видно, что именно ты получишь — частоту и качество, а не голое число ATR. Во-вторых,
# работает ровно ОДНА кнопка: нажал новую — прежняя выключилась. Значит включить
# сочетание, которого никто не мерил, невозможно в принципе.
#
# ПОЧЕМУ ИХ ПЯТЬ, А НЕ ДВЕНАДЦАТЬ. Сначала сюда попали все прогнанные конфигурации —
# двенадцать. На телефоне список не помещается, а подпись последней кнопки обрезается
# на середине числа. Но главное не это: выбор из двенадцати вариантов, отличающихся на
# сотые доли R, — это приглашение подбирать настройку под историю, чего в этом проекте
# и так уже случилось достаточно.
#
# КАК ОТОБРАНЫ ЭТИ ПЯТЬ. Список — ЛЕСТНИЦА: сверху вниз сигналов становится меньше, а
# средняя сделка — лучше, и так на каждой ступени без исключений (88→53→31→25→19 сделок
# при −0.044→+0.051→+0.094→+0.139→+0.179 R). Значит выбор честный: платишь частотой,
# получаешь качество, и никакого «а вдруг вон та кнопка лучше по обоим столбцам».
#
# В сетке из двенадцати такие кнопки БЫЛИ, и это главная причина сокращения. Вариант
# называется доминируемым, если другой даёт И больше сигналов, И лучшую среднюю сделку
# одновременно — выбирать его незачем никогда:
#   • «риск 0.5» (28/мес, +0.084) проигрывает «входу 0.1» (31/мес, +0.094);
#   • «вход 0.075 + риск 0.5» (13/мес, +0.164) проигрывает «+ риск 0.75» (19, +0.179);
#   • туда же «вход 0.05», «вход 0.1 + риск 0.5», «вход 0.05 + риск 0.5».
# Ни одной такой строки в списке ниже не осталось.
#
# ЗАМЕР: 19–20 августа 2026, 14 инструментов BingX, 833 дня часовых свечей, одна
# конвенция издержек на все строки (вход по закрытию мейкером). Одиночные фильтры —
# перебор dist2/risk2, сочетания — перебор both; три общие строки в них совпали до
# третьего знака, поэтому колонки сравнимы между собой. Полная сетка из двенадцати —
# в комментарии к config._DEFAULTS, она понадобится при следующем замере.
FILTER_CHOICES = [
    (0.0,   0.0,  "выкл",                  88, -0.044, -198.6),
    (0.0,   0.75, "риск 0.75",             53, +0.051,  -60.8),
    (0.1,   0.0,  "вход 0.1",              31, +0.094,   -4.7),
    (0.075, 0.0,  "вход 0.075",            25, +0.139,   +9.2),
    (0.075, 0.75, "вход 0.075+риск 0.75",  19, +0.179,  +24.8),
]


def _current_filters(user_id: int, is_admin: bool) -> tuple[float, float]:
    """Действующая пара (вход у уровня, вдогонку) — личная или общая."""
    eff = config.effective({} if is_admin else database.get_user_settings(user_id))
    return float(eff.get("MAX_ENTRY_DIST_ATR") or 0), float(eff.get("MAX_RISK_ATR") or 0)


def _known_choice(dist: float, risk: float) -> bool:
    """Есть ли такая конфигурация среди кнопок (иначе она осталась от прежнего списка)."""
    return any(abs(d - dist) < 1e-9 and abs(r - risk) < 1e-9 for d, r, *_ in FILTER_CHOICES)


def _filters_line(dist: float, risk: float) -> str:
    """«выключены» / «вход у уровня ≤ 0.075 ATR» / «… и вдогонку ≤ 0.5 ATR»."""
    parts = []
    if dist:
        parts.append(f"вход у уровня ≤ {dist:g} ATR")
    if risk:
        parts.append(f"вдогонку ≤ {risk:g} ATR")
    return " + ".join(parts) if parts else "выключены — движок берёт всё, что находит"


def settings_text(user_id: int, is_admin: bool) -> str:
    # Фильтры персональные: подписчик крутит их под себя, поверх общих значений.
    # Админ правит ОБЩИЙ дефолт (для всех, кто не настроил своё) — у него личных нет.
    overrides = {} if is_admin else database.get_user_settings(user_id)
    dist, risk = _current_filters(user_id, is_admin)
    personal = " (личное)" if overrides else ""
    # Список кнопок со временем меняется. Если человек когда-то выбрал конфигурацию,
    # которой в списке больше нет, галочки не будет ни на одной кнопке — и без этой
    # строки было бы непонятно, сломалось что-то или нет.
    stale = ("" if _known_choice(dist, risk) else
             "\nЭтой кнопки в списке больше нет — настройка работает, но чтобы её "
             "поменять, выбери любую из кнопок ниже.\n")

    footer = (
        "Это общий дефолт — для всех, кто не настроил своё. Кнопка применится сразу ко "
        "всем «по умолчанию»."
        if is_admin else
        "Это твоя личная настройка. «Сбросить» вернёт общую."
    )
    return (
        "⚙️ Строгость отбора сигналов\n\n"
        f"Сейчас: {_filters_line(dist, risk)}{personal}\n{stale}\n"
        "Оба фильтра про одно — не входить вдогонку за ушедшим движением. Пружина "
        "торгуется ОТ уровня: сняли ликвидность фитилём и вернулись; вход в конце "
        "размашистой свечи это уже погоня.\n"
        "• «вход у уровня» — не берём сигнал, если свеча пробоя закрылась далеко от "
        "пробитого уровня;\n"
        "• «вдогонку» — не берём, если сам риск сделки (вход → стоп) великоват.\n"
        "Меряется в ATR (средний размах свечи): «0.1% от цены» у биткоина и у золота "
        "значит разное, а «0.1 ATR» — одно и то же.\n"
        "Фильтры действуют на все 16 инструментов движка одинаково — валютных пар "
        "в движке больше нет.\n\n"
        "⚠️ Частота и средняя сделка с кнопок УБРАНЫ: они были измерены на движке с "
        "фильтром R:R 1:2, а его больше нет — сигналов стало в разы больше, и старые "
        "числа врут. Идёт новый замер, после него подписи вернутся.\n"
        "Работает ровно ОДНА кнопка: нажал новую — прежняя выключилась.\n\n"
        "⚠️ Честно: ПРИБЫЛЬНЫМ движок это не делает. После издержек ни одна строка не "
        "вышла в плюс на обеих половинах истории. Настройка про «реже и качественнее», "
        "а не про заработок.\n\n"
        + footer
    )


def settings_keyboard(user_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    """По кнопке на каждую измеренную конфигурацию. Галочка — действующая.

    Подпись держим короткой (до ~38 знаков): на телефоне длинная обрезается прямо
    посреди числа, и кнопка начинает врать. Поэтому R — с двумя знаками, а в
    сочетаниях нет пробелов вокруг «+».
    """
    cur_dist, cur_risk = _current_filters(user_id, is_admin)
    rows = []
    for dist, risk, label, per_month, r_trade, _fresh in FILTER_CHOICES:
        active = abs(cur_dist - dist) < 1e-9 and abs(cur_risk - risk) < 1e-9
        mark = "✅ " if active else ""
        # Числа с подписи СНЯТЫ 29 августа 2026 вместе со снятием фильтра R:R: они
        # измерены на прежней конфигурации и после правки движка врут. Как только
        # прогон rr_off.py в лаборатории досчитает — вернуть их сюда.
        rows.append([InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"set:{dist:g}:{risk:g}",
        )])
    # Подписчику — сброс личной настройки к общей. Админу нечего сбрасывать.
    if not is_admin:
        rows.append([InlineKeyboardButton(text="↩️ Сбросить к общим", callback_data="set:reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    # Доступно всем. Админ правит общий дефолт, подписчик — свои личные пороги.
    is_admin = ADMIN_ID is None or message.from_user.id == ADMIN_ID
    await message.answer(
        settings_text(message.from_user.id, is_admin),
        reply_markup=settings_keyboard(message.from_user.id, is_admin),
    )


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(call: CallbackQuery):
    is_admin = ADMIN_ID is None or call.from_user.id == ADMIN_ID
    parts = call.data.split(":")

    async def refresh() -> None:
        """Перерисовать меню. Нажатие на уже выбранную кнопку даёт тот же текст и ту же
        клавиатуру — Telegram отвечает на это ошибкой «не изменено», и она безобидна."""
        try:
            await call.message.edit_text(
                settings_text(call.from_user.id, is_admin),
                reply_markup=settings_keyboard(call.from_user.id, is_admin),
            )
        except TelegramBadRequest:
            pass

    # Сброс личной настройки к общей.
    if len(parts) == 2 and parts[1] == "reset":
        database.reset_user_settings(call.from_user.id)
        await call.answer("Сброшено к общим")
        await refresh()
        return

    # Кнопка — это ЦЕЛАЯ конфигурация: пишем оба ключа сразу. Поэтому прежняя настройка
    # всегда выключается, и включить непроверенное сочетание нельзя.
    try:
        _, dist, risk = parts
        dist, risk = float(dist), float(risk)
        if not _known_choice(dist, risk):
            raise ValueError("кнопка из прежнего списка")
    except ValueError:
        # Список кнопок мог смениться, а старое сообщение с ними осталось в чате.
        await call.answer("Кнопка устарела — открой /settings заново", show_alert=True)
        return

    values = {"MAX_ENTRY_DIST_ATR": dist, "MAX_RISK_ATR": risk}
    for key, value in values.items():
        if is_admin:
            config.set_value(key, value)                       # общий дефолт для всех
        else:
            database.set_user_setting(call.from_user.id, key, value)  # личная настройка
    await call.answer("Фильтры выключены" if not (dist or risk)
                      else f"Включено: {_filters_line(dist, risk)}")
    await refresh()


# ── Telegram Payments ─────────────────────────────────────────────────────────

@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Доступ к сигналам",
            description="Торговые сигналы Spring/Upthrust по фьючерсам BingX на 30 дней",
            payload="signals_access_30d",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label="Доступ к сигналам", amount=10000)],  # 100 руб = 10000 копеек
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
async def admin_reply(message: Message, state: FSMContext):
    src = message.reply_to_message
    is_user_msg = bool(src and src.text and src.text.startswith("✉️"))
    if ADMIN_ID is None or message.from_user.id != ADMIN_ID or not is_user_msg:
        # Не ответ админа на сообщение пользователя — обычный свободный текст.
        await free_text(message, state)
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


# ── Свободный текст → NL-роутер команд / журнал / чат (вне FSM-сценариев) ────

def nl_confirm_kb() -> InlineKeyboardMarkup:
    """Кнопки подтверждения действия, распознанного из текста."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="nlok"),
        InlineKeyboardButton(text="❌ Нет", callback_data="nlno"),
    ]])


async def _nl_chat(message: Message) -> None:
    """Обычный свободный чат через LLM (как было) — fallback роутера."""
    thinking = await message.answer("Думаю...")
    try:
        reply = await ask_openrouter(message.text)
        await thinking.delete()
        await message.answer(reply)
    except Exception:
        await thinking.delete()
        await message.answer("Не получилось ответить, попробуй через минуту")


async def _nl_log_trade(message: Message, state: FSMContext, intent: dict) -> None:
    """Намерение «записать сделку» из текста. Нужны вход, стоп и цель — иначе исход
    не отследить. Направление определяем по числам (надёжнее, чем по словам)."""
    raw = str(intent.get("instrument") or "").strip().upper()
    if not raw:
        await message.answer("По какому инструменту сделка? Например: "
                             "«взял золото по 2390, стоп 2380, цель 2410».")
        return
    nums: dict[str, float | None] = {}
    for k in ("entry", "stop", "target"):
        try:
            nums[k] = float(str(intent.get(k)).replace(",", "."))
        except (TypeError, ValueError):
            nums[k] = None
    if not all(nums.values()):
        await message.answer("Чтобы вести сделку и следить за исходом, нужны вход, стоп "
                             "и цель. Например: «взял золото по 2390, стоп 2380, цель 2410».")
        return
    entry, stop, target = nums["entry"], nums["stop"], nums["target"]
    if stop < entry < target:
        direction = "long"
    elif stop > entry > target:
        direction = "short"
    elif intent.get("direction") in ("long", "short"):
        direction = intent["direction"]
    else:
        await message.answer("Не понял направление: стоп должен быть по одну сторону от "
                             "входа, а цель — по другую. Проверь числа.")
        return
    pair = raw
    if raw not in INSTRUMENTS:
        # Своя пара: ищем контракт на бирже и храним его символ. Не нашли — записывать
        # нечего: вести сделку до цели/стопа будет не по чему.
        pair = await data_fetcher.find_symbol(raw)
        if pair is None:
            await message.answer(
                f"Не нашёл «{raw}» среди бессрочных контрактов BingX. Журнал ведёт "
                "сделку по биржевым свечам, поэтому нужен контракт с биржи."
            )
            return
    info = resolve(pair)
    decimals = info["decimals"] if info["decimals"] is not None else infer_decimals(entry)
    await state.set_state(NLConfirm.waiting)
    await state.update_data(kind="trade", pair=pair, direction=direction,
                            entry=entry, stop=stop, target=target, decimals=decimals)
    arrow = "🟢 лонг" if direction == "long" else "🔴 шорт"
    await message.answer(
        f"Записать сделку в журнал: {info['short']} {arrow}\n"
        f"вход {fmt(entry, decimals)}, стоп {fmt(stop, decimals)}, цель {fmt(target, decimals)}?",
        reply_markup=nl_confirm_kb(),
    )


async def _nl_set_alert(message: Message, state: FSMContext, intent: dict) -> None:
    """Намерение «поставить алерт» из текста: проверяем уровень и инструмент, просим
    подтвердить кнопкой — на случай, если модель не так расслышала число."""
    raw = str(intent.get("instrument") or "").strip().upper()
    try:
        level = float(str(intent.get("level")).replace(",", "."))
        if level <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("Понял, что нужен алерт, но не разобрал уровень. "
                             "Напиши, например: «алерт золото 2400».")
        return
    if not raw:
        await message.answer("На какой инструмент ставим алерт? "
                             "Например: «алерт биткоин 70000».")
        return
    pair = raw
    if raw not in INSTRUMENTS:
        pair = await data_fetcher.find_symbol(raw)
        if pair is None:
            await message.answer(f"Не нашёл «{raw}» среди контрактов BingX. Уточни "
                                 "название или поставь алерт через /alert.")
            return
    info = resolve(pair)
    try:
        # Той же дорогой, что и проверка алертов, — с биржи. Заодно проверяем, что
        # контракт живой и цена по нему идёт.
        window = await engine.alert_window(pair)
    except Exception:
        await message.answer(f"Цена по «{info['short']}» сейчас не приходит. Попробуй "
                             "позже или поставь алерт через /alert.")
        return
    d = window["decimals"]
    await state.set_state(NLConfirm.waiting)
    await state.update_data(kind="alert", pair=pair, level=level, decimals=d)
    await message.answer(
        f"Поставить алерт: {info['short']} {fmt(level, d)}? "
        f"(сейчас {fmt(window['last'], d)})",
        reply_markup=nl_confirm_kb(),
    )


async def _nl_subscribe(message: Message, intent: dict, action: str) -> None:
    code = str(intent.get("instrument") or "").strip().upper()
    if code not in engine_codes():
        await message.answer("Подписка на сигналы — по крипте, золоту и нефти "
                             "(фьючерсы BingX). Открой /subscribe и выбери инструмент.")
        return
    info = resolve(code)
    if action == "subscribe":
        database.add_subscription(message.from_user.id, code)
        await message.answer(f"Подписал на сигналы по {info['short']}. Управление — /subscribe.")
    else:
        database.remove_subscription(message.from_user.id, code)
        await message.answer(f"Отписал от сигналов по {info['short']}.")


async def _nl_analyze(message: Message, intent: dict) -> None:
    code = str(intent.get("instrument") or "").strip().upper()
    if code not in engine_codes():
        await message.answer("Анализ — по инструментам движка: " +
                             ", ".join(engine_codes()) + ".\nВыбрать — /analyze.")
        return
    await _do_analyze(message, code, message.from_user.id)


@dp.message(F.text, StateFilter(None))
async def free_text(message: Message, state: FSMContext):
    """Свободный текст: сначала распознаём команду (NL-роутер), иначе — обычный чат.
    Любая осечка роутера безопасно сводится к чату (см. llm.classify_intent)."""
    intent = await classify_intent(message.text or "")
    action = intent.get("action")
    if action == "set_alert":
        await _nl_set_alert(message, state, intent)
    elif action == "my_alerts":
        await cmd_myalerts(message)
    elif action == "log_trade":
        await _nl_log_trade(message, state, intent)
    elif action == "analyze":
        await _nl_analyze(message, intent)
    elif action == "signals":
        await cmd_signals(message)
    elif action == "stats":
        await cmd_stats(message)
    elif action == "my_trades":
        await cmd_trades(message)
    elif action in ("subscribe", "unsubscribe"):
        await _nl_subscribe(message, intent, action)
    else:
        await _nl_chat(message)


@dp.callback_query(F.data == "nlok", StateFilter(NLConfirm.waiting))
async def cb_nl_ok(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    info = resolve(data["pair"])
    d = data["decimals"]
    if data.get("kind") == "alert":
        database.add_alert(call.from_user.id, data["pair"], data["level"])
        await call.message.edit_text(
            f"🔔 Алерт сохранён: {info['short']} {fmt(data['level'], d)}. "
            "Уведомлю при касании. Список — /myalerts."
        )
    elif data.get("kind") == "trade":
        bar_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        database.add_trade(call.from_user.id, data["pair"], data["direction"],
                           data["entry"], data["stop"], data["target"], bar_time)
        arrow = "🟢 лонг" if data["direction"] == "long" else "🔴 шорт"
        await call.message.edit_text(
            f"Записал в журнал: {info['short']} {arrow}, вход {fmt(data['entry'], d)}, "
            f"стоп {fmt(data['stop'], d)}, цель {fmt(data['target'], d)}.\n"
            "Напишу, когда цена дойдёт до цели или стопа. Журнал — /trades."
        )
    await call.answer()


@dp.callback_query(F.data == "nlno", StateFilter(NLConfirm.waiting))
async def cb_nl_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Отменил.")
    await call.answer()


@dp.message(StateFilter(NLConfirm.waiting))
async def nl_confirm_text(message: Message):
    await message.answer("Нажми «Да» или «Нет».", reply_markup=nl_confirm_kb())


async def main():
    database.init_db()

    # Планировщик торгового движка: контекстный анализ (1ч) + мониторинг сигналов (5м),
    # трекинг сигналов и сделок журнала (5м). Своего планировщика у bot.py больше нет —
    # простые алерты «касание уровня» убраны, и единственные фоновые задачи теперь у движка.
    engine.setup(bot)
    await engine.run_analysis(bot)  # первичный анализ при старте

    # Меню обычного пользователя (видят все). Админских команд тут нет. /settings есть:
    # она доступна всем — подписчик крутит там СВОЮ строгость отбора, админ — общий дефолт.
    await bot.set_my_commands([
        BotCommand(command="start",       description="Главное меню"),
        BotCommand(command="alert",       description="Алерт: уведомить при касании уровня"),
        BotCommand(command="myalerts",    description="Мои алерты"),
        BotCommand(command="analyze",     description="Разбор инструмента глазами движка"),
        BotCommand(command="subscribe",   description="Подписка на торговые сигналы"),
        BotCommand(command="signals",     description="Последние сигналы"),
        BotCommand(command="stats",       description="Статистика сигналов (винрейт, R)"),
        BotCommand(command="trades",      description="Журнал сделок"),
        BotCommand(command="settings",    description="Строгость отбора сигналов"),
        BotCommand(command="write",       description="Написать администратору"),
        BotCommand(command="cancel",      description="Отмена"),
        BotCommand(command="help",        description="Помощь"),
        BotCommand(command="privacy",     description="Политика конфиденциальности"),
        BotCommand(command="unsubscribe", description="Отписаться от уведомлений"),
        BotCommand(command="myid",        description="Узнать свой Telegram ID"),
        BotCommand(command="pay",         description="Оплатить доступ к сигналам"),
    ])

    # Персональное меню админа (только в чате ADMIN_ID): админские команды наверху,
    # /write тут не нужен — админу некому себе писать.
    if ADMIN_ID is not None:
        await bot.set_my_commands(
            [
                BotCommand(command="users",     description="Пользователи и доступ"),
                BotCommand(command="requests",  description="Заявки на доступ"),
                BotCommand(command="ban",       description="Снять доступ: /ban id"),
                BotCommand(command="unban",     description="Вернуть доступ: /unban id"),
                BotCommand(command="broadcast", description="Рассылка всем: /broadcast текст"),
                BotCommand(command="settings",  description="Строгость отбора сигналов"),
                BotCommand(command="start",     description="Главное меню"),
                BotCommand(command="alert",     description="Алерт: уведомить при касании уровня"),
                BotCommand(command="myalerts",  description="Мои алерты"),
                BotCommand(command="analyze",   description="Разбор инструмента глазами движка"),
                BotCommand(command="subscribe", description="Подписка на торговые сигналы"),
                BotCommand(command="signals",   description="Последние сигналы"),
                BotCommand(command="stats",     description="Статистика сигналов (винрейт, R)"),
                BotCommand(command="trades",    description="Журнал сделок"),
                BotCommand(command="help",      description="Помощь"),
                BotCommand(command="cancel",    description="Отмена"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    try:
        await dp.start_polling(bot)
    finally:
        await data_fetcher.close()  # закрываем соединения бирж при остановке


if __name__ == "__main__":
    asyncio.run(main())
