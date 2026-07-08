#!/usr/bin/env python3
"""
Разовая выгрузка: 20 САМЫХ СВЕЖИХ заполненных лидов менеджера (по умолчанию Соня, id=111).

Штатная кнопка выгружает по FIFO (created_at ASC) — старые первыми.
Здесь наоборот: created_at DESC — свежие первыми. Всё остальное как у штатной
выгрузки: те же фильтры (не выгружено + есть родственники), тот же build_xlsx,
та же пометка exported_at через mark_military_exported.

Использование (на сервере, из корня проекта):
    venv/bin/python tools/export_sonya_latest.py            # DRY-RUN: только покажет 20 id/дат
    venv/bin/python tools/export_sonya_latest.py --commit   # соберёт файл + пометит выгруженными

Параметры по умолчанию: manager_id=111 (Соня), limit=20. Меняются флагами --manager / --limit.
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv

# load_dotenv ДО импорта bot.* — иначе прокси/DSN не подхватятся (как в main.py)
load_dotenv()

from bot.db.connection import get_pool                       # noqa: E402
from bot.db.queries import (                                 # noqa: E402
    fetch_relatives_for_military_ids,
    mark_military_exported,
)
from bot.services.export_service import build_xlsx, make_filename  # noqa: E402

EXPORT_DIR = "/root/exports"


async def fetch_newest_for_export(manager_id: int, limit: int) -> list:
    """Как fetch_military_for_export, но ORDER BY created_at DESC (свежие первыми)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pm.*, m.name AS manager_name, s.name AS source_name
            FROM persons_military pm
            LEFT JOIN managers m ON pm.added_by = m.id
            LEFT JOIN sources  s ON s.id = pm.source_id
            WHERE pm.exported_at IS NULL
              AND pm.added_by = $1
              AND EXISTS (SELECT 1 FROM military_relatives mr WHERE mr.military_id = pm.id)
            ORDER BY pm.created_at DESC
            LIMIT $2
            """,
            manager_id, limit,
        )
    return [dict(r) for r in rows]


async def main(manager_id: int, limit: int, label: str, commit: bool):
    records = await fetch_newest_for_export(manager_id, limit)

    if not records:
        print("📭 Нет подходящих лидов (не выгруженных, с родственниками).")
        return

    print(f"Выбрано {len(records)} самых свежих лидов менеджера {label} (id={manager_id}):")
    print(f"{'id':>8}  {'created_at':<19}  ФИО")
    for r in records:
        print(f"{r['id']:>8}  {str(r['created_at'])[:19]:<19}  {r.get('full_name')}")

    military_ids = [r["id"] for r in records]

    if not commit:
        print("\n[DRY-RUN] Файл НЕ создан, exported_at НЕ проставлен.")
        print("Если список верный — перезапусти с флагом --commit.")
        return

    relatives = await fetch_relatives_for_military_ids(military_ids)
    xlsx_bytes = build_xlsx(records, relatives, manager_label=label)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    filename = make_filename(label)
    path = os.path.join(EXPORT_DIR, filename)
    with open(path, "wb") as f:
        f.write(xlsx_bytes)

    # Пометка выгруженными — как штатная выгрузка (чтобы повторно не выгрузились)
    await mark_military_exported(military_ids)

    print(f"\n✅ Готово. Военных: {len(records)}, родственников: {len(relatives)}.")
    print(f"Файл: {path}")
    print(f"Помечено выгруженными (exported_at): {len(military_ids)} лидов.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manager", type=int, default=111, help="manager_id (по умолчанию Соня=111)")
    ap.add_argument("--limit", type=int, default=20, help="сколько свежих лидов (по умолчанию 20)")
    ap.add_argument("--label", type=str, default="Соня", help="метка в имени файла")
    ap.add_argument("--commit", action="store_true", help="создать файл и пометить выгруженными")
    args = ap.parse_args()
    asyncio.run(main(args.manager, args.limit, args.label, args.commit))