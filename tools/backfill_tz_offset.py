#!/usr/bin/env python3
"""
Разовый бэкфилл: проставить extra.tz_offset существующим relatives,
у которых есть extra.region, но нет tz_offset.

Обновляет ПАЧКАМИ ПО РЕГИОНУ (уникальных регионов ~220, а не 142k строк),
поэтому проходит за десятки секунд, а не часы.

    venv/bin/python -m tools.backfill_tz_offset            # DRY-RUN: что и сколько обновится
    venv/bin/python -m tools.backfill_tz_offset --commit   # выполнить

Нераспознанные регионы (нет в справочнике) пропускаются — tz_offset остаётся пустым (Q4).
"""
import argparse
import asyncio

from dotenv import load_dotenv
load_dotenv()

from bot.db.connection import get_pool                        # noqa: E402
from bot.services.tz_regions import region_to_msk_offset      # noqa: E402


async def collect():
    """Уникальные регионы без tz_offset + сколько строк на каждом."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT extra->>'region' AS region, COUNT(*) AS cnt
            FROM relatives
            WHERE extra ? 'region'
              AND COALESCE(extra->>'region','') <> ''
              AND NOT (extra ? 'tz_offset')
            GROUP BY extra->>'region'
            ORDER BY cnt DESC
            """
        )
    return [(r["region"], r["cnt"]) for r in rows]


async def backfill(commit: bool):
    data = await collect()
    if not data:
        print("Нечего бэкфиллить — все записи с region уже имеют tz_offset.")
        return

    planned, skipped = [], []
    for region, cnt in data:
        off = region_to_msk_offset(region)
        (planned if off else skipped).append((region, cnt, off))

    rows_planned = sum(c for _, c, _ in planned)
    rows_skipped = sum(c for _, c, _ in skipped)

    print(f"Регионов без tz_offset: {len(data)}  (строк: {rows_planned + rows_skipped})")
    print(f"  распознано: {len(planned)} регионов → {rows_planned} строк будет обновлено")
    print(f"  не распознано: {len(skipped)} регионов → {rows_skipped} строк останутся пустыми")

    if skipped:
        print("\nНе распознаны (tz_offset останется пустым):")
        for region, cnt, _ in skipped:
            print(f"    {cnt:>6}  {region}")

    print("\nТоп-10 к обновлению:")
    for region, cnt, off in planned[:10]:
        print(f"    {cnt:>6}  {region:<45} -> {off}")

    if not commit:
        print("\n[DRY-RUN] Ничего не изменено. Для выполнения: --commit")
        return

    pool = await get_pool()
    total = 0
    async with pool.acquire() as conn:
        for i, (region, cnt, off) in enumerate(planned, 1):
            res = await conn.execute(
                """
                UPDATE relatives
                SET extra = extra || jsonb_build_object('tz_offset', $1::text)
                WHERE extra->>'region' = $2::text
                  AND NOT (extra ? 'tz_offset')
                """,
                off, region,
            )
            n = int(res.split()[-1])
            total += n
            if i % 20 == 0 or n > 1000:
                print(f"  [{i}/{len(planned)}] {region} -> {off}: {n} строк (всего {total})")

    print(f"\n✅ Готово. Обновлено строк: {total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="выполнить обновление (иначе dry-run)")
    args = ap.parse_args()
    asyncio.run(backfill(args.commit))