#!/usr/bin/env python3
"""
Проверка ИНН у ЗАПОЛНЕННЫХ лидов Findre (есть родня хотя бы с одним телефоном).

Подошёл по году — лид и его родня уезжают Соне.
Не подошёл — id пишется в файл, удаление делается отдельным шагом.

    venv/bin/python -m tools.inn_harvest_filled --limit 400
    venv/bin/python -m tools.inn_harvest_filled --limit 400 --apply
"""
import argparse
import asyncio
import datetime as dt
import json

from bot.db.queries import get_pool, insert_probiv_log
from bot.services.fns_inn import extract_inns, invalid_since
from bot.services.sauron_api import query_person, split_full_name

FINDRE, SONYA = 1, 111
OFFICE = "pvl"
GOOD_YEARS = {2025, 2026}
DROPS = "/root/exports/findre_drop_ids.txt"
PAUSE = 1.0

SELECT_SQL = r"""
    SELECT pm.id,
           regexp_replace(pm.full_name, '\s+', ' ', 'g') AS full_name,
           pm.birth_date
    FROM persons_military pm
    WHERE pm.added_by = $1
      AND pm.exported_at IS NULL
      AND pm.birth_date IS NOT NULL
      AND COALESCE(pm.extra->>'inn_checked_at', '') = ''
      AND EXISTS (SELECT 1 FROM military_relatives mr
                    JOIN relatives r ON r.id = mr.relative_id
                   WHERE mr.military_id = pm.id
                     AND COALESCE(r.phone, '') <> '')
    ORDER BY pm.created_at DESC
    LIMIT $2
"""


async def run(limit: int, apply: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(SELECT_SQL, FINDRE, limit)

    work = []
    for r in rows:
        last, first, middle = split_full_name(r["full_name"])
        if last and first:
            work.append((r, last, first, middle))

    print(f"Отобрано {len(rows)}, пригодны к пробиву {len(work)}")
    if not apply:
        print("Сухой прогон. Для записи добавь --apply")
        return

    today = dt.date.today()
    keep, drop, spent, errs = [], [], 0.0, 0

    for i, (r, last, first, middle) in enumerate(work, 1):
        bd = r["birth_date"]
        inns = set()
        try:
            res = await query_person(last, first, middle, day=bd.day,
                                     month=bd.month, year=bd.year, office=OFFICE)
            inns = extract_inns(res.get("response", {}).get("result", []))
            cost = float(res.get("cost") or 0)
            spent += cost
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=cost,
                currency="USD", military_id=r["id"], success=True, office=OFFICE)
        except Exception as e:
            errs += 1
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=0,
                currency="USD", military_id=r["id"], success=False,
                error=str(e), office=OFFICE)
            print(f"  [{i}/{len(work)}] саурон упал id={r['id']}: {e}")
            await asyncio.sleep(PAUSE)
            continue

        dates = {}
        for inn in sorted(inns):
            try:
                d = await invalid_since(inn)
            except Exception as e:
                print(f"  фнс упала {inn}: {e}")
                d = None
            if d:
                dates[inn] = d

        best = max(dates.values()) if dates else None
        good = best is not None and best.year in GOOD_YEARS

        patch = {
            "inn_checked_at": today.isoformat(),
            "inn": ", ".join(sorted(inns)) if inns else "",
            "inn_invalid_date": best.isoformat() if best else "",
        }
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE persons_military
                SET extra = COALESCE(extra, '{}'::jsonb) || $2::text::jsonb
                WHERE id = $1
                """,
                r["id"], json.dumps(patch, ensure_ascii=False))

        (keep if good else drop).append(r["id"])
        print(f"  [{i}/{len(work)}] id={r['id']:<7} инн:{len(inns)} "
              f"дата:{best or '—'} → {'СОНЕ' if good else 'в отсев'}   ${spent:.2f}")
        await asyncio.sleep(PAUSE)

    # Перенос подошедших вместе с роднёй, одной транзакцией.
    # Родственника забираем только если он не висит на лидах вне набора.
    if keep:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE relatives r SET added_by = $1, office = $2
                    WHERE EXISTS (SELECT 1 FROM military_relatives mr
                                   WHERE mr.relative_id = r.id
                                     AND mr.military_id = ANY($3::int[]))
                      AND NOT EXISTS (SELECT 1 FROM military_relatives mr
                                   WHERE mr.relative_id = r.id
                                     AND NOT (mr.military_id = ANY($3::int[])))
                    """,
                    SONYA, OFFICE, keep)
                await conn.execute(
                    "UPDATE persons_military SET added_by = $1, office = $2 "
                    "WHERE id = ANY($3::int[])",
                    SONYA, OFFICE, keep)

    with open(DROPS, "w", encoding="utf-8") as f:
        f.write("\n".join(str(i) for i in drop) + ("\n" if drop else ""))

    print(f"\nСоне: {len(keep)}   в отсев: {len(drop)}   "
          f"ошибок саурона: {errs}   потрачено ${spent:.2f}")
    print(f"Список на удаление: {DROPS}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    asyncio.run(run(a.limit, a.apply))


if __name__ == "__main__":
    main()
