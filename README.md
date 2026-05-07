# Data Parsing System

> Система сбора и структуризации данных о людях. Этап 1 — ручной ввод через Telegram-бот.

**Статус:** 🟡 Этап 1 в работе (базовая версия готова, идут доработки)
**Репозиторий:** https://github.com/Saikokidd/tg-parser-bot
**Сервер:** VPS Zomro (`10.0.0.1`)

---

## 📊 Архитектура

```
┌─────────────────────────────────────────────────────┐
│                    VPS (Zomro)                      │
│                                                     │
│   ┌──────────────────┐      ┌──────────────────┐    │
│   │  parser_postgres │◄─────┤  tg-parser-bot   │    │
│   │   (Docker, 5433) │      │   (systemd)      │    │
│   └──────────────────┘      └──────────────────┘    │
│            │                          │             │
│            │                          ▼             │
│            │                   ┌────────────┐       │
│            │                   │  Telegram  │       │
│            │                   │   Bot API  │       │
│            │                   └────────────┘       │
└─────────────────────────────────────────────────────┘
```

---

## 🗄 База данных

**Контейнер:** `parser_postgres` (postgres:16-alpine)
**Порт:** `127.0.0.1:5433` (только локально)
**Compose:** `/opt/parser-postgres/docker-compose.yml`
**Init-скрипты:** `/opt/parser-postgres/init/`

### Подключение

```bash
docker exec -it parser_postgres psql -U tg_parser_user -d tg_parser
```

### Схема

#### `managers` — менеджеры (по имени)

| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| name | VARCHAR(255) UNIQUE NOT NULL | Имя сотрудника |
| is_active | BOOLEAN DEFAULT TRUE | Soft delete |
| created_at | TIMESTAMP | |

#### `manager_telegram_ids` — привязки ТГ-аккаунтов

| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| manager_id | FK → managers ON DELETE CASCADE | |
| telegram_id | BIGINT UNIQUE NOT NULL | |
| username | VARCHAR(255) | |
| added_at | TIMESTAMP | |

> Один менеджер может иметь несколько telegram_id — на случай смены аккаунта

#### `persons` — записи о людях

| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| full_name | VARCHAR(500) | ФИО |
| birth_date | DATE | Дата рождения |
| phone | VARCHAR(50) | Телефон |
| combat_mission | TEXT | БЗ — Боевое Задание |
| missing | BOOLEAN | БП — Безвести Пропавший |
| callsign | VARCHAR(255) | Позывной |
| military_unit | VARCHAR(255) | Б/Ч — Боевая Часть |
| added_by | FK → managers | Кто внёс |
| created_at, updated_at | TIMESTAMP | |

**Индексы:** `full_name`, `phone`, `birth_date`, `added_by`

---

## 🤖 Сервис: tg-parser-bot

