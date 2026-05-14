# iron-wake

Telegram-бот для мониторинга валютной пары USD/JPY.

## Что делает
- Следит за объёмами торгов по паре USD/JPY
- Отслеживает зоны маржинальности
- Уведомляет в Telegram когда происходит значимое движение

## Стек
- Python + aiogram 3 (FSM, inline-кнопки)
- SQLite через database.py
- OpenRouter API → модель `deepseek/deepseek-v4-flash:free` — обработка свободного текста
- Источник рыночных данных — решается позже (возможно ccxt или yfinance)

## Структура файлов

```
iron-wake/
├── bot.py           — точка входа, все обработчики aiogram, FSM-сценарии, вызов OpenRouter
├── database.py      — работа с SQLite: init_db(), upsert_alert()
├── bot.db           — SQLite-база данных (в .gitignore, создаётся автоматически)
├── system_prompt.md — системный промпт для LLM (читается при старте бота)
├── .env             — секреты: TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY
├── схема.md         — схема текущего функционала бота
└── схема-алерт.md   — схема FSM-сценария /alert
```

### Таблица `alerts` (bot.db)

| Поле | Тип | Описание |
|---|---|---|
| id | INTEGER PK | Автоинкремент |
| user_id | INTEGER UNIQUE | Telegram ID пользователя |
| threshold | REAL | Пороговый курс USD/JPY |
| direction | TEXT | «выше» или «ниже» |
| created_at | TEXT | Дата и время сохранения (ISO 8601) |

Один пользователь — один алерт. При повторном сохранении запись обновляется.

## Принципы
- Простой и читаемый код — всё должно быть понятно без знания Python
- Модульная структура — легко добавлять новые пары и метрики
- Комментарии на русском

## LLM-интеграция

Свободный текст пользователя (всё что не команда и не кнопка) обрабатывается через **OpenRouter**.

