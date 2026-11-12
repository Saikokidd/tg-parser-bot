#!/usr/bin/env python3
"""
Свап: случайные заполненные лиды доноров ha/dp → Соне (111),
встречно от Драйвера (139) каждому донору ровно столько же
лидов и родственников. Офис лида и его родственников меняется
на офис нового владельца.

    venv/bin/python -m bot.services.swap_hadp_sonya            # сухой прогон
    venv/bin/python -m bot.services.swap_hadp_sonya --apply    # боевой
    venv/bin/python -m bot.services.swap_hadp_sonya --count 50
"""

import argparse
import asyncio
import random
import sys
from collections import defaultdict

from bot.db.queries import get_pool

SONYA = 111
SONYA_OFFICE = "pvl"
DRIVER = 139
DONOR_OFFICES = ("ha", "dp", "pvl")

# Кого не трогаем как доноров: получатель, источник возврата
# и менеджеры, чей запас решили не расходовать.
EXCLUDE_DONORS = [SONYA, DRIVER, 54, 166, 156, 159, 58]
# 54 Макс, 166 Коля — запас не расходуем
# 156 Ника9900, 159 Дима9900, 58 Тёма — сырые посты из каналов, пробива нет


# Лиды, чей родственник привязан ещё к одному военному, в свап не берём:
# такой родственник уезжает в обе стороны сразу — и счёт двоится, и офис
# у него остаётся от того переноса, что отработал последним.
async def pool_donors(conn):
    rows = await conn.fetch(
        """
        SELECT pm.id, pm.added_by, pm.office, m.name AS manager_name,
               COUNT(DISTINCT mr.relative_id) AS rel_cnt
        FROM persons_military pm
        JOIN military_relatives mr ON mr.military_id = pm.id
        LEFT JOIN managers m ON m.id = pm.added_by
        WHERE pm.exported_at IS NULL AND NOT EXISTS (SELECT 1 FROM military_relatives mr2 JOIN military_relatives mr3 ON mr3.relative_id = mr2.relative_id AND mr3.military_id <> pm.id WHERE mr2.military_id = pm.id)
          AND pm.office = ANY($1)
          AND pm.added_by <> ALL($2::int[])
        GROUP BY pm.id, pm.added_by, pm.office, m.name
        """,
        list(DONOR_OFFICES), EXCLUDE_DONORS,
    )
    return [dict(r) for r in rows]


async def pool_driver(conn):
    """Лиды Драйвера с хотя бы одним телефоном — возвращаем равноценное."""
    rows = await conn.fetch(
        """
        SELECT pm.id, COUNT(DISTINCT mr.relative_id) AS rel_cnt
        FROM persons_military pm
        JOIN military_relatives mr ON mr.military_id = pm.id
        JOIN relatives r           ON r.id = mr.relative_id
        WHERE pm.exported_at IS NULL AND NOT EXISTS (SELECT 1 FROM military_relatives mr2 JOIN military_relatives mr3 ON mr3.relative_id = mr2.relative_id AND mr3.military_id <> pm.id WHERE mr2.military_id = pm.id) AND pm.added_by = $1
        GROUP BY pm.id
        HAVING COUNT(*) FILTER (WHERE r.phone IS NOT NULL AND r.phone <> '') > 0
        """,
        DRIVER,
    )
    return [dict(r) for r in rows]


def pick_exact(avail: list[dict], need_leads: int, need_rels: int):
    """
    Выбрать ровно need_leads лидов с суммой родственников ровно need_rels.
    Жадно: берём самые «лёгкие», затем меняем их на более «тяжёлые»,
    пока сумма не сойдётся. Возвращает (выбранные, остаток) или (None, avail).
    """
    if need_leads > len(avail):
        return None, avail
    pool = sorted(avail, key=lambda x: x["rel_cnt"])
    chosen = pool[:need_leads]
    rest = pool[need_leads:]
    total = sum(c["rel_cnt"] for c in chosen)

    # не хватает родственников — меняем лёгкий выбранный на тяжёлый свободный
    while total < need_rels and rest:
        deficit = need_rels - total
        light = min(chosen, key=lambda x: x["rel_cnt"])
        # лучший кандидат: не перелететь через нужную сумму
        cands = [r for r in rest if r["rel_cnt"] > light["rel_cnt"]]
        if not cands:
            break
        fit = [r for r in cands if r["rel_cnt"] - light["rel_cnt"] <= deficit]
        swap_in = max(fit, key=lambda x: x["rel_cnt"]) if fit else min(
            cands, key=lambda x: x["rel_cnt"])
        if swap_in["rel_cnt"] - light["rel_cnt"] > deficit:
            break
        chosen.remove(light); rest.append(light)
        rest.remove(swap_in); chosen.append(swap_in)
        total = sum(c["rel_cnt"] for c in chosen)

    # перебор — меняем тяжёлый выбранный на лёгкий свободный
    while total > need_rels and rest:
        excess = total - need_rels
        heavy = max(chosen, key=lambda x: x["rel_cnt"])
        cands = [r for r in rest if r["rel_cnt"] < heavy["rel_cnt"]]
        if not cands:
            break
        fit = [r for r in cands if heavy["rel_cnt"] - r["rel_cnt"] <= excess]
        swap_in = min(fit, key=lambda x: x["rel_cnt"]) if fit else max(
            cands, key=lambda x: x["rel_cnt"])
        if heavy["rel_cnt"] - swap_in["rel_cnt"] > excess:
            break
        chosen.remove(heavy); rest.append(heavy)
        rest.remove(swap_in); chosen.append(swap_in)
        total = sum(c["rel_cnt"] for c in chosen)

    if total != need_rels:
        return None, avail
    return chosen, rest


