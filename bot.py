import asyncio
import os
import random

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
dp = Dispatcher()

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


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Я iron-wake — бот для мониторинга USD/JPY.\n\nВыбери действие:",
        reply_markup=start_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — главное меню\n"
        "/help — эта справка\n"
        "/about — о боте\n"
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
        "/quote — цитата трейдера\n"
        "/tip — торговый совет\n"
        "/joke — шутка про трейдинг\n"
        "/note <текст> — сохранить заметку\n"
        "/notes — показать все заметки\n"
        "/clear — удалить все заметки"
    )
    await call.answer()


@dp.message(F.text)
async def echo(message: Message):
    await message.answer(message.text)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
