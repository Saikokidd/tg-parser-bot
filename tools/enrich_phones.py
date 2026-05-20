"""
Разовый скрипт: обогащение номеров телефонов родственников через voxlink.ru.

Берёт ВСЕХ родственников с непустым phone, проходит через voxlink,
записывает operator и region в JSONB extra.

Использование:
    cd ~/projects/tg-parser-bot
    venv/bin/python -m tools.enrich_phones

Если voxlink вернул None для номера — пропускаем, ничего не записываем.

Rate limit: 10 запросов/сек у voxlink. Делаем чуть медленнее (8/сек) на всякий случай.
"""
import asyncio
import sys
import logging
import argparse
from pathlib import Path

# Добавляем корень проекта в sys.path чтобы работали импорты bot.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db.connection import get_pool, close_pool
from bot.services.voxlink_service import lookup_phone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("enrich_phones")


# Rate limit: voxlink заявляет 10 RPS, но на практике 429 при 8.
# Делаем 4 RPS — медленнее, но без потерь.
MAX_RPS = 4


async def fetch_relatives_with_phone(only_missing: bool = False):
    """
    Все родственники у которых есть phone.
    only_missing=True → только без operator/region в extra (для повторных прогонов).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if only_missing:
            rows = await conn.fetch(
                """
                SELECT id, full_name, phone, extra
                FROM relatives
                WHERE phone IS NOT NULL AND phone != ''
                  AND (extra->>'operator' IS NULL OR extra->>'region' IS NULL)
                ORDER BY id
                """
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, full_name, phone, extra
                FROM relatives
                WHERE phone IS NOT NULL AND phone != ''
                ORDER BY id
                """
            )
        return [dict(r) for r in rows]


async def update_relative_phone_info(relative_id: int, operator: str, region: str):
    """Записать operator и region в extra JSONB"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # jsonb_set для каждого ключа
        await conn.execute(
            """
            UPDATE relatives
            SET extra = jsonb_set(
                jsonb_set(
                    COALESCE(extra, '{}'::jsonb),
                    '{operator}', to_jsonb($2::text)
                ),
                '{region}', to_jsonb($3::text)
            ),
            updated_at = NOW()
            WHERE id = $1
            """,
            relative_id, operator, region
        )


async def process_one(rel: dict, sem: asyncio.Semaphore, stats: dict):
    """Обработать одного родственника"""
    async with sem:
        # Поддерживаем rate limit: 1 / MAX_RPS секунд между стартами
        await asyncio.sleep(1 / MAX_RPS)

        info = await lookup_phone(rel["phone"])

        if not info:
            stats["skipped"] += 1
            logger.info(f"  [{rel['id']}] {rel['full_name']:40} {rel['phone']:15} → skip (no data)")
            return

        operator = info.get("operator")
        region = info.get("region")
        if not operator and not region:
            stats["skipped"] += 1
            return

        try:
            await update_relative_phone_info(rel["id"], operator or "", region or "")
            stats["updated"] += 1
            logger.info(
                f"  [{rel['id']}] {rel['full_name']:40} {rel['phone']:15} → "
                f"{operator}, {region}"
            )
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"  [{rel['id']}] {rel['full_name']} → DB error: {e}")


async def main(limit: int | None, auto_confirm: bool):
    logger.info("=" * 60)
    logger.info("ОБОГАЩЕНИЕ НОМЕРОВ ТЕЛЕФОНОВ через voxlink.ru")
    if limit:
        logger.info(f"РЕЖИМ ТЕСТА — обработаем только первые {limit}")
    logger.info("=" * 60)

    relatives = await fetch_relatives_with_phone(only_missing=args.only_missing if 'args' in dir() else True)
    total_in_db = len(relatives)
    logger.info(f"Найдено родственников с телефоном: {total_in_db}")

    if limit:
        relatives = relatives[:limit]

    total = len(relatives)
    if total == 0:
        logger.info("Нечего обрабатывать. Выходим.")
        await close_pool()
        return

    if not auto_confirm:
        confirm = input(f"\nОбработать {total} номеров? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            logger.info("Отмена.")
            await close_pool()
            return

    stats = {"updated": 0, "skipped": 0, "errors": 0}
    sem = asyncio.Semaphore(2)

    tasks = [process_one(rel, sem, stats) for rel in relatives]
    await asyncio.gather(*tasks)

    logger.info("=" * 60)
    logger.info(f"ИТОГО:")
    logger.info(f"  Всего:     {total}")
    logger.info(f"  Обновлено: {stats['updated']}")
    logger.info(f"  Пропущено: {stats['skipped']}")
    logger.info(f"  Ошибок:    {stats['errors']}")
    logger.info("=" * 60)

    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Обогащение номеров через voxlink")
    parser.add_argument("--limit", type=int, default=None,
                        help="Обработать только первые N записей (для теста)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Не спрашивать подтверждение")
    parser.add_argument("--only-missing", action="store_true", default=True,
                        help="Только те у кого ещё нет operator/region (по умолчанию True)")
    parser.add_argument("--all", action="store_true",
                        help="Обрабатывать всех, даже уже обогащённых (перезапись)")
    args = parser.parse_args()
    if args.all:
        args.only_missing = False

    asyncio.run(main(limit=args.limit, auto_confirm=args.yes))    



