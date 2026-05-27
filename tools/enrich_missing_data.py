"""
Дозаполнение родственников без телефона через Sauron API.

Логика для каждого родственника без phone:
1. Пробив через Sauron API по ФИО + ДР
2. Из ответа собираем шаблон (телефон, адрес, СНИЛС, ИНН, паспорт, email)
3. Найденный телефон → voxlink (operator + region)
4. Найденные emails → smtp.bz (валидация)
5. Записываем в БД ТОЛЬКО если поле было пустым (не перетираем)

Пропускаем:
- Родственников без ДР (Sauron без даты возвращает шум)
- Родственников у которых уже есть phone

Использование:
    venv/bin/python -m tools.enrich_missing_data --dry-run        # без записи в БД
    venv/bin/python -m tools.enrich_missing_data --limit 5        # тест на 5
    venv/bin/python -m tools.enrich_missing_data                  # боевой
"""
import asyncio
import argparse
import logging
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.db.connection import get_pool, close_pool
from bot.services.sauron_api import query_person, split_full_name, SauronError
from bot.db.queries import insert_probiv_log
from bot.services.voxlink_service import lookup_phone
from bot.services.email_validator_service import validate_emails_parallel
from bot.parser.sauron_parser import build_relative_template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("enrich_missing")

LOG_FILE = PROJECT_ROOT / "parsed" / "_enrich_missing_log.txt"
PARALLEL = 3


# ────────────── DB-доступ ──────────────