**Расположение:** `/root/projects/tg-parser-bot/`
**Запуск:** `systemctl start tg-parser-bot`
**Статус:** `systemctl status tg-parser-bot`
**Логи:** `journalctl -u tg-parser-bot -f`
**Перезапуск:** `systemctl restart tg-parser-bot`
**Telegram:** [@sbor_base_holod_bot](https://t.me/sbor_base_holod_bot) (Собиратель базы)

### Структура проекта

```
tg-parser-bot/
├── bot/
│   ├── main.py                  # точка входа
│   ├── db/
│   │   ├── connection.py        # пул asyncpg
│   │   └── queries.py           # все запросы
│   ├── handlers/
│   │   ├── commands.py          # /start, /help, кнопки меню
│   │   ├── admin.py             # управление менеджерами (FSM)
│   │   └── input.py             # парсинг свободного текста
│   ├── parser/
│   │   └── text_parser.py       # regex-парсер
│   ├── middlewares/
│   │   └── access.py            # проверка доступа (admin/manager)
│   └── keyboards/
│       └── menus.py             # клавиатуры
├── venv/
├── .env                         # BOT_TOKEN, ADMIN_IDS, БД-креды
├── requirements.txt
└── README.md
```

### Зависимости

- `aiogram==3.7.0`
- `asyncpg==0.29.0`
- `python-dotenv==1.0.1`

### Конфигурация (`.env`)

```env
BOT_TOKEN=...
ADMIN_IDS=123456789,987654321  # через запятую
POSTGRES_DB=tg_parser
POSTGRES_USER=tg_parser_user
POSTGRES_PASSWORD=...
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5433
```

### Роли

| Роль | Источник | Возможности |
|------|----------|-------------|
| 👑 Администратор | `ADMIN_IDS` в `.env` | Управление менеджерами + всё что менеджер |
| 👤 Менеджер | Запись в `managers` + `manager_telegram_ids` | Внесение записей, своя база |
| 🚫 Нет доступа | Не в `.env` и не в БД | Бот отвечает "доступа нет" |

### Меню админа

```
/start
├── 📝 Внести запись (если есть привязка как менеджер)
├── 📊 Моя база
└── ⚙️ Управление ботом
    └── 👥 Менеджеры
        ├── ➕ Добавить менеджера (имя + telegram_id)
        ├── 🔄 Изменить ID менеджера (привязать доп. tg_id)
        ├── 📋 Список менеджеров
        └── ❌ Удалить менеджера (soft delete)
```

### Парсинг свободного текста

Распознаются поля через regex:

- **ФИО** — 2-3 слова с заглавной буквы (поддержка дефисов)
- **Дата** — `15.03.1985`, `15/03/85`, `1985-03-15`
- **Телефон** — украинские/российские, нормализуется в `+380...`
- **Позывной** — после слова `позывной`
- **Б/Ч** — после `б/ч` или `боевая часть`
- **БЗ** — после `б/з` или `боевое задание`
- **БП** — слово `БП` или `безвести пропавший`

### Дубль-детекция

При совпадении **2 из 3** полей (ФИО, дата рождения, телефон) бот показывает найденные записи и просит подтверждения.

---

## ⚙️ Деплой / Обновление

```bash
cd /root/projects/tg-parser-bot
git pull
venv/bin/pip install -r requirements.txt  # если изменились зависимости
systemctl restart tg-parser-bot
journalctl -u tg-parser-bot -n 30 --no-pager
```

### Применение миграций БД

```bash
docker exec -i parser_postgres psql -U tg_parser_user -d tg_parser < init/XX_migration.sql
```

---

## 📌 Ключевые архитектурные решения

- **БД в Docker, бот через systemd** — гибрид: БД проще держать изолированной, бот удобнее в systemd
- **Один менеджер = одно имя** — UNIQUE constraint, к одному имени привязывается N telegram_id
- **Дубль-детекция 2 из 3** — сбалансированное правило, не слишком строгое и не слишком слабое
- **Regex вместо LLM** — на сервере недостаточно RAM для локальной модели, а Claude API будет дорого при больших объёмах
- **Soft delete** — записи менеджеров не удаляются, ставится `is_active = FALSE`

---

## 📋 Этапы

### 🟡 Этап 1: Ручной ввод через бота (в работе)

#### Сделано
- [x] PostgreSQL в Docker (изолированный инстанс на 5433)
- [x] Схема БД: managers + manager_telegram_ids + persons
- [x] Бот в systemd с автозапуском
- [x] Роли: админ / менеджер / нет доступа
- [x] Маппинг N telegram_id на одного менеджера
- [x] Меню админа для управления менеджерами (FSM)
- [x] Регекс-парсер свободного текста
- [x] Дубль-детекция (2 из 3 полей)
- [x] Подтверждение перед сохранением

#### Предстоит
- [ ] Выгрузка личной базы менеджера в `.csv` / `.xlsx`
- [ ] Система статусов записей (несколько статусов на одну запись)
- [ ] Загрузка обновлённой базы со статусами обратно в БД
- [ ] Доработка парсера под реальные форматы менеджеров
- [ ] Возможные дополнительные поля в записях

### ⚪ Этап 2: Автопарсинг Telegram (не начат)

- Парсинг групп, каналов, чатов
- Telethon (MTProto), не Bot API
- Режимы: разовый дамп истории + live-мониторинг
- Источники задаются вручную и через БД

### ⚪ Этап 3: Парсинг ВКонтакте (не начат)

---

## 🔗 Связанные ноды

- VPS Zomro (`10.0.0.1`)
- callcenter_postgres (соседний контейнер на 5432)