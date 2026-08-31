"""
Одноразовая утилита свапа: Соня <- dp/ha, Findre -> dp/ha.

Логика:
  1. Считаем квоту для каждого донора (пропорция от dp и ha, всего ~53)
  2. Для каждого донора берём FIFO военных (по created_at ASC) пока не наберём
     квоту родственников с погрешностью ±1
  3. Каждому донору подбираем от Findre набор военных с ТОЧНЫМ совпадением
     (military_count, uniq_relative_count). Соотношение пары строгое, но
     число родственников допускает ±1 от исходной квоты донора.
  4. Всё в одной транзакции: BEGIN → UPDATE persons_military.added_by/office
     + UPDATE relatives.added_by/office + set source_id=NULL → COMMIT
  5. Пишем отчёт в /root/exports/swap_YYYYMMDD_HHMMSS.txt

Использование:
    venv/bin/python -m tools.swap_leads_to_sonya                    # dry-run
    venv/bin/python -m tools.swap_leads_to_sonya --apply            # apply
"""
import asyncio
import argparse
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bot.db.connection import get_pool, close_pool


SONYA_ID = 111
SONYA_OFFICE = "pvl"
FINDRE_ID = 1
DP_QUOTA_TOTAL = 35
HA_QUOTA_TOTAL = 10
TOLERANCE_PER_DONOR = 1  # ±1 родственник допуск (стартовый)
MAX_QUOTA_PER_DONOR = 5  # верхний потолок на донора — облегчает симметрию с Findre


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def fetch_donor_pool(conn):
    """
    Все готовые невыгруженные лиды доноров dp/ha с числом родственников на лид.
    Возвращает: {mgr_id: {name, office, leads: [{military_id, rel_count, created_at, rel_ids}]}}
    """
    rows = await conn.fetch("""
        SELECT
            m.id AS mgr_id, m.name AS mgr_name, m.office,
            pm.id AS military_id,
            pm.created_at,
            array_agg(mr.relative_id ORDER BY mr.relative_id) AS rel_ids
        FROM persons_military pm
        JOIN managers m ON m.id = pm.added_by
        JOIN military_relatives mr ON mr.military_id = pm.id
        WHERE pm.exported_at IS NULL
          AND m.office IN ('dp', 'ha')
          AND m.is_active AND NOT m.is_disabled
        GROUP BY m.id, m.name, m.office, pm.id, pm.created_at
        ORDER BY m.id, pm.created_at ASC
    """)
    donors = defaultdict(lambda: {"name": None, "office": None, "leads": []})
    for r in rows:
        d = donors[r["mgr_id"]]
        d["name"] = r["mgr_name"]
        d["office"] = r["office"]
        d["leads"].append({
            "military_id": r["military_id"],
            "created_at": r["created_at"],
            "rel_ids": list(r["rel_ids"]),
            "rel_count": len(r["rel_ids"]),
        })
    return dict(donors)


async def fetch_findre_pool(conn):
    """
    Готовые невыгруженные лиды Findre с числом single-linked родственников.
    Возвращает: [{military_id, rel_count, created_at, rel_ids}] отсортированный FIFO.
    Многосвязных родственников исключаем (они привязаны к нескольким военным).
    """
    rows = await conn.fetch("""
        WITH findre_leads AS (
            SELECT pm.id AS military_id, pm.created_at
            FROM persons_military pm
            WHERE pm.added_by = $1 AND pm.exported_at IS NULL
        ),
        rel_link_count AS (
            SELECT mr.relative_id, COUNT(DISTINCT mr.military_id) AS linked_to
            FROM military_relatives mr
            JOIN findre_leads fl ON fl.military_id = mr.military_id
            GROUP BY mr.relative_id
        )
        SELECT
            fl.military_id, fl.created_at,
            COALESCE(
                array_agg(mr.relative_id ORDER BY mr.relative_id)
                FILTER (WHERE rlc.linked_to = 1),
                '{}'::int[]
            ) AS rel_ids
        FROM findre_leads fl
        LEFT JOIN military_relatives mr ON mr.military_id = fl.military_id
        LEFT JOIN rel_link_count rlc ON rlc.relative_id = mr.relative_id
        GROUP BY fl.military_id, fl.created_at
        HAVING COUNT(*) FILTER (WHERE rlc.linked_to = 1) > 0
        ORDER BY fl.created_at ASC
    """, FINDRE_ID)
    result = []
    for r in rows:
        rel_ids = list(r["rel_ids"])
        result.append({
            "military_id": r["military_id"],
            "created_at": r["created_at"],
            "rel_ids": rel_ids,
            "rel_count": len(rel_ids),
        })
    return result