async def fetch_relatives_to_enrich(limit: int | None = None, manager_id: int | None = None):
    """
    Берём родственников у кого:
    - phone IS NULL OR phone=''
    - есть ФИО (хотя бы фамилия + имя)
    - есть ДР (без него Sauron бесполезен)
    - менеджер-владелец имеет назначенный office (pvl или dp)
      — без этого мы не знаем какой счёт Sauron использовать.

    manager_id — если указан, только записи этого менеджера

    Возвращаемые поля включают office менеджера, чтобы process_one
    знал на какой счёт списать запрос.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        sql = """
            SELECT r.id, r.full_name, r.birth_date, r.address, r.extra,
                   m.id AS manager_id, m.office AS manager_office
            FROM relatives r
            JOIN managers m ON m.id = r.added_by
            WHERE (r.phone IS NULL OR r.phone = '')
              AND r.birth_date IS NOT NULL
              AND r.full_name IS NOT NULL
              AND TRIM(r.full_name) != ''
              AND ARRAY_LENGTH(STRING_TO_ARRAY(TRIM(r.full_name), ' '), 1) >= 2
              AND m.office IS NOT NULL
        """
        params = []
        if manager_id is not None:
            sql += f"\n  AND r.added_by = ${len(params) + 1}"
            params.append(manager_id)

        sql += "\nORDER BY r.id"
        if limit:
            sql += f"\nLIMIT {limit}"

        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def update_relative(relative_id: int, updates: dict, extra_updates: dict):
    """
    Обновить только заданные поля в relatives.
    extra_updates мерджится в существующий extra.
    """
    if not updates and not extra_updates:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Текущий extra
        row = await conn.fetchrow(
            "SELECT extra FROM relatives WHERE id = $1", relative_id
        )
        current_extra = dict(row["extra"] or {})
        # Применяем только те поля extra, которых ещё нет
        for k, v in extra_updates.items():
            if v and not current_extra.get(k):
                current_extra[k] = v

        sets = []
        values = [relative_id]
        idx = 2

        for col, val in updates.items():
            if val:
                sets.append(f"{col} = ${idx}")
                values.append(val)
                idx += 1

        sets.append(f"extra = ${idx}")
        values.append(current_extra)  # asyncpg + JSONB codec сериализует dict сам
        idx += 1

        sets.append("updated_at = NOW()")

        sql = f"UPDATE relatives SET {', '.join(sets)} WHERE id = $1"
        await conn.execute(sql, *values)


# ────────────── Обработка одного родственника ──────────────

async def process_one(rel: dict, dry_run: bool, log_lines: list, stats: dict):
    """
    Обработать одного родственника:
    Sauron → шаблон → voxlink + smtp.bz → запись в БД

    Использует office родственника (унаследованный от его менеджера)
    чтобы списать с правильного счёта Sauron.
    Также пишет в probiv_log с указанием manager_id — это позволяет
    разделить расход 'tool'-прогонов по офисам в отчётах.
    """
    rel_id = rel["id"]
    full_name = rel["full_name"]
    birth_date = rel["birth_date"]
    manager_id = rel["manager_id"]
    office = rel["manager_office"]

    try:
        ln, fn, mn = split_full_name(full_name)
    except ValueError as e:
        log_lines.append(f"[#{rel_id}] {full_name}: SKIP не парсится ФИО ({e})")
        stats["skipped_bad_name"] += 1
        return

    # Пробив через Sauron (на счёт офиса менеджера)
    try:
        result = await query_person(
            lastname=ln,
            firstname=fn,
            middlename=mn,
            day=birth_date.day,
            month=birth_date.month,
            year=birth_date.year,
            office=office,
        )
        # Логируем расход — успешный запрос.
        # manager_id указываем настоящий (а не None как раньше) —
        # чтобы можно было фильтровать tool-прогоны по офису.
        await insert_probiv_log(
            provider="sauron",
            context="tool",
            manager_id=manager_id,
            full_name=full_name,
            birth_date=birth_date,
            cost=float(result.get("cost", 0) or 0),
            success=True,
            office=office,
        )
    except SauronError as e:
        log_lines.append(f"[#{rel_id}] {full_name}: SAURON ERROR — {e}")
        stats["sauron_errors"] += 1
        # Логируем расход — неудача (часто оплачивается)
        await insert_probiv_log(
            provider="sauron",
            context="tool",
            manager_id=manager_id,
            full_name=full_name,
            birth_date=birth_date,
            cost=0,
            success=False,
            error=str(e),
            office=office,
        )
        return
    except Exception as e:
        log_lines.append(f"[#{rel_id}] {full_name}: UNEXPECTED — {e}")
        stats["sauron_errors"] += 1
        await insert_probiv_log(
            provider="sauron",
            context="tool",
            manager_id=manager_id,
            full_name=full_name,
            birth_date=birth_date,
            cost=0,
            success=False,
            error=str(e),
            office=office,
        )
        return

    # Собираем шаблон
    template = build_relative_template(result)
    if not template:
        log_lines.append(f"[#{rel_id}] {full_name}: NOT FOUND в Sauron")
        stats["not_found"] += 1
        return

    phone = template.get("phone") or ""
    if not phone:
        log_lines.append(f"[#{rel_id}] {full_name}: FOUND но без телефона "
                         f"(адрес={template.get('address') or '—'})")
        # Всё равно дозаполним остальное
        stats["found_no_phone"] += 1

    # Параллельно: voxlink (если есть телефон) + smtp.bz (если есть emails)
    voxlink_task = None
    smtpbz_task = None

    if phone:
        voxlink_task = asyncio.create_task(lookup_phone(phone))

    emails_top = template.get("emails_top") or []
    if emails_top:
        smtpbz_task = asyncio.create_task(_validate_emails_batch(emails_top))

    operator = ""
    region = ""
    if voxlink_task:
        try:
            info = await voxlink_task
            if info:
                operator = info.get("operator") or ""
                region = info.get("region") or ""
        except Exception as e:
            log_lines.append(f"[#{rel_id}] voxlink ERROR — {e}")

    valid_emails = []
    if smtpbz_task:
        try:
            valid_emails = await smtpbz_task
        except Exception as e:
            log_lines.append(f"[#{rel_id}] smtp.bz ERROR — {e}")

    # Готовим обновление
    updates = {}
    extra_updates = {}

    if phone:
        updates["phone"] = phone
    if template.get("address"):
        updates["address"] = template["address"]

    if operator:
        extra_updates["operator"] = operator
    if region:
        extra_updates["region"] = region
    if template.get("snils"):
        extra_updates["snils"] = template["snils"]
    if template.get("inn"):
        extra_updates["inn"] = template["inn"]
    if template.get("passport"):
        extra_updates["passport"] = template["passport"]
    if valid_emails:
        # Первый валидный — основной email; остальные через ', '
        extra_updates["email"] = valid_emails[0]

    summary = f"phone={'+' if phone else '-'} " \
              f"addr={'+' if template.get('address') else '-'} " \
              f"emails={len(valid_emails)}/{len(emails_top)}"

    if dry_run:
        log_lines.append(f"[#{rel_id}] {full_name}: DRY {summary}")
        stats["found"] += 1
        return

    try:
        await update_relative(rel_id, updates, extra_updates)
        log_lines.append(f"[#{rel_id}] {full_name}: OK {summary}")
        stats["enriched"] += 1
    except Exception as e:
        log_lines.append(f"[#{rel_id}] {full_name}: DB ERROR — {e}")
        stats["db_errors"] += 1


async def _validate_emails_batch(emails: list[str]) -> list[str]:
    """
    Прогнать список email через smtp.bz, вернуть только валидные (в исходном порядке).
    """
    results = await validate_emails_parallel(emails, max_concurrent=3)
    return [e for e in emails if results.get(e)]


# ────────────── Главный цикл ──────────────

async def main(dry_run: bool, limit: int | None, manager_id: int | None):
    if dry_run:
        logger.warning("=== DRY RUN — записи в БД НЕ будут произведены ===")

    relatives = await fetch_relatives_to_enrich(limit=limit, manager_id=manager_id)
    if not relatives:
        logger.info("Нет родственников подходящих под критерии (phone пуст + есть ФИО + есть ДР).")
        await close_pool()
        return

    logger.info(f"Найдено родственников для пробива: {len(relatives)}")
    estimated_cost_rub = len(relatives) * 0.02
    logger.info(f"Примерная стоимость: ~{estimated_cost_rub:.2f}₽ "
                f"(Sauron 0.02₽/запрос)")

    if not dry_run:
        confirm = input(f"\nПрогнать через Sauron {len(relatives)} запросов? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            logger.info("Отмена.")
            await close_pool()
            return

    stats = {
        "enriched": 0,
        "found": 0,
        "found_no_phone": 0,
        "not_found": 0,
        "sauron_errors": 0,
        "db_errors": 0,
        "skipped_bad_name": 0,
    }
    log_lines = []

    sem = asyncio.Semaphore(PARALLEL)

    async def worker(rel):
        async with sem:
            await process_one(rel, dry_run, log_lines, stats)

    # Прогресс-индикатор
    total = len(relatives)
    done = 0

    async def progress_worker(rel):
        nonlocal done
        await worker(rel)
        done += 1
        if done % 10 == 0 or done == total:
            logger.info(f"  Прогресс: {done}/{total}")

    tasks = [progress_worker(rel) for rel in relatives]
    await asyncio.gather(*tasks)

    # Сводка
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")

    logger.info("=" * 50)
    logger.info("ИТОГО:")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")
    logger.info(f"Лог сохранён: {LOG_FILE}")

    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Имитация без записи в БД (Sauron всё равно вызывается!)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Обработать только первые N (для теста)")
    parser.add_argument("--manager-id", type=int, default=None,
                        help="Обработать только записи этого менеджера (по id)")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run, limit=args.limit, manager_id=args.manager_id))
