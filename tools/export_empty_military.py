#!/usr/bin/env python3
"""
Выгрузка незаполненных военных (без родственников) для пробива.
Формат строки: "Иванов Иван Иванович 20.05.1985" (ДР как ДД.ММ.ГГГГ).
Разбивка по CHUNK строк на файл (лимит сервиса — 5000).

    venv/bin/python -m tools.export_empty_military            # DRY-RUN: сколько строк/файлов
    venv/bin/python -m tools.export_empty_military --commit   # создать файлы

Фильтр офиса: --office dp|ha|pvl (по умолчанию все три).
Военные без ДР пропускаются (в базе таких сейчас 0).
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from bot.db.connection import get_pool  # noqa: E402

CHUNK = 5000
OUT_DIR = "/root/exports/probiv_empty"


async def fetch_rows(office: str | None):
    pool = await get_pool()
    where = [
        "NOT EXISTS (SELECT 1 FROM military_relatives mr WHERE mr.military_id = pm.id)",
        "pm.birth_date IS NOT NULL",
        "COALESCE(TRIM(pm.full_name),'') <> ''",
    ]
    params = []
    if office:
        params.append(office)
        where.append(f"pm.office = ${len(params)}")
    sql = f"""
        SELECT pm.full_name,
               to_char(pm.birth_date, 'DD.MM.YYYY') AS dr
        FROM persons_military pm
        WHERE {' AND '.join(where)}
        ORDER BY pm.office, pm.id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    # строка "ФИО ДР"
    return [f"{r['full_name'].strip()} {r['dr']}" for r in rows]


async def main(office: str | None, commit: bool):
    lines = await fetch_rows(office)
    total = len(lines)
    files = (total + CHUNK - 1) // CHUNK
    scope = office or "все офисы"

    print(f"Незаполненных военных с ДР ({scope}): {total}")
    print(f"Файлов по {CHUNK}: {files}")
    if total:
        print(f"Пример строки: {lines[0]}")

    if not commit:
        print("\n[DRY-RUN] Файлы не создданы. Для выгрузки: --commit")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for i in range(files):
        chunk = lines[i * CHUNK:(i + 1) * CHUNK]
        path = os.path.join(OUT_DIR, f"probiv_part{i + 1:02d}.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        made.append((path, len(chunk)))

    print(f"\n✅ Создано файлов: {len(made)} в {OUT_DIR}")
    for path, n in made:
        print(f"   {os.path.basename(path)}: {n} строк")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--office", choices=["dp", "ha", "pvl"], default=None)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.office, args.commit))