def compute_donor_quotas(donors: dict) -> dict:
    """
    Пропорциональные квоты по родственникам для каждого донора,
    с верхним потолком MAX_QUOTA_PER_DONOR. Если у крупных доноров
    квота "срезается" потолком, остаток перераспределяется на других.
    Это даёт больше доноров с мелкими квотами (1-5 родств), которые
    легко симметрично подобрать в Findre-пуле.
    """
    def uniq_of(d):
        return len({r for lead in d["leads"] for r in lead["rel_ids"]})

    dp_donors = {mid: d for mid, d in donors.items() if d["office"] == "dp"}
    ha_donors = {mid: d for mid, d in donors.items() if d["office"] == "ha"}

    quotas = {}
    for office_donors, office_total in [(dp_donors, DP_QUOTA_TOTAL),
                                          (ha_donors, HA_QUOTA_TOTAL)]:
        if not office_donors:
            continue
        # Итеративное распределение: несколько проходов чтобы разложить
        # излишки от срезанных потолком доноров
        remaining_total = office_total
        assigned = {mid: 0 for mid in office_donors}
        eligible = dict(office_donors)  # кому ещё можно давать

        for _ in range(10):  # максимум 10 итераций
            if remaining_total <= 0 or not eligible:
                break
            pool_size = sum(uniq_of(d) for d in eligible.values())
            if pool_size == 0:
                break
            batch_total = remaining_total
            to_remove = []
            for mid, d in eligible.items():
                uniq = uniq_of(d)
                share = uniq / pool_size
                q = round(batch_total * share)
                # Ограничиваем потолком
                room = MAX_QUOTA_PER_DONOR - assigned[mid]
                if q > room:
                    q = room
                if q <= 0:
                    to_remove.append(mid)
                    continue
                assigned[mid] += q
                remaining_total -= q
                if assigned[mid] >= MAX_QUOTA_PER_DONOR:
                    to_remove.append(mid)
            for mid in to_remove:
                eligible.pop(mid, None)

        for mid, q in assigned.items():
            if q > 0:
                quotas[mid] = q

    return quotas


def pick_donor_leads(donor_leads: list, quota: int, tolerance: int) -> tuple[list, int, int]:
    """
    Гибридная стратегия выборки лидов донора:
      1. Идём FIFO (по created_at) пока не наберём квоту ±tolerance.
      2. Если первый же FIFO-лид сам по себе превышает quota+tolerance
         (или в процессе сумма перебирает) — переключаемся на выбор
         из остатка минимального по rel_count лида, добираем "мелкими".
      3. Останавливаемся когда попали в диапазон [quota-tol, quota+tol]
         или когда мелкие уже перебирают.
    """
    picked = []
    rel_count = 0
    idx = 0
    n = len(donor_leads)

    while idx < n:
        lead = donor_leads[idx]
        # Если добавление этого лида даст перебор — переключаемся на mode "мелкие"
        if rel_count + lead["rel_count"] > quota + tolerance:
            # Найти в остатке (от idx до конца) лид с минимальным rel_count
            remaining = donor_leads[idx:]
            smallest = min(remaining, key=lambda x: x["rel_count"])
            # Если даже самый мелкий даёт перебор — прекращаем
            if rel_count + smallest["rel_count"] > quota + tolerance:
                break
            picked.append(smallest)
            rel_count += smallest["rel_count"]
            # Убираем взятый из общего списка чтобы не взять его дважды
            donor_leads = donor_leads[:idx] + [l for l in remaining if l is not smallest]
            n = len(donor_leads)
            # НЕ инкрементируем idx — на этой позиции теперь другой лид
        else:
            picked.append(lead)
            rel_count += lead["rel_count"]
            idx += 1

        # Проверка попадания в квоту
        if quota - tolerance <= rel_count <= quota + tolerance:
            break

    return picked, len(picked), rel_count


