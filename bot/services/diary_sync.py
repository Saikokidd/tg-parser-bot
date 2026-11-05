#!/usr/bin/env python3
"""
Синхронизация «Личного дневника сотрудника» с Google Sheets.

Что делает:
  1. Берёт родственников, внесённых менеджером за указанный день (по Киеву).
  2. Пишет их на лист с датой следующего рабочего дня в его Google-таблице.
  3. Деактивирует источники менеджера, чтобы назавтра список кнопок был пуст.

Запуск:
    venv/bin/python -m bot.services.diary_sync              # за сегодня, боевой
    venv/bin/python -m bot.services.diary_sync --dry-run    # без записи
    venv/bin/python -m bot.services.diary_sync --date 2026-11-04
    venv/bin/python -m bot.services.diary_sync --manager 2  # только один
    venv/bin/python -m bot.services.diary_sync --no-clear   # не чистить источники
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from bot.db.queries import get_pool

# ─────────────────────────── настройки ───────────────────────────

KYIV = ZoneInfo("Europe/Kyiv")
SA_FILE = os.getenv("GOOGLE_SA_FILE", "/root/projects/tg-parser-bot/google_sa.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER = ["Источник", "ФИО", "ДР", "Телефон", "Итог"]
NO_SOURCE = "Без источника"
SHEET_DATE_FMT = "%d.%m.%Y"

# ширина колонок в пикселях, по порядку HEADER
COL_WIDTHS = [280, 360, 130, 170, 420]
FONT_SIZE = 12
ROW_HEIGHT = 30
SHEET_ROWS = 300

logger = logging.getLogger("diary_sync")


# ─────────────────────────── даты ───────────────────────────

def day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """Границы киевских суток в наивном UTC — created_at в БД хранится в UTC."""
    start_local = datetime.combine(d, time.min, tzinfo=KYIV)
    end_local = start_local + timedelta(days=1)
    to_utc = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(start_local), to_utc(end_local)


def next_workday(d: date) -> date:
    """Следующий рабочий день: с пятницы перескакиваем на понедельник."""
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:  # 5=сб, 6=вс
        nd += timedelta(days=1)
    return nd


# ─────────────────────────── БД ───────────────────────────

async def fetch_sheets(manager_id: int | None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        sql = """
            SELECT d.manager_id, d.spreadsheet_id, m.name AS manager_name
            FROM diary_sheets d
            JOIN managers m ON m.id = d.manager_id
            WHERE d.is_active = TRUE
        """
        params = []
        if manager_id is not None:
            sql += " AND d.manager_id = $1"
            params.append(manager_id)
        sql += " ORDER BY m.name"
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def fetch_relatives(manager_id: int, day: date) -> list[dict]:
    """Родственники, внесённые менеджером за указанный киевский день."""
    start, end = day_bounds_utc(day)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH picked AS (
                SELECT DISTINCT ON (r.id)
                       r.id, r.full_name, r.birth_date, r.phone,
                       COALESCE(
                           NULLIF(s.name, ''),
                           NULLIF(pm.extra->>'source', ''),
                           $4
                       ) AS source_name
                FROM relatives r
                LEFT JOIN military_relatives mr ON mr.relative_id = r.id
                LEFT JOIN persons_military pm   ON pm.id = mr.military_id
                LEFT JOIN sources s             ON s.id = pm.source_id
                WHERE r.added_by = $1
                  AND r.created_at >= $2
                  AND r.created_at <  $3
                ORDER BY r.id, mr.id NULLS LAST
            )
            SELECT * FROM picked
            ORDER BY source_name, full_name
            """,
            manager_id, start, end, NO_SOURCE,
        )
        return [dict(r) for r in rows]


async def filter_unsynced(manager_id: int, sheet_date: date,
                          rows: list[dict]) -> list[dict]:
    """Отбросить то, что уже выгружено — защита от повторного запуска."""
    if not rows:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        done = await conn.fetch(
            """
            SELECT relative_id FROM diary_sync_log
            WHERE manager_id = $1 AND sheet_date = $2
            """,
            manager_id, sheet_date,
        )
    seen = {r["relative_id"] for r in done}
    return [r for r in rows if r["id"] not in seen]


