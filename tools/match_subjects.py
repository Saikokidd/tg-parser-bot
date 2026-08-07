#!/usr/bin/env python3
"""
Матч-тест (READ-ONLY): сопоставить военных со связями из sauron-файла
с persons_military по нормализованному ФИО + ДР.

Нормализация: trim, lower, ё->е, схлопывание пробелов — чтобы
'Налётов'/'налетов' и разный регистр не расходились.

    venv/bin/python -m tools.match_subjects /root/exports/svyazi_est_2186.xlsx

Выводит: найдено уникально / не найдено / несколько совпадений (+ примеры).
Ничего не пишет в БД.
"""
import argparse
import asyncio
import re
import sys

from datetime import date
from openpyxl import load_workbook
from dotenv import load_dotenv
load_dotenv()

from bot.db.connection import get_pool  # noqa: E402


def norm(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е")
    s = s.rstrip(" .,;")          # мусорные точки/пунктуация в конце ФИО
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


async def main(path: str):
    subjects = read_subjects(path)
    print(f"Военных со связями в файле: {len(subjects)}")

    pool = await get_pool()
    found_one, found_many, not_found = 0, [], []
    async with pool.acquire() as conn:
        for fio, dr in subjects:
            # ДР из файла ДД.ММ.ГГГГ -> date
            try:
                d, m, y = dr.split(".")
                bd = date(int(y), int(m), int(d))
            except Exception:
                not_found.append((fio, dr, "плохая ДР"))
                continue
            rows = await conn.fetch(
                """
                SELECT id, office, added_by FROM persons_military
                WHERE RTRIM(LOWER(REPLACE(full_name,'ё','е')), ' .,;') = $1
                  AND birth_date = $2
                """,
                norm(fio), bd,
            )
            if len(rows) == 1:
                found_one += 1
            elif len(rows) > 1:
                found_many.append((fio, dr, len(rows)))
            else:
                not_found.append((fio, dr, "нет в БД"))

    print(f"\n  найдено ровно 1:   {found_one}")
    print(f"  несколько совпадений: {len(found_many)}")
    print(f"  не найдено:        {len(not_found)}")

    if found_many:
        print("\nПримеры 'несколько совпадений' (первые 10):")
        for fio, dr, n in found_many[:10]:
            print(f"    {fio} {dr} -> {n} записей")
    if not_found:
        print("\nПримеры 'не найдено' (первые 15):")
        for fio, dr, why in not_found[:15]:
            print(f"    {fio} {dr} [{why}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="путь к svyazi_est_*.xlsx на сервере")
    args = ap.parse_args()
    asyncio.run(main(args.path))