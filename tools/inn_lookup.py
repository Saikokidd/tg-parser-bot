#!/usr/bin/env python3
"""
ИНН военных лидов Сони через Sauron.

    venv/bin/python -m tools.inn_lookup                  # сухой прогон
    venv/bin/python -m tools.inn_lookup --apply --limit 3
    venv/bin/python -m tools.inn_lookup --apply
"""
import argparse
import asyncio
import re

from bot.db.queries import get_pool, insert_probiv_log
from bot.services.sauron_api import query_person, split_full_name

MANAGER = 111
OFFICE = "pvl"
OUT = "/root/exports/sonya_inn.txt"
PAUSE = 1.0


def valid_inn(d: str) -> bool:
    """Личный ИНН: ровно 12 цифр и верные контрольные разряды."""
    if len(d) != 12 or not d.isdigit():
        return False
    n = [int(c) for c in d]
    c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k11 = sum(a * b for a, b in zip(c11, n[:10])) % 11 % 10
    k12 = sum(a * b for a, b in zip(c12, n[:11])) % 11 % 10
    return k11 == n[10] and k12 == n[11]


def collect_inn(node, found: set) -> None:
    """Рекурсивно собрать все значения под ключом 'ИНН' в ответе Саурона."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.strip().upper() == "ИНН" and v:
                d = re.sub(r"\D", "", str(v))
                if valid_inn(d):
                    found.add(d)
            else:
                collect_inn(v, found)
    elif isinstance(node, list):
        for item in node:
            collect_inn(item, found)


async def main(apply: bool, limit) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            r"""
            SELECT id, regexp_replace(full_name, '\s+', ' ', 'g') AS full_name,
                   birth_date
            FROM persons_military
            WHERE added_by = $1 AND exported_at IS NULL
            ORDER BY id
            """,
            MANAGER,
        )

    work, skip = [], []
    for r in rows:
        if r["birth_date"] is None:
            skip.append(r)
            continue
        last, first, middle = split_full_name(r["full_name"])
        if not last or not first:
            skip.append(r)
        else:
            work.append((r, last, first, middle))

    print(f"Всего: {len(rows)}   к пробиву: {len(work)}   пропуск: {len(skip)}")
    for r in skip:
        print(f"  пропуск: {r['full_name'][:60]}")

    if limit:
        work = work[:limit]
    if not apply:
        print(f"\nСухой прогон. Запросов будет {len(work)}. Для записи добавь --apply")
        return

    results, spent, found_cnt = {}, 0.0, 0
    for i, (r, last, first, middle) in enumerate(work, 1):
        bd = r["birth_date"]
        inns = set()
        try:
            res = await query_person(last, first, middle,
                                     day=bd.day, month=bd.month, year=bd.year,
                                     office=OFFICE)
            collect_inn(res.get("response", {}).get("result", []), inns)
            cost = float(res.get("cost") or 0)
            spent += cost
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=cost,
                currency="USD", military_id=r["id"], success=True, office=OFFICE,
            )
        except Exception as e:
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=0,
                currency="USD", military_id=r["id"], success=False,
                error=str(e), office=OFFICE,
            )
            print(f"  [{i}/{len(work)}] ОШИБКА id={r['id']}: {e}")
            results[r["id"]] = "ошибка"
            await asyncio.sleep(PAUSE)
            continue

        if inns:
            found_cnt += 1
        results[r["id"]] = ", ".join(sorted(inns)) if inns else "не найден"
        print(f"  [{i}/{len(work)}] id={r['id']:<7} ИНН: {len(inns)}   ${spent:.2f}")
        await asyncio.sleep(PAUSE)

    with open(OUT, "w", encoding="utf-8") as f:
        for r in rows:
            bd = r["birth_date"].strftime("%d.%m.%Y") if r["birth_date"] else ""
            val = results.get(r["id"], "пропущен")
            f.write(" ".join(f"{r['full_name']} {bd}".split()) + f" — {val}\n")

    print(f"\nНайдено у {found_cnt} из {len(work)}. Потрачено ${spent:.2f}")
    print(f"Файл: {OUT}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    a = p.parse_args()
    asyncio.run(main(a.apply, a.limit))
