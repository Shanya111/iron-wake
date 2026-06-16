# iron-wake

Telegram-бот для мониторинга финансовых инструментов (форекс, металлы, нефть, крипта).

## Что делает
- Следит за курсами инструментов через yfinance (см. реестр `instruments.py`)
- Позволяет настроить алерт на уровень — уведомление когда цена **коснётся** заданного уровня
  (срабатывание ловится по диапазону минутных свечей, направление выбирать не нужно)
- Алертов у пользователя может быть много, на разные инструменты
- Поддерживает выбор инструмента из списка кнопок **или** ввод своего тикера Yahoo Finance
- Уведомляет в Telegram первым (бот пишет пользователю сам)
- Обрабатывает свободный текст через LLM
- Поддерживает рассылку администратором по всем согласившимся пользователям

## Стек
- Python + aiogram 3 (FSM, inline-кнопки)
- SQLite через database.py
- APScheduler (`AsyncIOScheduler`) — фоновая проверка алертов каждые 5 минут
- yfinance + pandas — котировки и минутные свечи по любому тикеру Yahoo Finance
- OpenRouter API → модель `deepseek/deepseek-v4-flash:free` — обработка свободного текста

## Структура файлов

```
iron-wake/
├── bot.py           — точка входа, все обработчики aiogram, FSM-сценарии, планировщик
├── database.py      — работа с SQLite + котировки: init_db(), add_alert(), get_price_window() и др.
├── instruments.py   — реестр инструментов (имя, тикер Yahoo, точность), fmt/infer_decimals/resolve
├── bot.db           — SQLite-база данных (в .gitignore, создаётся автоматически)
├── system_prompt.md — системный промпт для LLM (читается при старте бота)
├── .env             — секреты: TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, ADMIN_ID
├── .env.example     — пример переменных окружения (без значений)
├── requirements.txt — зависимости для деплоя
├── схема.md         — схема текущего функционала бота
└── схема-алерт.md   — схема FSM-сценария /alert
```

### Инструменты (`instruments.py`)

Единый реестр `INSTRUMENTS` (код → имя, тикер Yahoo, число знаков после запятой). Готовых пар 10:
USD/JPY, EUR/USD, GBP/USD, USD/CAD, Золото (`GC=F`), Нефть Brent (`BZ=F`), Bitcoin, Solana,
Ethereum, Toncoin (`TON11419-USD`). Плюс «своя пара» — любой тикер Yahoo вводится вручную и
проверяется на лету. `resolve(pair)` различает реестровый код и сырой тикер; для своей пары
точность подбирается по цене (`infer_decimals`).

### Таблица `alerts` (bot.db)

| Поле | Тип | Описание |
|---|---|---|
| id | INTEGER PK | Автоинкремент |
| user_id | INTEGER | Telegram ID пользователя (НЕ unique — алертов может быть много) |
| threshold | REAL | Уровень цены |
| pair | TEXT DEFAULT 'USDJPY' | Код инструмента из реестра или сырой тикер Yahoo (своя пара) |
| start_above | INTEGER NULL | С какой стороны от уровня была цена при первой проверке (NULL → ещё не инициализирован) |
| created_at | TEXT | Дата и время сохранения (ISO 8601) |
| is_triggered | INTEGER DEFAULT 0 | 1 — алерт уже сработал, повторно не отправляется (срабатывает один раз) |

Алертов на пользователя может быть много (разные инструменты и уровни). Каждый `/alert` создаёт
новую запись. Управление — команда `/myalerts` (список + удаление).

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

Текущий функционал (команды, inline-меню) описан в [схема.md](схема.md).

### Сценарий настройки алерта `/alert`

Реализован через aiogram FSM (`StatesGroup` / `FSMContext`).
Состояния: `waiting_pair` → (`waiting_custom_pair`) → `waiting_rate` → `waiting_confirm`.
Полная схема — [схема-алерт.md](схема-алерт.md).

