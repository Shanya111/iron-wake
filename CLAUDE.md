# iron-wake

Telegram-бот для мониторинга валютной пары USD/JPY.

## Что делает
- Следит за курсом USD/JPY через yfinance (тикер `USDJPY=X`)
- Позволяет настроить алерт — уведомление когда курс пробьёт заданный порог
- Уведомляет в Telegram первым (бот пишет пользователю сам)
- Обрабатывает свободный текст через LLM
- Поддерживает рассылку администратором по всем согласившимся пользователям

## Стек
- Python + aiogram 3 (FSM, inline-кнопки)
- SQLite через database.py
- APScheduler (`AsyncIOScheduler`) — фоновая проверка алертов каждые 5 минут
- yfinance — получение текущего курса USD/JPY (`USDJPY=X`)
- OpenRouter API → модель `deepseek/deepseek-v4-flash:free` — обработка свободного текста

## Структура файлов

```
iron-wake/
├── bot.py           — точка входа, все обработчики aiogram, FSM-сценарии, планировщик
├── database.py      — работа с SQLite: init_db(), upsert_alert(), save_user() и др.
├── bot.db           — SQLite-база данных (в .gitignore, создаётся автоматически)
├── system_prompt.md — системный промпт для LLM (читается при старте бота)
├── .env             — секреты: TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, ADMIN_ID
├── .env.example     — пример переменных окружения (без значений)
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
| is_triggered | INTEGER DEFAULT 0 | 1 — алерт уже сработал, повторно не отправляется |

Один пользователь — один алерт. При повторном `/alert` запись обновляется, `is_triggered` сбрасывается в 0.

### Таблица `users` (bot.db)

| Поле | Тип | Описание |
|---|---|---|
| chat_id | INTEGER PK | Telegram chat_id пользователя |
| user_name | TEXT | Отображаемое имя (full_name) |
| joined_at | TEXT | Дата первого /start (ISO 8601) |
| consent | INTEGER DEFAULT 0 | 1 — пользователь дал согласие на обработку данных |
| consent_at | TEXT | Дата последнего изменения consent |
| is_active | INTEGER DEFAULT 1 | 0 — бот заблокирован пользователем |

Запись создаётся при каждом `/start`. При попытке отправить сообщение заблокировавшему пользователю — `is_active` ставится в 0.

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

### Система уведомлений (планировщик + рассылка)

#### Проверка алертов — `check_alerts()`

Запускается автоматически каждые **5 минут** через `AsyncIOScheduler` (APScheduler), а также один раз при старте бота.

```mermaid
flowchart TD
    SCHED(["APScheduler\nкаждые 5 минут"])

    SCHED --> GET_RATE["get_usd_jpy_rate()\nyfinance USDJPY=X"]
    GET_RATE -->|ошибка| LOG_ERR["print: ошибка получения курса\n(пропуск итерации)"]
    GET_RATE -->|курс получен| GET_ALERTS["get_pending_alerts()\nSELECT из alerts\nгде is_triggered = 0"]

    GET_ALERTS --> FOR{Для каждого алерта}

    FOR -->|direction=выше\nкурс >= threshold| TRIGGERED["mark_alert_triggered()\nis_triggered = 1"]
    FOR -->|direction=ниже\nкурс <= threshold| TRIGGERED
    FOR -->|не сработал| SKIP["Пропустить"]

    TRIGGERED --> SEND["bot.send_message(user_id)\n«Алерт сработал! USD/JPY = ...\nТвой порог ... пробит.»"]
    SEND -->|TelegramError| INACTIVE["mark_inactive(user_id)\nis_active = 0"]

    TRIGGERED:::db
    SEND:::bot
    LOG_ERR:::err
    INACTIVE:::err

    classDef bot fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef err fill:#f9e4e4,stroke:#c0392b,color:#333
    classDef db fill:#e8f4fd,stroke:#2980b9,color:#333
