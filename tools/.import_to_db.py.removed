"""
Импорт распарсенных JSON в БД.

Логика:
- Для каждого JSON-файла берём имя менеджера из имени файла (Миша.json → менеджер 'Миша')
- Для каждого военного:
    - Если ФИО None → пропускаем (логируем)
    - Дубль-чек по ФИО+ДР → пропускаем
    - Иначе INSERT
- Для каждого родственника:
    - Дубль-чек 2 из 4 → если есть, переиспользуем существующий и просто создаём связку
    - Иначе INSERT + связка
- Все пропуски логируем в parsed/_import_log.txt

Использование:
    venv/bin/python -m tools.import_to_db --dry-run        # тест без записи в БД
    venv/bin/python -m tools.import_to_db                  # реальный импорт
"""
import asyncio
import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.db.connection import get_pool, close_pool
from bot.db.queries import (
    find_military_duplicates,
    insert_military,
    find_relative_duplicates,
    insert_relative,
    link_military_relative,
    list_managers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_to_db")


PARSED_DIR = PROJECT_ROOT / "parsed"
LOG_FILE = PARSED_DIR / "_import_log.txt"


def parse_date_str(s: str | None) -> date | None:
    """'15.03.1985' → date(1985, 3, 15). None если невалидно."""
    if not s:
        return None
    try:
        d, m, y = s.split(".")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


async def get_manager_id_by_name(name: str) -> int | None:
    """Найти менеджера по имени"""
    managers = await list_managers(only_active=False)
    for m in managers:
        if m['name'].strip().lower() == name.strip().lower():
            return m['id']
    return None


async def import_one_file(json_path: Path, dry_run: bool, log_lines: list, stats: dict):
    """Импорт одного JSON-файла"""
    manager_name = json_path.stem  # 'Миша.json' → 'Миша'

    manager_id = await get_manager_id_by_name(manager_name)
    if not manager_id:
        msg = f"⚠️  Менеджер '{manager_name}' не найден в БД — пропускаем файл"
        logger.error(msg)
        log_lines.append(msg)
        return

    logger.info(f"=== {manager_name} (manager_id={manager_id}) ===")

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    file_stats = {
        "military_total": len(records),
        "military_skipped_no_name": 0,
        "military_skipped_dup": 0,
        "military_inserted": 0,
        "relatives_inserted": 0,
        "relatives_reused": 0,
        "relatives_skipped_no_name": 0,
        "links_created": 0,
    }

    for idx, m in enumerate(records, 1):
        full_name = (m.get("full_name") or "").strip()
        if not full_name:
            file_stats["military_skipped_no_name"] += 1
            note = m.get("extra", {}).get("note", "")
            log_lines.append(
                f"[{manager_name} #{idx}] SKIP военный без ФИО (note='{note}'). "
                f"Родственников в записи: {len(m.get('relatives', []))}"
            )
            continue

        birth_date = parse_date_str(m.get("birth_date"))

        # Дубль-чек
        if birth_date:
            dups = await find_military_duplicates(full_name=full_name, birth_date=birth_date)
            if dups:
                file_stats["military_skipped_dup"] += 1
                log_lines.append(
                    f"[{manager_name} #{idx}] DUP военный {full_name} ({m.get('birth_date')}) — "
                    f"уже есть #{dups[0]['id']}"
                )
                # Используем существующего и продолжаем привязывать родственников
                military_id = dups[0]['id']
            else:
                military_id = None
        else:
            military_id = None

        if military_id is None:
            military_data = {
                "full_name": full_name,
                "birth_date": birth_date,
                "status": m.get("status") or "missing",
                "extra": {k: v for k, v in (m.get("extra") or {}).items() if v},
            }

            if dry_run:
                military_id = -1  # фейковый
                file_stats["military_inserted"] += 1
            else:
                try:
                    record = await insert_military(military_data, manager_id)
                    military_id = record["id"]
                    file_stats["military_inserted"] += 1
                except Exception as e:
                    log_lines.append(f"[{manager_name} #{idx}] ERR военный {full_name}: {e}")
                    continue

        # Родственники
        for r in m.get("relatives", []):
            r_name = (r.get("full_name") or "").strip()
            if not r_name:
                file_stats["relatives_skipped_no_name"] += 1
                continue

            r_birth = parse_date_str(r.get("birth_date"))
            r_phone = r.get("phone") or None
            r_address = r.get("address") or None

            # Очищаем extra от пустот
            r_extra = {k: v for k, v in (r.get("extra") or {}).items() if v}

            # Дубль-чек 2 из 4
            existing_id = None
            if not dry_run:
                rel_dups = await find_relative_duplicates(
                    full_name=r_name,
                    birth_date=r_birth,
                    phone=r_phone,
                    address=r_address,
                )
                if rel_dups:
                    existing_id = rel_dups[0]["id"]

            if existing_id:
                # Переиспользуем — просто создаём связку
                if not dry_run:
                    created = await link_military_relative(military_id, existing_id, manager_id)
                    if created:
                        file_stats["links_created"] += 1
                file_stats["relatives_reused"] += 1
            else:
                # Создаём нового родственника + связку
                if dry_run:
                    file_stats["relatives_inserted"] += 1
                    file_stats["links_created"] += 1
                else:
                    try:
                        rel_data = {
                            "full_name": r_name,
                            "birth_date": r_birth,
                            "phone": r_phone,
                            "address": r_address,
                            "extra": r_extra,
                        }
                        rel_record = await insert_relative(rel_data, manager_id)
                        await link_military_relative(military_id, rel_record["id"], manager_id)
                        file_stats["relatives_inserted"] += 1
                        file_stats["links_created"] += 1
                    except Exception as e:
                        log_lines.append(f"[{manager_name} #{idx}] ERR родственник {r_name}: {e}")

    # Сводка по файлу
    logger.info(f"  Военных: всего={file_stats['military_total']}, "
                f"добавлено={file_stats['military_inserted']}, "
                f"дубль={file_stats['military_skipped_dup']}, "
                f"без ФИО={file_stats['military_skipped_no_name']}")
    logger.info(f"  Родственников: добавлено={file_stats['relatives_inserted']}, "
                f"переиспользовано={file_stats['relatives_reused']}, "
                f"без ФИО={file_stats['relatives_skipped_no_name']}, "
                f"связок={file_stats['links_created']}")

    # Накапливаем общую статистику
    for k, v in file_stats.items():
        stats[k] = stats.get(k, 0) + v


async def main(dry_run: bool):
    if dry_run:
        logger.warning("=== DRY RUN — записи в БД НЕ будут произведены ===")

    files = sorted(PARSED_DIR.glob("*.json"))
    files = [f for f in files if not f.name.startswith("_")]

    if not files:
        logger.error(f"Не найдено JSON-файлов в {PARSED_DIR}")
        await close_pool()
        return

    logger.info(f"Найдено JSON-файлов: {len(files)}")
    for f in files:
        logger.info(f"  • {f.name}")

    if not dry_run:
        confirm = input("\nЗагрузить в БД? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            logger.info("Отмена.")
            await close_pool()
            return

    log_lines = []
    stats = {}

    for json_path in files:
        await import_one_file(json_path, dry_run, log_lines, stats)

    # Сохраняем лог
    LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")

    logger.info("=" * 50)
    logger.info("ИТОГО:")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")
    logger.info(f"\nЛог сохранён в: {LOG_FILE}")

    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Имитация импорта без записи в БД")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))
