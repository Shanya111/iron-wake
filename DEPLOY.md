# Деплой бота iron-wake (Docker-схема)

> Эта инструкция описывает **актуальную** схему: бот работает в Docker-контейнере
> `iron-wake-bot`, а не под systemd. Прежняя редакция (systemd + venv + `pip install`)
> устарела с 2026-06-12 — бот переехал на Docker. Если видишь на сервере systemd-сервис
> `bot.service` в статусе `inactive (disabled)` — это оставленная точка отката, не трогай её.

## Как всё устроено (один абзац для контекста)

Бот — Python-сервис (aiogram), он сам ходит наружу: к Telegram и OpenRouter — через
серверный прокси `127.0.0.1:1080` (xray), к Yahoo Finance — за котировками. Портов он не
слушает (исходящий long-polling), поэтому контейнер запущен с `network_mode: host`.
Код живёт в `/opt/apps/bot` (git-репозиторий, ветка `main`), а описание контейнера —
в compose-проекте `/opt/projects/web` (`docker-compose.yml` + `Dockerfile.bot`).

**Где что лежит (это важно понять до деплоя):**

| Сущность | Путь | Роль |
|---|---|---|
| Исходный код бота | `/opt/apps/bot` | git-репо `github.com/Shanya111/iron-wake`, ветка `main`. **Это build context** — образ собирается из этой папки. |
| compose-проект | `/opt/projects/web` | `docker-compose.yml` (сервисы `bot` + `site`) и `Dockerfile.bot`. |
| Dockerfile | `/opt/projects/web/Dockerfile.bot` | рецепт образа. `build.context` в compose указывает на `../../apps/bot` = `/opt/apps/bot`. |
| База данных | `/opt/apps/bot/bot.db` | смонтирована в контейнер томом `:rw` → **переживает пересборку образа**. |
| Секреты | `/opt/apps/bot/.env` | токен Telegram, прокси. `env_file` в compose. **НЕ в git, не трогать при деплое.** |
| Образ | тег `iron-wake-bot:local` | локальный, собирается вручную (см. ниже). |

## Предусловия (проверить до начала)

```bash
# 1. Контейнер сейчас работает и не в рестарт-петле:
docker inspect iron-wake-bot --format 'Running={{.State.Running}} RestartCount={{.RestartCount}}'
#   → Running=true RestartCount=0

# 2. Серверный прокси 1080 слушает (без него pip в сборке не достанет PyPI, бот не достучится до Telegram):
ss -tlnp | grep 1080
#   → LISTEN 127.0.0.1:1080

# 3. Свободное место на диске (сборка образа ~0.5 ГБ):
df -h /
#   → хотя бы пара ГБ свободно

# 4. Рабочее дерево кода чистое (только git pull, без локальных правок):
cd /opt/apps/bot && git status --porcelain
#   → пусто (или только untracked .dockerignore / bot.db.bak-* — это нормально, они в .gitignore/.dockerignore)
```

## Процедура деплоя

### Шаг 0 — узнать, что приедет в новом релизе

```bash
cd /opt/apps/bot
git fetch origin
git log --oneline HEAD..origin/main      # что нового
git diff --stat HEAD origin/main         # какие файлы и насколько меняются
```

**Особое внимание двум вещам:**
- **Появились ли НОВЫЕ `.py`-файлы** (в `git diff --stat`)? Если да — см. шаг 1, Dockerfile надо
  поправить, иначе новый модуль не попадёт в образ и бот упадёт с `ModuleNotFoundError`.
- **Изменился ли `requirements.txt`** (новые зависимости)? Если да — сборка образа их подтянет
  автоматически (отдельных действий не нужно), но знать полезно.
- **Есть ли в коде миграция БД** (меняется схема таблиц)? Если да — бэкап `bot.db` (шаг 3)
  становится критичным: миграция обычно необратима.

### Шаг 1 — правка Dockerfile, ЕСЛИ появились новые .py-файлы (иначе пропустить)

`Dockerfile.bot` копирует исходники в образ **поимённо**, а не всю папку:

```dockerfile
COPY bot.py database.py instruments.py system_prompt.md ./
```

Если в релизе появился новый файл (например, `foo.py`, который импортирует `bot.py`) — его
надо дописать в эту строку, иначе он не попадёт в образ.

```bash
cd /opt/projects/web
# бэкап Dockerfile перед правкой:
sudo cp -p Dockerfile.bot "Dockerfile.bot.bak.$(date +%Y%m%d)"
# добавить новый файл в строку COPY (пример — допиши foo.py):
sudo sed -i 's|^COPY bot.py database.py instruments.py system_prompt.md ./|COPY bot.py database.py instruments.py foo.py system_prompt.md ./|' Dockerfile.bot
grep -n COPY Dockerfile.bot   # сверить
```

> Почему поимённо, а не `COPY . ./`: чтобы в образ не утекли `.env`, `bot.db`, `.venv`, `.git`.
> Это сознательное решение. `.dockerignore` тоже их отсекает, но явный COPY — вторая страховка.

### Шаг 2 — забрать новый код (только fast-forward)

```bash
cd /opt/apps/bot
git pull --ff-only origin main
git rev-parse HEAD     # сверить, что HEAD = ожидаемый коммит
```

`--ff-only` гарантирует: если кто-то правил код прямо на сервере и истории разошлись —
pull откажется, а не сделает merge-кашу. Тогда сначала разобраться с расхождением.

