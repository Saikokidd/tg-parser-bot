"""
Одноразовая утилита: backfill номеров из legacy relatives → relative_phones,
чтобы прогнать их через HLR-проверку.

По умолчанию обрабатывает родственников офиса pvl, созданных за вчера
(или указанная дата). Берёт legacy relatives.phone и extra.operator,
создаёт по одной записи в relative_phones (is_primary=true), которая
потом подхватится hlr_enricher и hlr_poller.

Фильтр:
  - office = 'pvl'
  - created_at в указанный диапазон
  - phone не NULL и не пустой
  - extra.operator не NULL и не пустой
  - оператор НЕ в скип-листе (MEGAFON / YOTA в любом регистре и кириллице)
  - в relative_phones нет записи с таким же phone_last10 (любой родственник)

Использование:
    venv/bin/python -m tools.backfill_relative_phones_for_hlr --dry-run
    venv/bin/python -m tools.backfill_relative_phones_for_hlr --date 2026-06-25
    venv/bin/python -m tools.backfill_relative_phones_for_hlr --date 2026-06-25 --apply
"""
import asyncio
import argparse
import logging
from datetime import date, datetime, timedelta

from bot.db.connection import get_pool, close_pool


SKIP_OPERATORS_UPPER = {"MEGAFON", "MEGAFONE", "YOTA", "МЕГАФОН", "ЙОТА"}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def fetch_candidates(target_date: date, limit: int | None = None) -> list[dict]:
    """
    Кандидаты для backfill: родственники pvl,
    созданные в указанную дату, с phone и operator.
    """
    pool = await get_pool()
    start = datetime.combine(target_date, datetime.min.time())
    end = start + timedelta(days=1)
    
    sql = """
        SELECT r.id, r.phone, r.extra, r.office, r.created_at
        FROM relatives r
        WHERE r.office = 'pvl'
          AND r.created_at >= $1
          AND r.created_at < $2
          AND r.phone IS NOT NULL AND r.phone != ''
          AND (r.extra->>'operator') IS NOT NULL
          AND (r.extra->>'operator') != ''
        ORDER BY r.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, start, end)
        return [dict(r) for r in rows]


async def phone_already_in_relative_phones(phone: str) -> bool:
    """Проверка: есть ли уже такой phone (по последним 10 цифрам) в relative_phones."""
    pool = await get_pool()
    only_digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    last10 = only_digits[-10:] if only_digits else ""
    if not last10:
        return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM relative_phones WHERE phone_last10(phone::text) = $1 LIMIT 1",
            last10,
        )
        return row is not None


async def insert_phone(relative_id: int, phone: str, operator: str) -> bool:
    """Вставка записи в relative_phones. False — если конфликт UNIQUE."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                """
                INSERT INTO relative_phones
                    (relative_id, phone, operator, operator_checked_at,
                     is_primary, source_frequency)
                VALUES ($1, $2, $3, NOW(), TRUE, 1)
                """,
                relative_id, phone, operator,
            )
            return result.endswith(" 1")
        except Exception as e:
            logger.warning(f"insert failed for rel_id={relative_id}: {e}")
            return False


async def main(target_date: date, apply: bool, limit: int | None = None):
    candidates = await fetch_candidates(target_date, limit=limit)
    logger.info(f"Кандидатов: {len(candidates)} (office=pvl, created_at={target_date})")

    stats = {
        "total": len(candidates),
        "skip_operator": 0,
        "skip_dup": 0,
        "would_insert": 0,
        "inserted": 0,
        "errors": 0,
    }

    for cand in candidates:
        rel_id = cand["id"]
        phone = cand["phone"]
        extra = cand.get("extra") or {}
        operator = (extra.get("operator") or "").strip()

        # Skip if operator in skip-list
        if operator.upper() in SKIP_OPERATORS_UPPER:
            stats["skip_operator"] += 1
            continue

        # Skip if phone already in relative_phones (любой родственник)
        if await phone_already_in_relative_phones(phone):
            stats["skip_dup"] += 1
            continue

        stats["would_insert"] += 1

        if apply:
            ok = await insert_phone(rel_id, phone, operator)
            if ok:
                stats["inserted"] += 1
            else:
                stats["errors"] += 1

    logger.info("─" * 60)
    logger.info(f"Total candidates:     {stats['total']}")
    logger.info(f"Skipped (operator):   {stats['skip_operator']}")
    logger.info(f"Skipped (dup phone):  {stats['skip_dup']}")
    logger.info(f"Would insert:         {stats['would_insert']}")
    if apply:
        logger.info(f"Inserted:             {stats['inserted']}")
        logger.info(f"Errors:               {stats['errors']}")
    else:
        logger.info(f"(dry-run — для записи добавьте --apply)")
    logger.info("─" * 60)
    if apply and stats["inserted"] > 0:
        logger.info(
            f"hlr_enricher подхватит {stats['inserted']} новых записей "
            f"в следующем прогоне (раз в минуту)"
        )


async def _run_all(target_date: date, apply: bool, limit: int | None = None):
    try:
        await main(target_date, apply=apply, limit=limit)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Дата в формате YYYY-MM-DD (default: сегодня)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально записать в БД (без флага — dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Максимальное число записей (для экономии бюджета)",
    )
    args = parser.parse_args()
    target_date = date.fromisoformat(args.date)
    asyncio.run(_run_all(target_date, apply=args.apply, limit=args.limit))
