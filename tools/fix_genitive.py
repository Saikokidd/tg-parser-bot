#!/usr/bin/env python3
"""
Приведение ФИО из родительного падежа в именительный.

Менеджеры копируют из объявления «Ищу Косачева Александра Андреевича» — и ФИО
остаётся в родительном. Саурон ищет по именительному и такого человека не
находит, лид уходит в мусор.

Якорь — отчество на «ича»: в именительном падеже мужское отчество так
кончаться не может, поэтому ложных срабатываний нет.

    venv/bin/python -m tools.fix_genitive
    venv/bin/python -m tools.fix_genitive --owner 1
    venv/bin/python -m tools.fix_genitive --apply
"""
import argparse
import asyncio
import re

from bot.db.queries import get_pool

# Имена с беглой гласной: простое отбрасывание «а» даёт мусор


# Имена с беглой гласной: простое отбрасывание «а» даёт мусор
FLEETING = {"павла": "павел", "льва": "лев"}


def fix_patr(w: str) -> str:
    """Андреевича → Андреевич"""
    return w[:-1] if w.lower().endswith("ича") else w


def fix_name(w: str) -> str:
    low = w.lower()
    if low in FLEETING:
        out = FLEETING[low]
    elif low.endswith("я"):          # Алексея → Алексей, Николая → Николай
        out = low[:-1] + "й"
    elif low.endswith("ы"):          # Никиты → Никита
        out = low[:-1] + "а"
    elif low.endswith("и"):          # Ильи → Илья
        out = low[:-1] + "я"
    elif low.endswith("а"):          # Александра → Александр
        out = low[:-1]
    else:
        return w
    return out.capitalize() if w[:1].isupper() else out


def fix_surname(w: str) -> str:
    low = w.lower()
    if low.endswith(("ского", "цкого")):      # Достоевского → Достоевский
        out = low[:-3] + "ий"
    elif low.endswith("ого"):                 # Белого → Белый
        out = low[:-3] + "ый"
    elif low.endswith("я"):                   # Коваля → Коваль
        out = low[:-1] + "ь"
    elif low.endswith("а"):                   # Косачева → Косачев
        out = low[:-1]
    else:
        return w
    return out.capitalize() if w[:1].isupper() else out


def convert(fio: str):
    t = re.split(r"\s+", fio.strip())
    if len(t) != 3 or not t[2].lower().endswith("ича"):
        return None
    new = [fix_surname(t[0]), fix_name(t[1]), fix_patr(t[2])]
    out = " ".join(new)
    return out if out != fio.strip() else None


async def run(owner, apply, limit):
    pool = await get_pool()
    where = ["pm.exported_at IS NULL", "pm.birth_date IS NOT NULL",
             r"btrim(pm.full_name) ~* 'ича\s*$'"]
    params = []
    if owner:
        params.append(owner)
        where.append(f"pm.added_by = ${len(params)}")
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT pm.id, btrim(pm.full_name) AS fio, m.name AS владелец
            FROM persons_military pm LEFT JOIN managers m ON m.id = pm.added_by
            WHERE {' AND '.join(where)}
            ORDER BY pm.id
            LIMIT ${len(params)}
        """, *params)

    plan = []
    for r in rows:
        new = convert(r["fio"])
        if new:
            plan.append((r["id"], r["fio"], new, r["владелец"]))

    print(f"кандидатов: {len(rows)}   преобразуется: {len(plan)}")
    print("\nпримеры:")
    for _, old, new, who in plan[:15]:
        print(f"  {old:38s} → {new:38s}  [{who}]")

    if not apply:
        print("\nСухой прогон. Для записи добавь --apply")
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS _bak_genitive_20261116
                (id int, old_name text, new_name text, fixed_at timestamp default now())
            """)
            for lid, old, new, _ in plan:
                await conn.execute(
                    "INSERT INTO _bak_genitive_20261116 (id, old_name, new_name) "
                    "VALUES ($1, $2, $3)", lid, old, new)
                await conn.execute(
                    "UPDATE persons_military SET full_name = $2 WHERE id = $1",
                    lid, new)
    print(f"\nисправлено: {len(plan)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--owner", type=int, default=None)
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    asyncio.run(run(a.owner, a.apply, a.limit))


if __name__ == "__main__":
    main()