### Шаг 3 — бэкап базы (ОБЯЗАТЕЛЬНО, если в релизе миграция БД)

```bash
cd /opt/apps/bot
cp -p bot.db "bot.db.bak-$(date +%Y%m%d)"
ls -l bot.db bot.db.bak-*       # сверить: размер бэкапа = размеру bot.db, не ноль
```

Это единственный необратимый момент: новый код на старте перестраивает таблицы. Без бэкапа
откат к старой схеме невозможен. Бэкап монтируется тем же томом, переживёт пересборку.

### Шаг 4 — пересобрать образ (через `--network=host`, иначе pip не достучится до PyPI)

```bash
cd /opt/projects/web && docker build --network=host \
  --build-arg HTTP_PROXY=http://127.0.0.1:1080 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:1080 \
  -f /opt/projects/web/Dockerfile.bot -t iron-wake-bot:local /opt/apps/bot
```

**Почему `--network=host` обязателен:** BuildKit (`docker compose build` / обычный `docker build`
без флага) собирает образ в изолированной сети, где `127.0.0.1` = сам build-контейнер, а НЕ хост.
Прокси xray слушает `127.0.0.1:1080` на хосте → из изолированной сборки недостижим →
`pip` падает с `ProxyError: Cannot connect to proxy ... Connection refused`. С `--network=host`
loopback сборки = loopback хоста → прокси виден, pip скачивает пакеты. `network_mode: host` из
compose тут НЕ помогает — он действует только на runtime, не на build.

Дождись `Successfully installed ...` и `naming to ... iron-wake-bot:local`. Тег совпадает с
`image:` в compose, поэтому следующий шаг подхватит свежий образ.

Быстрая проверка, что новый файл реально попал в образ (если правил Dockerfile на шаге 1):

```bash
docker run --rm --entrypoint ls iron-wake-bot:local -la /app/
```

### Шаг 5 — перезапустить контейнер на новом образе

```bash
cd /opt/projects/web
docker compose up -d bot      # пересоздаст iron-wake-bot на свежем образе
```

`up -d` сам увидит, что образ изменился, и пересоздаст контейнер. Тома (`bot.db`, `.env`,
`system_prompt.md`) переедут как есть. Кратковременный простой (секунды) — норма: Telegram
поставит апдейты в очередь и отдаст после старта.

> **Никогда не запускай одновременно контейнер И `bot.service`** — один токен + один long-polling
> не терпят двух читателей (`getUpdates` вернёт 409 Conflict). systemd-сервис должен оставаться
> `disabled`.

### Шаг 6 — проверка (фактом, а не на глаз)

```bash
# 6.1 Контейнер на новом образе, не в петле (повтори через 10-15 с — RestartCount не должен расти):
docker inspect iron-wake-bot --format 'Running={{.State.Running}} RestartCount={{.RestartCount}} Image={{.Config.Image}}'

# 6.2 Логи: миграция прошла, нет ModuleNotFoundError / Traceback:
docker compose logs --tail=80 bot
docker compose logs bot | grep -iE 'traceback|modulenotfound|error|exception|409|conflict'   # должно быть пусто

# 6.3 Бот реально жив (getMe через прокси; токен НЕ печатать в общий лог):
set -a; . /opt/apps/bot/.env; set +a
curl -s -o /dev/null -w 'getMe HTTP=%{http_code}\n' --max-time 15 \
  -x socks5h://127.0.0.1:1080 "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
#   → 200

# 6.4 (если была миграция БД) схема обновилась, старые данные на месте:
docker exec iron-wake-bot python -c \
  "import sqlite3;c=sqlite3.connect('/app/bot.db');print([r[1] for r in c.execute('PRAGMA table_info(alerts)')])"
```

В Telegram: `/start` отвечает, `/alert` показывает инструменты, `/myalerts` показывает алерты.

## Откат

Если бот не стартует, в петле рестартов или миграция БД сломалась:

```bash
cd /opt/projects/web && docker compose stop bot      # остановить сломанный контейнер

cd /opt/apps/bot
git reset --hard <ПРЕДЫДУЩИЙ_SHA>                    # вернуть код (git log --oneline -n 5 → найти SHA)
cp -p bot.db.bak-ГГГГММДД bot.db                     # вернуть БД из бэкапа шага 3

# если правил Dockerfile (шаг 1) — вернуть и его:
cd /opt/projects/web && sudo cp -p Dockerfile.bot.bak.ГГГГММДД Dockerfile.bot

# пересобрать образ на старом коде и поднять:
cd /opt/projects/web && docker build --network=host \
  --build-arg HTTP_PROXY=http://127.0.0.1:1080 --build-arg HTTPS_PROXY=http://127.0.0.1:1080 \
  -f /opt/projects/web/Dockerfile.bot -t iron-wake-bot:local /opt/apps/bot
docker compose up -d bot
docker compose logs --tail=50 bot
```

> Аварийный откат на systemd (только если Docker совсем сломан): `docker compose stop bot` →
> `sudo systemctl start bot.service`. Но это временная мера — каноничная схема Docker.

## Памятка про секреты

`.env` (токен Telegram, прокси) и `bot.db` (данные пользователей) деплой НЕ трогает — они в
`.gitignore`/`.dockerignore` и живут только на сервере + в Bitwarden. При деплое их не печатать
в логи, в git не коммитить.