```mermaid
flowchart TD
    START(["Пользователь: /alert"])

    START --> ASK_PAIR["Бот: Выбери инструмент\n[10 кнопок] [✏️ Своя пара] [Отмена]"]

    ASK_PAIR -->|кнопка инструмента| SHOW_PRICE
    ASK_PAIR -->|«Своя пара»| ASK_TICKER["Бот: Введи тикер Yahoo\n(EURGBP=X, AAPL...)"]
    ASK_PAIR -->|«Отмена»| CANCELLED["Бот: Настройка алерта отменена"]

    ASK_TICKER -->|тикер валиден| SHOW_PRICE
    ASK_TICKER -->|нет данных у Yahoo| ERR_TICKER["Бот: Не нашёл тикер,\nпопробуй ещё раз / cancel"]
    ERR_TICKER --> ASK_TICKER

    SHOW_PRICE["Бот: {Инструмент} — сейчас {цена}\nВведи уровень:"]
    SHOW_PRICE --> INPUT_RATE{Пользователь вводит...}

    INPUT_RATE -->|"/cancel"| CANCELLED
    INPUT_RATE -->|число| VALIDATE{Число > 0?}
    INPUT_RATE -->|некорректный текст| ERR_RATE["Бот: Введи число, например 155.00"]
    ERR_RATE --> INPUT_RATE

    VALIDATE -->|нет| ERR_RATE
    VALIDATE -->|да| CONFIRM["Бот: Алерт — {Инструмент} {уровень}\nУведомлю при касании. Сохранить?\n[ Сохранить ] [ Отмена ]"]

    CONFIRM --> INPUT_CONFIRM{Пользователь вводит...}

    INPUT_CONFIRM -->|"/cancel"| CANCELLED
    INPUT_CONFIRM -->|кнопка «Сохранить»| DB[("SQLite: alerts\nadd_alert(user_id, pair, threshold)")]
    INPUT_CONFIRM -->|кнопка «Отмена»| CANCELLED
    INPUT_CONFIRM -->|текст вместо кнопки| ERR_CONFIRM["Бот: Нажми одну из кнопок"]
    ERR_CONFIRM --> INPUT_CONFIRM

    DB --> SAVED["Бот: Алерт сохранён!\nУведомлю когда {Инструмент} коснётся {уровень}"]

    CANCELLED:::cancel
    SAVED:::success
    DB:::db

    classDef cancel fill:#f9e4e4,stroke:#c0392b,color:#333
    classDef success fill:#e4f9e8,stroke:#27ae60,color:#333
    classDef db fill:#e8f4fd,stroke:#2980b9,color:#333
```

При выборе инструмента (и для своей пары) бот сразу показывает **актуальную цену**. Цена
запрашивается только для выбранного инструмента — на этапе показа списка кнопок запросов к Yahoo нет.

### Система уведомлений (планировщик + рассылка)

#### Проверка алертов — `check_alerts()`

Запускается автоматически каждые **5 минут** через `AsyncIOScheduler` (APScheduler), а также один раз при старте бота.

**Логика касания вместо «выше/ниже»:** проверка идёт раз в 5 минут, поэтому ловим не одну точку,
а диапазон минутных свечей за период (`get_price_window` → low/high/last). Алерт срабатывает, если
уровень попал в `[low, high]` (цена доходила до него, хоть фитилём) ИЛИ цена перешла на другую сторону
уровня (`start_above` сменился). При первой проверке у алерта `start_above=NULL` — запоминаем сторону
и ждём следующего цикла (на этом шаге не срабатываем). Срабатывает один раз.

**Только нужные пары:** запрашиваются котировки лишь тех инструментов, на которые есть активные
алерты (по одному запросу на пару за цикл, через `asyncio.to_thread` — не блокируем event loop).

```mermaid
flowchart TD
    SCHED(["APScheduler\nкаждые 5 минут"])

    SCHED --> GET_ALERTS["get_pending_alerts()\nSELECT из alerts где is_triggered = 0"]
    GET_ALERTS --> PAIRS["Собрать distinct pair\n(только пары с алертами)"]
    PAIRS --> GET_WIN["для каждой пары:\nget_price_window(ticker)\n→ low / high / last"]
    GET_WIN -->|ошибка по паре| LOG_ERR["print ошибки\n(пропуск пары)"]

    GET_WIN --> FOR{Для каждого алерта}

    FOR -->|start_above = NULL| INIT["set_alert_side()\nзапомнить сторону, пропустить"]
    FOR -->|уровень в [low, high]\nИЛИ сторона сменилась| TRIGGERED["mark_alert_triggered()\nis_triggered = 1"]
    FOR -->|иначе| SKIP["Пропустить"]

    TRIGGERED --> SEND["bot.send_message(user_id)\n«Алерт сработал! Цена {инструм.}\nдоходила до {уровень}...»"]
    SEND -->|TelegramError| INACTIVE["(в check_alerts только лог;\nmark_inactive — в /broadcast)"]

    TRIGGERED:::db
    SEND:::bot
    LOG_ERR:::err

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