async def mark_synced(manager_id: int, sheet_date: date, rows: list[dict]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO diary_sync_log (manager_id, sheet_date, relative_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (manager_id, sheet_date, relative_id) DO NOTHING
            """,
            [(manager_id, sheet_date, r["id"]) for r in rows],
        )


async def clear_sources(manager_id: int) -> int:
    """Деактивировать источники менеджера. Записи остаются, кнопки исчезают."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE sources SET is_active = FALSE, updated_at = NOW()
            WHERE owner_manager_id = $1 AND is_active = TRUE
            """,
            manager_id,
        )
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


# ─────────────────────────── Google ───────────────────────────

def gs_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def format_ws(sh: gspread.Spreadsheet, sheet_id: int) -> None:
    """Крупный шрифт, широкие колонки, жирная шапка — чтобы читалось с экрана."""
    reqs = []
    for idx, width in enumerate(COL_WIDTHS):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": idx, "endIndex": idx + 1},
            "properties": {"pixelSize": width},
            "fields": "pixelSize",
        }})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": SHEET_ROWS},
        "properties": {"pixelSize": ROW_HEIGHT},
        "fields": "pixelSize",
    }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id},
        "cell": {"userEnteredFormat": {"textFormat": {"fontSize": FONT_SIZE},
                                       "verticalAlignment": "MIDDLE"}},
        # поля только через точку: скобочный синтаксис Sheets тут не принимает
        "fields": "userEnteredFormat.textFormat.fontSize,"
                  "userEnteredFormat.verticalAlignment",
    }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": FONT_SIZE, "bold": True},
            "backgroundColorStyle": {"rgbColor": {"red": 0.906, "green": 0.937,
                                                  "blue": 0.996}},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat.textFormat.fontSize,"
                  "userEnteredFormat.textFormat.bold,"
                  "userEnteredFormat.backgroundColorStyle,"
                  "userEnteredFormat.verticalAlignment",
    }})
    sh.batch_update({"requests": reqs})


def get_or_create_ws(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=SHEET_ROWS, cols=len(HEADER))
        ws.update(values=[HEADER], range_name="A1", value_input_option="RAW")
        ws.freeze(rows=1)
        try:
            format_ws(sh, ws.id)
        except Exception:
            logger.exception("Не удалось отформатировать лист %s", title)
        return ws


def fmt_row(r: dict) -> list[str]:
    bd = r.get("birth_date")
    return [
        r.get("source_name") or NO_SOURCE,
        r.get("full_name") or "",
        bd.strftime("%d.%m.%Y") if bd else "",
        r.get("phone") or "",
        "",
    ]


# ─────────────────────────── основной проход ───────────────────────────

async def run(day: date, dry_run: bool, manager_id: int | None,
              clear: bool) -> None:
    sheet_date = next_workday(day)
    title = sheet_date.strftime(SHEET_DATE_FMT)
    logger.info(
        "Дневники: данные за %s → лист «%s», dry_run=%s",
        day.isoformat(), title, dry_run,
    )

    sheets = await fetch_sheets(manager_id)
    if not sheets:
        logger.warning("Нет активных таблиц в diary_sheets — нечего делать")
        return

    client = None if dry_run else gs_client()
    total = 0

    for s in sheets:
        mid, name = s["manager_id"], s["manager_name"]
        try:
            rows = await fetch_relatives(mid, day)
            fresh = await filter_unsynced(mid, sheet_date, rows)

            if not fresh:
                logger.info("%s (id=%s): за день ничего нет — пропуск", name, mid)
            else:
                if dry_run:
                    logger.info("%s (id=%s): записал бы %s строк", name, mid, len(fresh))
                    for r in fresh[:3]:
                        logger.info("    %s", fmt_row(r)[:4])
                else:
                    sh = client.open_by_key(s["spreadsheet_id"])
                    ws = get_or_create_ws(sh, title)
                    ws.append_rows([fmt_row(r) for r in fresh],
                                   value_input_option="RAW")
                    await mark_synced(mid, sheet_date, fresh)
                    logger.info("%s (id=%s): записано %s строк", name, mid, len(fresh))
                total += len(fresh)

            if clear:
                if dry_run:
                    logger.info("%s: источники были бы очищены", name)
                else:
                    n = await clear_sources(mid)
                    logger.info("%s: деактивировано источников — %s", name, n)

        except Exception:
            logger.exception("%s (id=%s): ошибка синхронизации", name, mid)

    logger.info("Готово. Всего строк: %s", total)


def main() -> None:
    p = argparse.ArgumentParser(description="Выгрузка дневников в Google Sheets")
    p.add_argument("--date", help="день сбора данных, ГГГГ-ММ-ДД (по умолчанию сегодня)")
    p.add_argument("--manager", type=int, help="только этот manager_id")
    p.add_argument("--dry-run", action="store_true", help="без записи в Sheets и БД")
    p.add_argument("--no-clear", action="store_true", help="не трогать источники")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("/root/projects/tg-parser-bot/logs/diary_sync.log",
                                encoding="utf-8"),
        ],
    )

    day = (datetime.strptime(args.date, "%Y-%m-%d").date()
           if args.date else datetime.now(KYIV).date())

    asyncio.run(run(day, args.dry_run, args.manager, not args.no_clear))


if __name__ == "__main__":
    main()