```

#### Управление согласием и рассылка

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | любой | `save_user()` + запрос согласия (inline-кнопки) |
| `/privacy` | любой | Текст политики конфиденциальности |
| `/unsubscribe` | любой | `set_consent(chat_id, 0)` — отключает уведомления |
| `/myid` | любой | Отвечает своим `chat_id` (нужен для настройки `ADMIN_ID`) |
| `/broadcast текст` | только ADMIN_ID | Рассылка всем `consent=1, is_active=1` пользователям |

#### Переменная `ADMIN_ID` в `.env`

```
ADMIN_ID=123456789   # Telegram ID администратора
```

Читается при старте: `ADMIN_ID = int(os.getenv("ADMIN_ID"))`. Если не задана — `/broadcast` недоступен всем. Узнать свой ID: команда `/myid` в боте.

#### Логика `/broadcast`

```mermaid
flowchart TD
    BC(["Администратор: /broadcast текст"])

    BC --> CHECK_ADMIN{from_user.id\n== ADMIN_ID?}
    CHECK_ADMIN -->|нет| DENY["«Нет доступа.»"]
    CHECK_ADMIN -->|да| CHECK_TEXT{Текст\nпустой?}
    CHECK_TEXT -->|да| HINT["«Укажи текст:\n/broadcast Ваше сообщение»"]
    CHECK_TEXT -->|нет| LOAD["get_active_consented_users()\nconsent=1, is_active=1"]

    LOAD --> LOOP{Для каждого\nchat_id}
    LOOP -->|успех| SENT["sent += 1"]
    LOOP -->|Exception| BLOCK["mark_inactive(chat_id)\nblocked += 1"]

    SENT --> REPORT
    BLOCK --> REPORT
    REPORT["«Отправлено: N, заблокировано: M»"]

    DENY:::err
    BLOCK:::err
    REPORT:::bot

    classDef bot fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef err fill:#f9e4e4,stroke:#c0392b,color:#333
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

    %% /volume
    CMD -->|"/volume"| VOL["Загрузка 120 свечей\nUSD/JPY (данные)"]
    VOL --> VOL_CALC["Анализ зон объёма:\n• Point of Control\n• Value Area High/Low\n• Накопление / дистрибуция"]
    VOL_CALC --> VOL_AI["ИИ-комментарий по зонам\n(GPT / Claude)"]
    VOL_AI --> VOL_OUT["Сообщение: зоны + вывод"]
```

## Приём оплаты

### Общее

Telegram Payments — встроенный механизм оплаты внутри Telegram. Работает через платёжного провайдера (ЮKassa, Robokassa и др.). Бот выставляет инвойс, пользователь платит не выходя из мессенджера.

Цены передаются в **копейках** (целое число): 100 рублей = `10000`.

### Переменная `PAYMENT_TOKEN` в `.env`

```
PAYMENT_TOKEN=381764678:TEST:...   # тестовый токен от BotFather
```

- **Тестовый токен** — содержит `:TEST:` в середине. Деньги не списываются, карту можно указать любую из тестового набора Telegram.
- **Боевой токен** — получить через BotFather → Payments → выбрать провайдера (ЮKassa или Robokassa). Требуется статус самозанятого или ИП, договор с провайдером.

Читается при старте: `PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")`. Если не задан — команда `/pay` отвечает «Оплата временно недоступна».

### Команда `/pay`

Отправляет пользователю инвойс через `bot.send_invoice()`:

| Параметр | Значение |
|---|---|
| `title` | Название продукта (например: «Премиум-подписка») |
| `description` | Краткое описание |
| `payload` | Внутренний идентификатор (например: `"premium_1month"`) |
| `provider_token` | `PAYMENT_TOKEN` из `.env` |
| `currency` | `"RUB"` |
| `prices` | Список `LabeledPrice` в копейках |

### Обработчики

| Обработчик | Тип | Что делает |
|---|---|---|
| `pre_checkout_query` | `PreCheckoutQuery` | Подтверждает корректность заказа — обязательно вызвать `answer_pre_checkout_query(ok=True)` в течение 10 сек, иначе платёж отменяется |
| `successful_payment` | `Message` (content_type=SUCCESSFUL_PAYMENT) | Фиксирует факт оплаты; `message.successful_payment` содержит детали транзакции |

### Схема флоу `/pay`

```mermaid
flowchart TD
    PAY(["Пользователь: /pay"])

    PAY --> CHECK_TOKEN{PAYMENT_TOKEN\nзадан?}
    CHECK_TOKEN -->|нет| UNAVAIL["«Оплата временно недоступна»"]
    CHECK_TOKEN -->|да| INVOICE["bot.send_invoice()\nИнвойс пользователю"]

    INVOICE --> USER_ACTION{Пользователь}
    USER_ACTION -->|закрыл| DONE["Ничего не происходит"]
    USER_ACTION -->|нажал Оплатить| PRE["pre_checkout_query\n(Telegram → бот)"]

    PRE --> VALIDATE{Заказ\nкорректен?}
    VALIDATE -->|да| OK["answer_pre_checkout_query(ok=True)"]
    VALIDATE -->|нет| FAIL["answer_pre_checkout_query(ok=False, ...)"]

    OK --> PAYMENT["Telegram проводит платёж"]
    PAYMENT --> SUCCESS["successful_payment\nФиксируем оплату в БД"]
    SUCCESS --> CONFIRM["«Оплата прошла! Спасибо»"]

    FAIL:::err
    UNAVAIL:::err
    CONFIRM:::success

    classDef success fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef err fill:#f9e4e4,stroke:#c0392b,color:#333
```

### Тестирование

В тестовом режиме (токен содержит `:TEST:`) Telegram показывает форму с тестовыми картами. Реальные деньги не списываются. Переключение на боевой режим — только замена `PAYMENT_TOKEN` в `.env`.

### Путь к боевому токену

1. Оформить статус самозанятого (приложение «Мой налог»).
2. Зарегистрироваться в ЮKassa или Robokassa и заключить договор.
3. В BotFather: **Payments** → выбрать провайдера → получить токен.
4. Заменить тестовый `PAYMENT_TOKEN` на боевой в `.env`.

## Автор
Аким — вайбкодер, трейдер, термист
