"""
Слияние дубликатов военных в БД.

Логика:
- Находим группы записей с одинаковыми (ФИО + ДР) или (ФИО + ДР IS NULL)
- В каждой группе оставляем САМУЮ РАННЮЮ запись (по created_at)
- Все военные-родственники-связки от других записей перенаправляем на оставшуюся
- Удаляем лишние записи военных
- При наличии конфликта (родственник уже привязан к оставшемуся) — связка не задваивается

Использование:
    venv/bin/python -m tools.merge_military_duplicates --dry-run
    venv/bin/python -m tools.merge_military_duplicates
"""
import asyncio
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.db.connection import get_pool, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("merge_dups")


async def find_duplicate_groups():
    """
    Найти все группы дублей.
    Группируем по (LOWER(TRIM(full_name)), birth_date), но IS NULL тоже считаем равным IS NULL.
    Возвращаем список групп — каждая группа это список dict (id, full_name, birth_date, created_at).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, full_name, birth_date, created_at,
                   LOWER(TRIM(full_name)) AS norm_name
            FROM persons_military
            ORDER BY LOWER(TRIM(full_name)), birth_date NULLS FIRST, created_at
            """
        )

    groups = {}
    for r in rows:
        # ключ — нормализованное ФИО + ДР (либо строка-маркер для NULL)
        key = (r["norm_name"], r["birth_date"] or "__NULL__")
        groups.setdefault(key, []).append(dict(r))

    # Оставляем только группы с >=2 записями
    return [g for g in groups.values() if len(g) >= 2]


async def merge_group(group: list, dry_run: bool, stats: dict):
    """
    Слить группу дублей. group отсортирована по created_at — первый элемент остаётся.
    """
    keeper = group[0]
    others = group[1:]
    keeper_id = keeper["id"]

    other_ids = [o["id"] for o in others]
    name = keeper["full_name"]
    bd = keeper["birth_date"]
    bd_str = bd.strftime("%d.%m.%Y") if bd else "—"

    logger.info(f"Слияние: {name} ({bd_str}) — оставляем #{keeper_id}, удаляем {other_ids}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Переподвязать m2m связки родственников: military_id из other_ids → keeper_id
            # Возможны конфликты (родственник уже у keeper) — UPDATE их не тронет благодаря UNIQUE.
            # Для каждой связки делаем INSERT ... ON CONFLICT DO NOTHING с keeper_id,
            # потом удаляем старые связки.

            # Сначала — переподвязать
            relinked = await conn.fetchval(
                """
                WITH moved AS (
                    INSERT INTO military_relatives (military_id, relative_id, added_by, created_at)
                    SELECT $1, mr.relative_id, mr.added_by, mr.created_at
                    FROM military_relatives mr
                    WHERE mr.military_id = ANY($2::int[])
                    ON CONFLICT (military_id, relative_id) DO NOTHING
                    RETURNING 1
                )
                SELECT COUNT(*) FROM moved
                """,
                keeper_id, other_ids
            ) or 0

            # Удаляем старые связки (они теперь либо переехали на keeper, либо были дублями)
            removed_links = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM military_relatives
                    WHERE military_id = ANY($1::int[])
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                other_ids
            ) or 0

            # 2. Удаляем лишних военных
            removed_military = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM persons_military
                    WHERE id = ANY($1::int[])
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                other_ids
            ) or 0

            if dry_run:
                # Откатываем транзакцию
                raise _DryRunRollback(f"  → relinked={relinked}, removed_links={removed_links}, removed_military={removed_military}")

            stats["military_removed"] += removed_military
            stats["links_relinked"] += relinked
            stats["links_removed"] += removed_links
            stats["groups_merged"] += 1

            logger.info(f"  → relinked={relinked}, removed_links={removed_links}, removed_military={removed_military}")


class _DryRunRollback(Exception):
    """Спец-исключение для отката транзакции в dry-run режиме"""
    pass


async def main(dry_run: bool):
    if dry_run:
        logger.warning("=== DRY RUN — изменения откатываются ===")

    groups = await find_duplicate_groups()
    if not groups:
        logger.info("Дубли не найдены, делать нечего.")
        await close_pool()
        return

    logger.info(f"Найдено групп дублей: {len(groups)}")
    total_records = sum(len(g) for g in groups)
    logger.info(f"Всего записей в дублях: {total_records}, будет удалено: {total_records - len(groups)}")

    if not dry_run:
        confirm = input("\nПродолжаем слияние? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            logger.info("Отмена.")
            await close_pool()
            return

    stats = {
        "groups_merged": 0,
        "military_removed": 0,
        "links_relinked": 0,
        "links_removed": 0,
    }

    for group in groups:
        try:
            await merge_group(group, dry_run, stats)
        except _DryRunRollback as info:
            logger.info(str(info))
            continue
        except Exception as e:
            logger.error(f"Ошибка при слиянии группы {[r['id'] for r in group]}: {e}")
            continue

    logger.info("=" * 50)
    logger.info("ИТОГО:")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")

    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Имитация без записи в БД")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))
