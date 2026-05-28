"""
Чистка битых телефонов в БД.

Битый = после удаления нецифровых символов получилось не 11 и не 12 цифр.
Это либо склейка нескольких номеров, либо обрезок, либо мусор.

Стратегия:
1. Берём все relatives с битым phone.
2. Прогоняем через extract_all_phones() (умеет резать слипшиеся).
3. Если получили хотя бы один валидный номер:
   - Первый → phone
   - Остальные → extra.phones_other (если их там ещё нет)
4. Если ни одного валидного — phone = NULL.

Запуск:
    venv/bin/python -m tools.fix_broken_phones --dry-run    # посмотреть
    venv/bin/python -m tools.fix_broken_phones              # применить
"""
import asyncio
import argparse
import json
import re
from collections import Counter

from bot.db.connection import get_pool, close_pool
from bot.parser.relative_parser import extract_all_phones


async def find_broken() -> list[dict]:
    """
    Берём родственников у которых phone после удаления нецифровых
    содержит не 11 и не 12 цифр — это и есть критерий "битого".
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, full_name, phone, extra
            FROM relatives
            WHERE phone IS NOT NULL
              AND phone != ''
              AND LENGTH(REGEXP_REPLACE(phone, '\D', '', 'g')) NOT IN (11, 12)
            ORDER BY id
            """
        )
        return [dict(r) for r in rows]


def reparse(old_phone: str) -> list[str]:
    """Вернуть список валидных номеров после повторного парсинга."""
    return extract_all_phones(old_phone) or []


async def apply_fix(rel_id: int, new_phone: str | None, new_extra: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE relatives SET phone = $2, extra = $3, updated_at = NOW() WHERE id = $1",
            rel_id, new_phone, new_extra,
        )


async def main(dry_run: bool) -> None:
    broken = await find_broken()
    print(f"Найдено битых номеров: {len(broken)}")
    if not broken:
        return

    stats = Counter()
    samples = {"split": [], "cleared": [], "kept": []}

    for r in broken:
        rel_id = r["id"]
        old = r["phone"]
        extra = dict(r["extra"] or {})
        phones = reparse(old)

        if not phones:
            # ничего валидного не извлеклось — обнуляем
            new_phone = None
            stats["cleared"] += 1
            if len(samples["cleared"]) < 5:
                samples["cleared"].append((rel_id, r["full_name"], old))
        else:
            new_phone = phones[0]
            extras_phones = phones[1:]
            if extras_phones:
                # дополняем existing phones_other (через запятую)
                current_other = extra.get("phones_other", "") or ""
                current_list = [p.strip() for p in re.split(r'[,\s]+', current_other) if p.strip()]
                merged = list(dict.fromkeys(current_list + extras_phones))  # uniq, preserve order
                extra["phones_other"] = ", ".join(merged)
                stats["split"] += 1
                if len(samples["split"]) < 5:
                    samples["split"].append((rel_id, r["full_name"], old, new_phone, extras_phones))
            else:
                stats["kept"] += 1
                if len(samples["kept"]) < 5:
                    samples["kept"].append((rel_id, r["full_name"], old, new_phone))

        if not dry_run:
            await apply_fix(rel_id, new_phone, extra)

    print(f"\n=== Статистика ===")
    print(f"  split   (несколько номеров → phone + phones_other): {stats['split']}")
    print(f"  kept    (один валидный, просто нормализован):       {stats['kept']}")
    print(f"  cleared (мусор без валидных → phone=NULL):          {stats['cleared']}")
    print(f"  Всего:                                              {len(broken)}")

    print(f"\n=== Примеры split (до 5) ===")
    for rel_id, name, old, new, extras in samples["split"]:
        print(f"  #{rel_id} {name}: {old!r} → {new} (доп: {extras})")

    print(f"\n=== Примеры kept (до 5) ===")
    for rel_id, name, old, new in samples["kept"]:
        print(f"  #{rel_id} {name}: {old!r} → {new}")

    print(f"\n=== Примеры cleared (до 5) ===")
    for rel_id, name, old in samples["cleared"]:
        print(f"  #{rel_id} {name}: {old!r} → NULL")

    if dry_run:
        print("\n⚠️ DRY-RUN — изменения в БД НЕ применены. Запусти без --dry-run чтобы применить.")
    else:
        print("\n✅ Изменения применены.")


async def _entrypoint(dry_run: bool) -> None:
    try:
        await main(dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Показать что будет сделано, без записи в БД")
    args = parser.parse_args()
    asyncio.run(_entrypoint(args.dry_run))