| Параметр | Значение |
|---|---|
| Провайдер | [OpenRouter](https://openrouter.ai) |
| Модель | `deepseek/deepseek-v4-flash:free` |
| Системный промпт | `system_prompt.md` (читается при старте) |
| Переменная окружения | `OPENROUTER_API_KEY` в `.env` |
| Таймаут | 30 секунд |

Логика в `bot.py`: функция `ask_openrouter()` делает POST на `https://openrouter.ai/api/v1/chat/completions`. Пока модель думает — пользователю приходит «Думаю...», которое удаляется после ответа. При ошибке — «Не получилось ответить, попробуй через минуту».

### Маршрутизация сообщений

```mermaid
flowchart TD
    U(["Пользователь пишет в бот"])

    U --> TYPE{Тип сообщения}

    TYPE -->|"Команда (/start, /alert...)"| CMD["Обработчик команды\n(aiogram router)"]
    TYPE -->|"Нажатие inline-кнопки"| CB["Обработчик callback_query\n(aiogram router)"]
    TYPE -->|"Свободный текст\n(вне FSM)"| THINK["Бот: «Думаю...»"]

    CMD --> FSM["FSM-сценарий\nили прямой ответ"]
    CB --> FSM

    THINK --> OR[("OpenRouter API\ndeepseek/deepseek-v4-flash:free")]
    OR --> SYS["system_prompt.md\n(системный промпт)"]
    SYS --> OR
    OR -->|"Ответ получен"| DEL["Удалить «Думаю...»"]
    OR -->|"Ошибка / таймаут"| ERR["«Не получилось ответить,\nпопробуй через минуту»"]
    DEL --> REPLY["Бот: ответ модели"]

    FSM:::bot
    REPLY:::bot
    ERR:::err
    OR:::llm

    classDef bot fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef err fill:#f9e4e4,stroke:#c0392b,color:#333
    classDef llm fill:#f0e8ff,stroke:#8e44ad,color:#333
```

## Архитектура

Текущий функционал (команды, inline-меню, заметки) описан в [схема.md](схема.md).

### Сценарий настройки алерта `/alert`

Реализован через aiogram FSM (`StatesGroup` / `FSMContext`).
Полная схема — [схема-алерт.md](схема-алерт.md).

```mermaid
flowchart TD
    START(["Пользователь: /alert"])

    START --> ASK_RATE["Бот: Введи пороговый курс USD/JPY\n(например: 155.00)"]

    ASK_RATE --> INPUT_RATE{Пользователь вводит...}

    INPUT_RATE -->|"/cancel"| CANCELLED["Бот: Настройка алерта отменена"]
    INPUT_RATE -->|число| VALIDATE{Корректное\nчисло?}
    INPUT_RATE -->|некорректный текст| ERR_RATE["Бот: Введи число, например 155.00"]
    ERR_RATE --> INPUT_RATE

    VALIDATE -->|нет| ERR_RATE
    VALIDATE -->|да| ASK_DIR["Бот: Уведомить когда курс...\n[ Выше порога ] [ Ниже порога ]"]

    ASK_DIR --> INPUT_DIR{Пользователь вводит...}

    INPUT_DIR -->|"/cancel"| CANCELLED
    INPUT_DIR -->|кнопка «Выше порога»| DIR_ABOVE["direction = выше"]
    INPUT_DIR -->|кнопка «Ниже порога»| DIR_BELOW["direction = ниже"]
    INPUT_DIR -->|текст вместо кнопки| ERR_DIR["Бот: Нажми одну из кнопок\n[ Выше порога ] [ Ниже порога ]"]
    ERR_DIR --> INPUT_DIR

    DIR_ABOVE --> CONFIRM
    DIR_BELOW --> CONFIRM

    CONFIRM["Бот: Алерт — USD/JPY {direction} {порог}\nСохранить?\n[ Сохранить ] [ Отмена ]"]

    CONFIRM --> INPUT_CONFIRM{Пользователь вводит...}

    INPUT_CONFIRM -->|"/cancel"| CANCELLED
    INPUT_CONFIRM -->|кнопка «Сохранить»| DB[("SQLite: alerts\nupsert_alert()")]
    INPUT_CONFIRM -->|кнопка «Отмена»| CANCELLED
    INPUT_CONFIRM -->|текст вместо кнопки| ERR_CONFIRM["Бот: Нажми одну из кнопок\n[ Сохранить ] [ Отмена ]"]
    ERR_CONFIRM --> INPUT_CONFIRM

    DB --> SAVED["Бот: Алерт сохранён!\nУведомлю когда USD/JPY {direction} {порог}"]

    CANCELLED:::cancel
    SAVED:::success
    DB:::db

    classDef cancel fill:#f9e4e4,stroke:#c0392b,color:#333
    classDef success fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef db fill:#e8f4fd,stroke:#2980b9,color:#333
```

### Планируемый функционал:

```mermaid
flowchart TD
    U(["Пользователь"])

    %% Whitelist
    U --> WL{Пользователь\nв whitelist?}
    WL -->|нет| DENY["Извини, бот доступен\nтолько по приглашению"]
    WL -->|да| CMD["Команды доступны"]

    %% /rate
    CMD -->|"/rate"| RATE["Запрос текущего курса\nUSD/JPY к источнику данных"]
    RATE --> RATE_OK["USD/JPY: 152.34\n(обновлено HH:MM)"]
    RATE --> RATE_ERR["Не удалось получить курс.\nПопробуй позже"]

    %% /alert
    CMD -->|"/alert 155.00"| ALC{Порог\nуказан?}
    ALC -->|нет| ALC_ERR["Укажи порог:\n/alert 155.00"]
    ALC -->|да| ALC_SAVE["Алерт сохранён:\nUSD/JPY ≥ 155.00"]

    BACKGROUND(["Фоновая задача\n(планировщик)"])
    BACKGROUND -->|каждые N минут| CHECK{Курс достиг\nпорога?}
    CHECK -->|да| NOTIFY["Уведомление пользователю:\nUSD/JPY достиг 155.00!"]
    CHECK -->|нет| WAIT["Ждём следующей проверки"]

    %% /volume
    CMD -->|"/volume"| VOL["Загрузка 120 свечей\nUSD/JPY (данные)"]
    VOL --> VOL_CALC["Анализ зон объёма:\n• Point of Control\n• Value Area High/Low\n• Накопление / дистрибуция"]
    VOL_CALC --> VOL_AI["ИИ-комментарий по зонам\n(GPT / Claude)"]
    VOL_AI --> VOL_OUT["Сообщение: зоны + вывод"]
```

## Автор
Аким — вайбкодер, трейдер, термист