async def move(conn, lead_ids: list[int], manager_id: int, office: str) -> None:
    await conn.execute(
        """
        UPDATE relatives r SET added_by = $1, office = $2
        FROM military_relatives mr
        WHERE mr.relative_id = r.id AND mr.military_id = ANY($3::int[])
        """,
        manager_id, office, lead_ids,
    )
    await conn.execute(
        "UPDATE persons_military SET added_by = $1, office = $2 WHERE id = ANY($3::int[])",
        manager_id, office, lead_ids,
    )


async def run(count: int, apply: bool, seed: int | None) -> None:
    rnd = random.Random(seed)
    pool = await get_pool()
    async with pool.acquire() as conn:
        donors = await pool_donors(conn)
        driver = await pool_driver(conn)

        print(f"Пул доноров:       {len(donors)} лидов")
        print(f"Пул Драйвера:      {len(driver)} лидов с телефоном")
        if count > len(donors):
            sys.exit(f"У доноров всего {len(donors)} лидов, запрошено {count}")

        taken = rnd.sample(donors, count)

        by_donor = defaultdict(list)
        for t in taken:
            by_donor[(t["added_by"], t["office"], t["manager_name"])].append(t)

        print(f"\nОтбор {count} лидов у {len(by_donor)} доноров:\n")
        plan = []
        avail = list(driver)
        ok = True
        for (mid, office, name), leads in sorted(
                by_donor.items(), key=lambda kv: -len(kv[1])):
            L = len(leads)
            R = sum(x["rel_cnt"] for x in leads)
            back, avail = pick_exact(avail, L, R)
            status = "ok" if back else "НЕ ПОДОБРАТЬ"
            if not back:
                ok = False
            print(f"  {office}/{name:16s} (id={mid:3d}): отдаёт {L:3d} лид / "
                  f"{R:3d} родств. → возврат {status}")
            plan.append((mid, office, name, leads, back))

        tot_l = sum(len(p[3]) for p in plan)
        tot_r = sum(sum(x["rel_cnt"] for x in p[3]) for p in plan)
        print(f"\nИтого к Соне: {tot_l} лидов, {tot_r} родственников")
        print(f"Остаток пула Драйвера после возврата: {len(avail)}")

        if not ok:
            sys.exit("\nПодбор не сошёлся — боевой запуск отменён.")
        if not apply:
            print("\nСухой прогон. Для записи добавь --apply")
            return

        async with conn.transaction():
            for mid, office, name, leads, back in plan:
                await move(conn, [x["id"] for x in leads], SONYA, SONYA_OFFICE)
                await move(conn, [x["id"] for x in back], mid, office)
            print("\nПеренос выполнен.")

        rows = await conn.fetch(
            """
            SELECT m.name, pm.office, COUNT(*) AS невыгруженных
            FROM persons_military pm JOIN managers m ON m.id = pm.added_by
            WHERE pm.exported_at IS NULL
              AND pm.added_by = ANY($1::int[])
            GROUP BY 1,2 ORDER BY 1
            """,
            [SONYA, DRIVER] + [p[0] for p in plan],
        )
        print("\nПосле переноса:")
        for r in rows:
            print(f"  {r['name']:18s} {r['office']:4s} {r['невыгруженных']:6d}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=50)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--seed", type=int, default=None)
    a = p.parse_args()
    asyncio.run(run(a.count, a.apply, a.seed))


if __name__ == "__main__":
    main()
