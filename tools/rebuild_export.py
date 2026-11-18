#!/usr/bin/env python3
"""
Пересобрать xlsx по уже помеченной партии выгрузки.

Нужен, когда бот пометил лиды выгруженными, а файл до менеджера не дошёл:
пометка ставится до отправки, и байты теряются безвозвратно.

    venv/bin/python -m tools.rebuild_export 5            # показать партии
    venv/bin/python -m tools.rebuild_export 5 "2026-11-18 13:56:29"
"""
import asyncio
import datetime as dt
import sys

from bot.db.queries import (fetch_relatives_for_military_ids, get_pool)
from bot.services.export_service import build_xlsx, make_filename


async def main():
    manager_id = int(sys.argv[1])
    stamp = sys.argv[2] if len(sys.argv) > 2 else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        if not stamp:
            rows = await conn.fetch("""
                SELECT exported_at, COUNT(*) AS n
                FROM persons_military
                WHERE added_by = $1 AND exported_at >= now() - interval '7 days'
                GROUP BY 1 ORDER BY 1 DESC
            """, manager_id)
            for r in rows:
                print(f"  {r['exported_at']}  {r['n']} лидов")
            return

        name = await conn.fetchval("SELECT name FROM managers WHERE id = $1",
                                   manager_id)
        military = await conn.fetch("""
            SELECT pm.*, m.name AS manager_name, s.name AS source_name
            FROM persons_military pm
            LEFT JOIN managers m ON pm.added_by = m.id
            LEFT JOIN sources s ON s.id = pm.source_id
            WHERE pm.added_by = $1 AND pm.exported_at = $2::timestamp
            ORDER BY pm.created_at DESC
        """, manager_id, dt.datetime.fromisoformat(stamp))

    military = [dict(r) for r in military]
    if not military:
        sys.exit("партия не найдена")
    rels = await fetch_relatives_for_military_ids([m["id"] for m in military])

    data = await asyncio.to_thread(build_xlsx, military, rels, manager_label=name)
    tag = stamp[11:19].replace(":", "")
    path = "/root/exports/%s_%s" % (tag, make_filename(name))
    with open(path, "wb") as f:
        f.write(data)
    print(f"{path}\n  лидов {len(military)}, родственников {len(rels)}")


asyncio.run(main())
