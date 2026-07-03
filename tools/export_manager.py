"""
Утилита: выгрузить готовых не-выгруженных лидов конкретного менеджера
в xlsx (тот же формат что штатный бот), опционально пометить как выгруженные.

Использует те же функции что и хендлер export.py, чтобы файл был идентичен.

Примеры:
    # Dry-run (только показать сколько будет выгружено)
    venv/bin/python -m tools.export_manager --manager-id 65
    
    # Реальная выгрузка + пометка
    venv/bin/python -m tools.export_manager --manager-id 65 --apply
    
    # Выгрузка в конкретный файл
    venv/bin/python -m tools.export_manager --manager-id 65 --apply --out /root/exports/valeria.xlsx
"""
import asyncio
import argparse
import logging
from pathlib import Path

from bot.db.connection import get_pool, close_pool
from bot.db.queries import (
    fetch_military_for_export,
    fetch_relatives_for_military_ids,
    mark_military_exported,
    get_manager_by_id,
)
from bot.services.export_service import build_xlsx, make_filename


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main(manager_id: int, apply: bool, out_path: Path | None):
    mgr = await get_manager_by_id(manager_id)
    if not mgr:
        logger.error(f"Менеджер id={manager_id} не найден в БД")
        return 1
    logger.info(f"Менеджер: {mgr['name']} ({mgr['office']}), id={mgr['id']}")

    military_records = await fetch_military_for_export(manager_id=manager_id)
    total = len(military_records)
    logger.info(f"Готовых не-выгруженных лидов: {total}")

    if total == 0:
        logger.info("Нечего выгружать.")
        return 0

    military_ids = [m["id"] for m in military_records]
    relatives = await fetch_relatives_for_military_ids(military_ids)
    logger.info(f"Родственников привязано: {len(relatives)}")

    if not apply:
        logger.info("─" * 60)
        logger.info(f"DRY-RUN: было бы выгружено {total} лидов, "
                    f"{len(relatives)} родственников")
        logger.info(f"Для реальной выгрузки добавьте флаг --apply")
        logger.info("─" * 60)
        return 0

    # Генерируем xlsx (тот же вызов что в export.py)
    try:
        xlsx_bytes = build_xlsx(
            military_records, relatives, manager_label=mgr["name"]
        )
    except Exception:
        logger.exception("Ошибка генерации xlsx")
        return 1

    # Сохраняем в файл
    if out_path is None:
        filename = make_filename(mgr["name"])
        out_path = Path("/root/exports") / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(xlsx_bytes)
    logger.info(f"Файл сохранён: {out_path} ({len(xlsx_bytes)} байт)")

    # Помечаем как выгруженные
    await mark_military_exported(military_ids)
    logger.info(f"Помечено как выгруженные: {total} лидов")

    logger.info("─" * 60)
    logger.info(f"✅ Готово. Забрать: scp root@188.137.224.21:{out_path} .")
    logger.info("─" * 60)
    return 0


async def _run(manager_id: int, apply: bool, out_path: Path | None):
    try:
        return await main(manager_id, apply, out_path)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-id", type=int, required=True,
                        help="ID менеджера в таблице managers")
    parser.add_argument("--apply", action="store_true",
                        help="Реально выгрузить и пометить (без флага — dry-run)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Путь к выходному файлу (по умолчанию /root/exports/<auto>)")
    args = parser.parse_args()
    rc = asyncio.run(_run(args.manager_id, args.apply, args.out))
    raise SystemExit(rc or 0)
