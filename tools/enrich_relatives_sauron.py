"""
Разовый скрипт: обогащение родственников партии sauron_links_20260817 через Sauron.

По каждому родственнику без телефона:
  query_person(ФИО+ДР, office='pvl') -> build_relative_template ->
  пишем phone (самый частый), address, snils/inn/passport в БД.

Идемпотентность: берём только тех, у кого phone пустой. Прерывание безопасно —
при повторном запуске продолжит с необработанных.

Запуск:
  cd /root/projects/tg-parser-bot
  venv/bin/python -m tools.enrich_relatives_sauron          # dry-run (10 шт, без записи)
  venv/bin/python -m tools.enrich_relatives_sauron --apply  # боевой, все
  venv/bin/python -m tools.enrich_relatives_sauron --apply --limit 100  # частями
"""
import asyncio
import logging
import argparse
from pathlib import Path

from bot.db.connection import get_pool
from bot.services.sauron_api import query_person, SauronError
from bot.parser import sauron_parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("enrich_relatives")

SOURCE = "sauron_links_20261015"
OFFICE = "pvl"          # родня загружена в pvl, токен SAURON_TOKEN_PVL
CONCURRENCY = 3         # параллельных запросов к Sauron
PAUSE_BETWEEN = 0.3     # пауза между запросами (сек), бережём rate-limit


def _split_fio(full_name: str):
    parts = (full_name or "").split()
    last = parts[0] if len(parts) > 0 else ""
    first = parts[1] if len(parts) > 1 else ""
    middle = parts[2] if len(parts) > 2 else None
    return last, first, middle


async def _fetch_pending(limit: int | None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        q = """
            SELECT id, full_name, birth_date
            FROM relatives
            WHERE extra->>'source' = $1
              AND (phone IS NULL OR phone = '')
            ORDER BY id
        """
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = await conn.fetch(q, SOURCE)
        return [dict(r) for r in rows]


async def _write_result(rel_id: int, template: dict):
    """Записать phone/address/snils/inn/passport в БД."""
    phone = template.get("phone")
    address = template.get("address")
    snils = template.get("snils")
    inn = template.get("inn")
    passport = template.get("passport")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # phone/address — в колонки; документы — в extra (их нет отдельными колонками)
        await conn.execute(
            """
            UPDATE relatives
            SET phone = COALESCE($2, phone),
                address = COALESCE($3, address),
                extra = extra
                        || CASE WHEN $4::text IS NOT NULL THEN jsonb_build_object('snils',$4) ELSE '{}'::jsonb END
                        || CASE WHEN $5::text IS NOT NULL THEN jsonb_build_object('inn',$5) ELSE '{}'::jsonb END
                        || CASE WHEN $6::text IS NOT NULL THEN jsonb_build_object('passport',$6) ELSE '{}'::jsonb END,
                updated_at = NOW()
            WHERE id = $1
            """,
            rel_id, phone, address, snils, inn, passport,
        )


async def _process_one(rel: dict, sem: asyncio.Semaphore, stats: dict, apply: bool):
    async with sem:
        rel_id = rel["id"]
        last, first, middle = _split_fio(rel["full_name"])
        bd = rel["birth_date"]
        day = month = year = None
        if bd:
            day, month, year = bd.day, bd.month, bd.year
        try:
            result = await query_person(
                lastname=last, firstname=first, middlename=middle,
                day=day, month=month, year=year, office=OFFICE,
            )
        except SauronError as e:
            logger.warning(f"[#{rel_id}] {rel['full_name']}: Sauron error — {e}")
            stats["errors"] += 1
            return
        except Exception as e:
            logger.warning(f"[#{rel_id}] {rel['full_name']}: ошибка — {e}")
            stats["errors"] += 1
            return

        template = sauron_parser.build_relative_template(result)
        if not template or not (template.get("phone") or template.get("address")):
            stats["empty"] += 1
            logger.info(f"[#{rel_id}] {rel['full_name']}: пусто (нет данных)")
            await asyncio.sleep(PAUSE_BETWEEN)
            return

        if apply:
            await _write_result(rel_id, template)
        stats["updated"] += 1
        logger.info(
            f"[#{rel_id}] {rel['full_name']}: "
            f"phone={template.get('phone') or '—'} "
            f"addr={'да' if template.get('address') else '—'} "
            f"снилс={'да' if template.get('snils') else '—'} "
            f"инн={'да' if template.get('inn') else '—'} "
            f"пасп={'да' if template.get('passport') else '—'}"
            + ("" if apply else "  [DRY]")
        )
        await asyncio.sleep(PAUSE_BETWEEN)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="боевой запуск (иначе dry-run 10 шт)")
    ap.add_argument("--limit", type=int, default=None, help="ограничить число")
    args = ap.parse_args()

    limit = args.limit if args.apply else (args.limit or 10)
    pending = await _fetch_pending(limit)
    logger.info(f"К обработке: {len(pending)} родственников "
                f"({'БОЕВОЙ' if args.apply else 'DRY-RUN'})")
    if not pending:
        logger.info("Нет родственников без телефона — всё обработано.")
        return

    stats = {"updated": 0, "empty": 0, "errors": 0}
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*[_process_one(r, sem, stats, args.apply) for r in pending])

    logger.info("═" * 50)
    logger.info(f"ИТОГО: обновлено={stats['updated']}, пусто={stats['empty']}, "
                f"ошибок={stats['errors']}, всего={len(pending)}")
    if not args.apply:
        logger.info("Это был DRY-RUN. Для записи: --apply")


if __name__ == "__main__":
    asyncio.run(main())