def find_findre_match(findre_pool: list, used_ids: set,
                       target_mil: int, target_rel: int,
                       tolerance: int):
    """
    Подобрать из findre_pool набор ровно target_mil военных с
    target_rel ±tolerance родственниками.
    Ищет НАИЛУЧШЕЕ совпадение (мин. отклонение от target_rel),
    а не первое подходящее. Быстрая ранняя остановка если найден
    точный target_rel.
    """
    available = [l for l in findre_pool if l["military_id"] not in used_ids]
    if target_mil > len(available):
        return None

    MAX_BRANCHES = 30
    MAX_STATES = 50000
    state_count = [0]

    # Хранилище лучшего найденного
    best = {"picked": None, "dev": None}

    def solve(picked, remaining_mil, remaining_rel, start_idx):
        state_count[0] += 1
        if state_count[0] > MAX_STATES:
            return
        if remaining_mil == 0:
            dev = abs(remaining_rel)
            if dev <= tolerance:
                if best["dev"] is None or dev < best["dev"]:
                    best["picked"] = list(picked)
                    best["dev"] = dev
            return

        candidates = available[start_idx:]
        if len(candidates) < remaining_mil:
            return

        rel_counts = sorted([c["rel_count"] for c in candidates])
        min_sum = sum(rel_counts[:remaining_mil])
        max_sum = sum(rel_counts[-remaining_mil:])
        if remaining_rel + tolerance < min_sum or remaining_rel - tolerance > max_sum:
            return

        # Прекращаем поиск если найден точный target (dev=0)
        if best["dev"] == 0:
            return

        avg_needed = remaining_rel / remaining_mil
        sorted_cands = sorted(
            enumerate(candidates),
            key=lambda x: abs(x[1]["rel_count"] - avg_needed)
        )

        for orig_i, cand in sorted_cands[:MAX_BRANCHES]:
            global_idx = start_idx + orig_i
            solve(
                picked + [cand],
                remaining_mil - 1,
                remaining_rel - cand["rel_count"],
                global_idx + 1,
            )
            if best["dev"] == 0:
                return

    solve([], target_mil, target_rel, 0)
    if best["picked"] is None:
        return None
    total_rel = sum(l["rel_count"] for l in best["picked"])
    return best["picked"], target_mil, total_rel


