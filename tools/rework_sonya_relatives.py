#!/usr/bin/env python3
"""
Переработка batch-родственников Сони по НОВЫМ критериям фонда.

Область: ТОЛЬКО военные с extra.moved.batch = sauron_links_20260720
и их родственники added_by=111 с непустым extra.relation.
Ручные родственники (relation IS NULL, не-batch) НЕ трогаются.

ОСТАВЛЯЕМ на военного:
  - Жена (Супруга / ровесница)
  - Мать
  - Отец  — только если у военного НЕТ матери среди batch-родственников
  - Сын/Дочь с возрастом >= 18 лет на сегодня
УДАЛЯЕМ: всё остальное (брат, сестра, дети <18, дети без ДР,
         старший/младший родственник, бабушка, дедушка, «связь» и пр.)

Удаление: карточка relatives + связь, НО только если родственник привязан
к одному военному. Если к нескольким — лишь отвязываем (чужую связь не рушим).

    venv/bin/python -m tools.rework_sonya_relatives            # DRY-RUN
    venv/bin/python -m tools.rework_sonya_relatives --commit   # выполнить
"""
import argparse
import asyncio
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from bot.db.connection import get_pool

BATCH = "sauron_links_20260720"
SONYA = 111
def _minus_18(d: date) -> date:
    # дата «18 лет назад» без dateutil; 29 фев -> 28 фев
    try:
        return d.replace(year=d.year - 18)
    except ValueError:
        return d.replace(year=d.year - 18, day=28)

ADULT_BEFORE = _minus_18(date.today())   # ДР <= этой даты => >=18 лет


def classify(relation: str) -> str:
    r = (relation or "").strip().lower()
    if "супруг" in r:
        return "spouse"
    if "мать" in r:
        return "mother"
    if "отец" in r:
        return "father"
    if "сын" in r or "дочь" in r:
        return "child"
    return "other"


async def main(commit: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pm.id AS mil_id, pm.full_name AS mil_name,
                   r.id AS rel_id, r.full_name AS rel_name, r.birth_date,
                   r.extra->>'relation' AS relation,
                   (SELECT COUNT(*) FROM military_relatives mr2 WHERE mr2.relative_id = r.id) AS link_count
            FROM persons_military pm
            JOIN military_relatives mr ON mr.military_id = pm.id
            JOIN relatives r ON r.id = mr.relative_id
            WHERE pm.added_by = $1
              AND pm.extra->'moved'->>'batch' = $2
              AND r.added_by = $1
              AND (r.extra->>'relation') IS NOT NULL
            ORDER BY pm.id
            """,
            SONYA, BATCH,
        )

    # группируем по военному
    by_mil = {}
    for r in rows:
        by_mil.setdefault(r["mil_id"], []).append(r)

    keep_ids, del_rel_ids, unlink_pairs = [], [], []
    emptied = 0
    for mil_id, rels in by_mil.items():
        has_mother = any(classify(x["relation"]) == "mother" for x in rels)
        kept_here = 0
        for x in rels:
            c = classify(x["relation"])
            keep = False
            if c == "spouse" or c == "mother":
                keep = True
            elif c == "father":
                keep = not has_mother
            elif c == "child":
                keep = x["birth_date"] is not None and x["birth_date"] <= ADULT_BEFORE
            # other -> удалить
            if keep:
                keep_ids.append(x["rel_id"]); kept_here += 1
            else:
                if x["link_count"] == 1:
                    del_rel_ids.append(x["rel_id"])
                else:
                    unlink_pairs.append((mil_id, x["rel_id"]))
        if kept_here == 0:
            emptied += 1

    print(f"Batch-военных с роднёй: {len(by_mil)}")
    print(f"Batch-связей рассмотрено: {len(rows)}")
    print(f"  ОСТАВЛЯЕМ: {len(keep_ids)}")
    print(f"  УДАЛЯЕМ (карточка+связь, одиночные): {len(del_rel_ids)}")
    print(f"  ОТВЯЗЫВАЕМ (общие с др. военными): {len(unlink_pairs)}")
    print(f"  Военных станет пустыми: {emptied}")

    # разбивка что удаляем по типам
    from collections import Counter
    del_by_type = Counter()
    for r in rows:
        if r["rel_id"] in set(del_rel_ids) or (r["mil_id"], r["rel_id"]) in set(unlink_pairs):
            del_by_type[classify(r["relation"])] += 1
    print("  Удаляемое по типам:", dict(del_by_type))

    if not commit:
        print("\n[DRY-RUN] Ничего не удалено. Для выполнения: --commit")
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            if unlink_pairs:
                await conn.executemany(
                    "DELETE FROM military_relatives WHERE military_id=$1 AND relative_id=$2",
                    unlink_pairs,
                )
            if del_rel_ids:
                # сперва связи, потом карточки
                await conn.execute(
                    "DELETE FROM military_relatives WHERE relative_id = ANY($1::int[])",
                    del_rel_ids,
                )
                await conn.execute(
                    "DELETE FROM relatives WHERE id = ANY($1::int[])",
                    del_rel_ids,
                )
    print(f"\n✅ Удалено карточек: {len(del_rel_ids)}, отвязано: {len(unlink_pairs)}")
    print(f"   Военных стало пустыми (выпадут из выгрузки): {emptied}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.commit))