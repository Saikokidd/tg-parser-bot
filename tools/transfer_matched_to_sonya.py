#!/usr/bin/env python3
"""
Шаг 1: перенос военных со связями (из sauron-файла) менеджеру Соня (id=111).

Матч по нормализованному ФИО+ДР (trim, lower, ё->е, срез концевых '.,;').
- найдено ровно 1  -> переносим: added_by=111, office='pvl', created_at=NOW(),
                       маркер extra.moved={from_manager, from_office, at, batch}
- несколько (дубли) -> НЕ трогаем, пишем в файл deferred_duplicates.csv
- не найдено        -> пишем в файл not_found.csv

    venv/bin/python -m tools.transfer_matched_to_sonya /root/exports/svyazi_est_2186.xlsx            # DRY-RUN
    venv/bin/python -m tools.transfer_matched_to_sonya /root/exports/svyazi_est_2186.xlsx --commit   # выполнить
"""
import argparse
import asyncio
import csv
import re
from datetime import date, datetime

from openpyxl import load_workbook
from dotenv import load_dotenv
load_dotenv()

from bot.db.connection import get_pool  # noqa: E402

SONYA = 111
BATCH = "sauron_links_20260720"
OUT_DIR = "/root/exports"


def norm(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е").rstrip(" .,;")
    return re.sub(r"\s+", " ", s)


def read_subjects(path: str):
    ws = load_workbook(path, read_only=True).active
    seen = {}
    for r in list(ws.iter_rows(values_only=True))[1:]:
        fio = str(r[1]).strip() if r[1] else ""
        dr = str(r[2]).strip() if r[2] else ""
        if fio and dr:
            seen.setdefault((fio, dr), True)
    return list(seen.keys())


async def resolve(conn, fio, dr):
    """Вернуть (status, rows). status: 'one' | 'many' | 'none' | 'baddr'."""
    try:
        d, m, y = dr.split(".")
        bd = date(int(y), int(m), int(d))
    except Exception:
        return "baddr", []
    rows = await conn.fetch(
        """
        SELECT id, office, added_by FROM persons_military
        WHERE RTRIM(LOWER(REPLACE(full_name,'ё','е')), ' .,;') = $1
          AND birth_date = $2
        """,
        norm(fio), bd,
    )
    if len(rows) == 1:
        return "one", rows
    if len(rows) > 1:
        return "many", rows
    return "none", rows


async def main(path: str, commit: bool):
    subjects = read_subjects(path)
    print(f"Военных со связями в файле: {len(subjects)}")

    pool = await get_pool()
    to_move = []          # (id, fio, dr, from_office, from_added_by)
    dups, none = [], []

    async with pool.acquire() as conn:
        for fio, dr in subjects:
            status, rows = await resolve(conn, fio, dr)
            if status == "one":
                r = rows[0]
                to_move.append((r["id"], fio, dr, r["office"], r["added_by"]))
            elif status == "many":
                dups.append((fio, dr, len(rows), ",".join(str(x["id"]) for x in rows)))
            else:
                none.append((fio, dr, status))

    print(f"  к переносу (уникальные): {len(to_move)}")
    print(f"  отложено — дубли:        {len(dups)}")
    print(f"  отложено — не найдено:   {len(none)}")

    # всегда выгружаем отложенные списки, чтобы были на руках
    with open(f"{OUT_DIR}/deferred_duplicates.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["fio", "dr", "count", "ids"])
        w.writerows(dups)
    with open(f"{OUT_DIR}/deferred_notfound.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["fio", "dr", "reason"])
        w.writerows(none)
    print(f"\nОтложенные выгружены: {OUT_DIR}/deferred_duplicates.csv, deferred_notfound.csv")

    # разбивка «у кого забираем»
    from collections import Counter
    by_mgr = Counter(x[4] for x in to_move)
    print("\nУ кого забираем (added_by -> сколько):")
    for mgr, n in by_mgr.most_common(15):
        print(f"    mgr={mgr}: {n}")

    if not commit:
        print("\n[DRY-RUN] Ничего не перенесено. Для выполнения добавь --commit")
        return

    ids = [x[0] for x in to_move]
    at = datetime.now().isoformat(timespec="seconds")
    moved = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for mil_id, fio, dr, from_office, from_added_by in to_move:
                res = await conn.execute(
                    """
                    UPDATE persons_military
                    SET added_by = $1,
                        office = 'pvl',
                        created_at = NOW(),
                        extra = COALESCE(extra,'{}'::jsonb) || jsonb_build_object(
                                  'moved', jsonb_build_object(
                                     'from_office',  office,
                                     'from_manager', added_by,
                                     'at', $2::text,
                                     'batch', $3::text))
                    WHERE id = $4
                    """,
                    SONYA, at, BATCH, mil_id,
                )
                moved += 1 if res.endswith(" 1") else 0
    print(f"\n✅ Перенесено к Соне: {moved} военных (batch={BATCH})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.path, args.commit))