async def main(apply: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        donors = await fetch_donor_pool(conn)
        findre_pool = await fetch_findre_pool(conn)

        logger.info(f"Findre пул: {len(findre_pool)} лидов, "
                    f"{sum(l['rel_count'] for l in findre_pool)} родственников (single-linked)")
        logger.info(f"Доноров: {len(donors)}")

        quotas = compute_donor_quotas(donors)
        logger.info(f"Рассчитаны квоты: {sum(quotas.values())} родственников всего")

        # === Первое плечо: берём у доноров ===
        take_plan = []
        for mgr_id, quota in sorted(quotas.items(), key=lambda kv: -kv[1]):
            donor = donors[mgr_id]
            picked, mc, rc = pick_donor_leads(donor["leads"], quota, TOLERANCE_PER_DONOR)
            if mc == 0:
                logger.warning(f"У {donor['name']} нечего взять")
                continue
            take_plan.append({
                "mgr_id": mgr_id, "mgr_name": donor["name"], "office": donor["office"],
                "leads": picked, "mil_count": mc, "rel_count": rc, "quota": quota,
            })

        # === Второе плечо: подбираем Findre под каждого донора ===
        used_findre_ids = set()
        return_plan = []
        skipped_donors = []
        MAX_TOLERANCE = 5
        for tp in take_plan:
            match = None
            actual_tol = None
            for tol in range(TOLERANCE_PER_DONOR, MAX_TOLERANCE + 1):
                match = find_findre_match(
                    findre_pool, used_findre_ids,
                    target_mil=tp["mil_count"], target_rel=tp["rel_count"],
                    tolerance=tol,
                )
                if match is not None:
                    actual_tol = tol
                    if tol > TOLERANCE_PER_DONOR:
                        logger.info(
                            f"Для {tp['mgr_name']} расширили tolerance до ±{tol}"
                        )
                    break
            if match is None:
                logger.warning(
                    f"Не удалось подобрать Findre для {tp['mgr_name']} "
                    f"({tp['mil_count']} воен / {tp['rel_count']} род) "
                    f"даже при ±{MAX_TOLERANCE} — ПРОПУСК"
                )
                skipped_donors.append(tp)
                continue
            findre_leads, mc, rc = match
            # 0 потерь: возврат должен быть >= забранного по родне И военным
            if rc < tp["rel_count"] or mc < tp["mil_count"]:
                logger.warning(
                    f"Для {tp['mgr_name']} Findre вернул бы меньше "
                    f"({mc} воен/{rc} род vs забрали {tp['mil_count']}/{tp['rel_count']}) "
                    f"— ПРОПУСК (без потерь)"
                )
                skipped_donors.append(tp)
                continue
            used_findre_ids.update(l["military_id"] for l in findre_leads)
            return_plan.append({
                "donor_mgr_id": tp["mgr_id"],
                "donor_name": tp["mgr_name"],
                "office": tp["office"],
                "findre_leads": findre_leads,
                "mil_count": mc,
                "rel_count": rc,
                "tolerance_used": actual_tol,
            })

        skipped_ids = {t["mgr_id"] for t in skipped_donors}
        take_plan_final = [p for p in take_plan if p["mgr_id"] not in skipped_ids]

        # === Отчёт ===
        print()
        print("=" * 70)
        print("ПЛАН СВАПА")
        print("=" * 70)
        print()
        print(f"Плечо 1: Соне ← dp/ha")
        print(f"{'Донор':<20} {'Office':<6} {'Военных':>8} {'Родств':>8} {'(квота)':>10}")
        for p in take_plan_final:
            print(f"{p['mgr_name']:<20} {p['office']:<6} {p['mil_count']:>8} "
                  f"{p['rel_count']:>8} {p['quota']:>10}")
        print(f"{'ИТОГО':<20} {'':<6} "
              f"{sum(p['mil_count'] for p in take_plan_final):>8} "
              f"{sum(p['rel_count'] for p in take_plan_final):>8}")
        print()
        print(f"Плечо 2: dp/ha ← Findre")
        print(f"{'Донор':<20} {'Office':<6} {'Военных':>8} {'Родств':>8}")
        for p in return_plan:
            print(f"{p['donor_name']:<20} {p['office']:<6} {p['mil_count']:>8} "
                  f"{p['rel_count']:>8}")
        print(f"{'ИТОГО':<20} {'':<6} "
              f"{sum(p['mil_count'] for p in return_plan):>8} "
              f"{sum(p['rel_count'] for p in return_plan):>8}")
        print()
        if skipped_donors:
            print(f"ПРОПУЩЕНО {len(skipped_donors)} доноров (не подобрался Findre):")
            for s in skipped_donors:
                print(f"  {s['mgr_name']}: {s['mil_count']} воен / {s['rel_count']} род")
            print()

        if not apply:
            print("DRY-RUN. Для реального выполнения --apply")
            return

        report_path = Path(f"/root/exports/swap_{datetime.now():%Y%m%d_%H%M%S}.txt")
        report_lines = ["SWAP REPORT", f"Timestamp: {datetime.now().isoformat()}", ""]

        async with conn.transaction():
            for p in take_plan_final:
                military_ids = [l["military_id"] for l in p["leads"]]
                rel_ids = list({rid for l in p["leads"] for rid in l["rel_ids"]})
                await conn.execute("""
                    UPDATE persons_military
                    SET added_by = $1, office = $2, source_id = NULL
                    WHERE id = ANY($3::int[])
                """, SONYA_ID, SONYA_OFFICE, military_ids)
                await conn.execute("""
                    UPDATE relatives
                    SET added_by = $1, office = $2
                    WHERE id = ANY($3::int[])
                """, SONYA_ID, SONYA_OFFICE, rel_ids)
                report_lines.append(
                    f"Плечо 1: от {p['mgr_name']} ({p['office']}) к Соне: "
                    f"military_ids={military_ids}"
                )
                report_lines.append(f"  relative_ids={rel_ids}")

            for p in return_plan:
                military_ids = [l["military_id"] for l in p["findre_leads"]]
                rel_ids = list({rid for l in p["findre_leads"] for rid in l["rel_ids"]})
                await conn.execute("""
                    UPDATE persons_military
                    SET added_by = $1, office = $2, source_id = NULL
                    WHERE id = ANY($3::int[])
                """, p["donor_mgr_id"], p["office"], military_ids)
                await conn.execute("""
                    UPDATE relatives
                    SET added_by = $1, office = $2
                    WHERE id = ANY($3::int[])
                """, p["donor_mgr_id"], p["office"], rel_ids)
                report_lines.append(
                    f"Плечо 2: от Findre к {p['donor_name']} ({p['office']}): "
                    f"military_ids={military_ids}"
                )
                report_lines.append(f"  relative_ids={rel_ids}")

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report_lines))
        logger.info(f"Отчёт сохранён: {report_path}")
        logger.info("Свап применён.")


async def _run(apply: bool):
    try:
        await main(apply)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Применить (без флага — dry-run)")
    args = parser.parse_args()
    asyncio.run(_run(args.apply))
