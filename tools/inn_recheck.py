#!/usr/bin/env python3
"""
Бесплатная перепроверка отложенных лидов по реестру ФНС.

Берёт тех, у кого ИНН уже сохранён, но год аннулирования не подошёл или ИНН
был действующим. Саурон не трогаем вообще — ИНН лежит в extra, а ФНС бесплатна.
Если появилась свежая дата, лид переезжает к Соне.

    venv/bin/python -m tools.inn_recheck
    venv/bin/python -m tools.inn_recheck --apply
"""
import argparse
import asyncio
import datetime as dt
import json
import re
from collections import Counter

from bot.db.queries import get_pool
from bot.services import fns_inn
from bot.services.fns_inn import FnsUnavailable, allowed_years, invalid_since

SONYA, OFFICE = 111, "pvl"
PAUSE = 0.4


async def run(apply: bool, limit: int) -> None:
    years = allowed_years()
    if not years:
        raise SystemExit("PVL_INN_YEARS пуст — перепроверять не по чему")
    print(f"рабочие годы: {sorted(years)}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pm.id, pm.full_name, pm.added_by,
                   pm.extra->>'inn' AS inns,
                   pm.extra->>'inn_invalid_date' AS known
            FROM persons_military pm
            WHERE pm.exported_at IS NULL
              AND COALESCE(pm.extra->>'inn', '') <> ''
              AND pm.added_by <> $1
            ORDER BY pm.id
            LIMIT $2
            """, SONYA, limit)

    print(f"к перепроверке: {len(rows)}")
    if not apply:
        print("Сухой прогон. Для записи добавь --apply")
        return

    st = Counter()
    moved = []
    today = dt.date.today()

    for i, r in enumerate(rows, 1):
        inns = [x for x in re.findall(r"\d{12}", r["inns"] or "")]
        dates = {}
        failed = False
        for inn in inns:
            try:
                d = await invalid_since(inn)
            except FnsUnavailable as e:
                st["ФНС недоступна"] += 1
                failed = True
                break
            if d:
                dates[inn] = d
            await asyncio.sleep(PAUSE)
        if failed:
            continue

        best = max(dates.values()) if dates else None
        patch = {
            "inn_checked_at": today.isoformat(),
            "inn_invalid_date": best.isoformat() if best else "",
        }
        good = best is not None and best.year in years

        async with pool.acquire() as conn:
            if good:
                await conn.execute(
                    """
                    UPDATE persons_military
                    SET extra = COALESCE(extra, '{}'::jsonb) || $2::text::jsonb,
                        added_by = $3, office = $4
                    WHERE id = $1
                    """, r["id"], json.dumps(patch, ensure_ascii=False),
                    SONYA, OFFICE)
                st["переехал к Соне"] += 1
                moved.append(f"{r['full_name']} — {best.strftime('%d.%m.%Y')}")
            else:
                await conn.execute(
                    """
                    UPDATE persons_military
                    SET extra = COALESCE(extra, '{}'::jsonb) || $2::text::jsonb
                    WHERE id = $1
                    """, r["id"], json.dumps(patch, ensure_ascii=False))
                st["без изменений"] += 1

        if i % 200 == 0:
            print(f"  {i} из {len(rows)}", flush=True)

    if fns_inn._session and not fns_inn._session.closed:
        await fns_inn._session.close()

    print("\n=== ИТОГ ===")
    for k, v in st.most_common():
        print(f"{v:6d}  {k}")
    for m in moved[:20]:
        print("  →", m)
    if len(moved) > 20:
        print(f"  ... и ещё {len(moved) - 20}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=5000)
    a = p.parse_args()
    asyncio.run(run(a.apply, a.limit))


if __name__ == "__main__":
    main()
