"""
Отчёт по расходам на Sauron из исторических логов.

Парсит все logs/bot.log*, сопоставляет ФИО с менеджерами через БД
и выводит сводку: кто сколько потратил.

Использование:
    cd ~/projects/tg-parser-bot
    venv/bin/python -m tools.sauron_cost_report

Опции:
    --since YYYY-MM-DD  — только записи начиная с даты
    --until YYYY-MM-DD  — только записи до даты (включительно)
"""
import asyncio
import argparse
import re
import sys
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.db.connection import get_pool, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sauron_cost_report")

LOG_DIR = PROJECT_ROOT / "logs"


# ──────────── Регулярки для парсинга ────────────

# Строка лога целиком, чтобы извлечь timestamp:
# "2026-05-26 10:34:57 [INFO] bot.services.sauron_api: ..."
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[\w+\]\s+([\w.]+):\s*(.*)$"
)

# Строка "пробив": "Sauron: пробив ФАМ ИМЯ ОТЧ ДД.ММ.ГГГГ"
# или без даты: "Sauron: пробив ФАМ ИМЯ"
PROBIV_RE = re.compile(
    r"^Sauron:\s*пробив\s+(.+?)(?:\s+(\d{2}\.\d{2}\.\d{4}))?\s*$"
)

# Строка стоимости — поддерживает оба формата ($ и ₽):
# "Sauron: cost=$0.03 balance=$273.16 records=49"
# "Sauron: cost=0.03₽ balance=273.16₽ records=49"
COST_RE = re.compile(
    r"^Sauron:\s*cost=\$?([\d.]+)[$₽]?\s+balance=\$?[\d.]+[$₽]?\s+records=\d+"
)


# ──────────── Парсинг логов ────────────

def parse_log_files(log_files: list[Path], since: datetime = None, until: datetime = None) -> list[dict]:
    """
    Прочитать все указанные лог-файлы, вернуть список запросов:
    [{timestamp, full_name, birth_date_str, cost}, ...]

    Логика: проходим построчно, в каждой строке ищем cost. Когда нашли —
    смотрим последнее зафиксированное ФИО (из строки "Sauron: пробив").
    """
    requests = []
    last_probiv = None  # (full_name, birth_date_str)

    for log_file in log_files:
        logger.info(f"Читаю {log_file.name}")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    m = LINE_RE.match(raw.rstrip())
                    if not m:
                        continue
                    ts_str, _module, msg = m.groups()

                    # Фильтр по дате
                    try:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if since and ts < since:
                        continue
                    if until and ts > until:
                        continue

                    # Строка "пробив"
                    pm = PROBIV_RE.match(msg)
                    if pm:
                        last_probiv = (pm.group(1).strip(), pm.group(2) or "")
                        continue

                    # Строка "cost"
                    cm = COST_RE.match(msg)
                    if cm and last_probiv:
                        cost = float(cm.group(1))
                        requests.append({
                            "timestamp": ts,
                            "full_name": last_probiv[0],
                            "birth_date_str": last_probiv[1],
                            "cost": cost,
                        })
                        last_probiv = None  # сбрасываем чтобы не приклеить к следующему cost
        except FileNotFoundError:
            logger.warning(f"  Не найден: {log_file}")
            continue
        except Exception as e:
            logger.warning(f"  Ошибка чтения {log_file}: {e}")
            continue

    return requests


# ──────────── Сопоставление с менеджерами ────────────

async def build_name_to_manager_map() -> dict:
    """
    Построить отображение LOWER(full_name) → manager_id.

    Берём из persons_military и из relatives.
    Если одно ФИО у двух разных менеджеров — оставляем того, у кого
    больше записей (самая ранняя запись по created_at не так важна,
    т.к. для исторического отчёта мы просто хотим грубую разнесёнку).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Считаем сколько раз каждое (LOWER(full_name), manager_id) встречается
        military_rows = await conn.fetch(
            """
            SELECT LOWER(TRIM(full_name)) AS norm_name,
                   added_by AS manager_id,
                   COUNT(*) AS cnt
            FROM persons_military
            WHERE added_by IS NOT NULL
            GROUP BY LOWER(TRIM(full_name)), added_by
            """
        )
        relatives_rows = await conn.fetch(
            """
            SELECT LOWER(TRIM(full_name)) AS norm_name,
                   added_by AS manager_id,
                   COUNT(*) AS cnt
            FROM relatives
            WHERE added_by IS NOT NULL
            GROUP BY LOWER(TRIM(full_name)), added_by
            """
        )

    # Складываем counts: для каждого имени получаем dict {manager_id: count}
    name_managers = defaultdict(lambda: defaultdict(int))
    for r in military_rows:
        name_managers[r["norm_name"]][r["manager_id"]] += r["cnt"]
    for r in relatives_rows:
        name_managers[r["norm_name"]][r["manager_id"]] += r["cnt"]

    # Выбираем самого "массового" менеджера для каждого имени
    result = {}
    for name, mgr_counts in name_managers.items():
        best_mgr = max(mgr_counts.items(), key=lambda x: x[1])[0]
        result[name] = best_mgr
    return result


async def get_manager_names() -> dict:
    """{manager_id: name} для красивого вывода"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM managers")
    return {r["id"]: r["name"] for r in rows}


# ──────────── Главная логика ────────────

async def main(since: datetime = None, until: datetime = None, show_unmatched: bool = False):
    # 1. Находим лог-файлы
    log_files = sorted(LOG_DIR.glob("bot.log*"))
    if not log_files:
        logger.error(f"Нет лог-файлов в {LOG_DIR}")
        return

    logger.info(f"Найдено лог-файлов: {len(log_files)}")

    # 2. Парсим
    requests = parse_log_files(log_files, since=since, until=until)
    logger.info(f"Всего запросов в логах: {len(requests)}")
    if not requests:
        await close_pool()
        return

    # 3. Строим карту "имя → manager_id"
    logger.info("Строю карту ФИО → менеджер из БД...")
    name_to_mgr = await build_name_to_manager_map()
    manager_names = await get_manager_names()
    await close_pool()
    logger.info(f"В БД найдено {len(name_to_mgr)} уникальных ФИО")

    # 4. Агрегируем
    by_manager = defaultdict(lambda: {"count": 0, "cost": 0.0})
    unmatched = {"count": 0, "cost": 0.0}

    for req in requests:
        norm = req["full_name"].lower().strip()
        mgr_id = name_to_mgr.get(norm)
        if mgr_id is None:
            unmatched["count"] += 1
            unmatched["cost"] += req["cost"]
        else:
            by_manager[mgr_id]["count"] += 1
            by_manager[mgr_id]["cost"] += req["cost"]

    # 5. Период — берём min/max timestamp из реально найденных запросов
    timestamps = [r["timestamp"] for r in requests]
    period_start = min(timestamps).date()
    period_end = max(timestamps).date()

    # 6. Печать
    print()
    print("=" * 60)
    print("Расход на пробив по менеджерам (исторические логи)")
    print(f"Период: {period_start} — {period_end}")
    print("=" * 60)
    print()

    # Сортируем менеджеров по убыванию расхода
    sorted_mgrs = sorted(by_manager.items(), key=lambda x: x[1]["cost"], reverse=True)

    if not sorted_mgrs:
        print("Ни один запрос не привязан к менеджеру.")
    else:
        for mgr_id, stats in sorted_mgrs:
            name = manager_names.get(mgr_id, f"(id={mgr_id})")
            print(f"  {name:30} | {stats['count']:6} запросов | ${stats['cost']:8.2f}")

    print()
    print("─" * 60)
    total_count = sum(s["count"] for s in by_manager.values()) + unmatched["count"]
    total_cost = sum(s["cost"] for s in by_manager.values()) + unmatched["cost"]
    print(f"  {'Итого:':30} | {total_count:6} запросов | ${total_cost:8.2f}")

    if unmatched["count"]:
        print()
        print(f"  (не привязано к менеджеру: "
              f"{unmatched['count']} запросов | ${unmatched['cost']:.2f})")
    print()

    # Опциональный детальный лог unmatched
    if show_unmatched:
        print()
        print("=" * 60)
        print("ТОП-30 НЕСМАТЧЕННЫХ ФИО (по частоте)")
        print("=" * 60)
        unmatched_names = defaultdict(int)
        for req in requests:
            norm = req["full_name"].lower().strip()
            if name_to_mgr.get(norm) is None:
                unmatched_names[req["full_name"]] += 1

        top = sorted(unmatched_names.items(), key=lambda x: x[1], reverse=True)[:30]
        for name, cnt in top:
            print(f"  {cnt:4}x  {name}")
        print()


# ──────────── CLI ────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=str, default=None,
                        help="Начало периода YYYY-MM-DD (включительно)")
    parser.add_argument("--until", type=str, default=None,
                        help="Конец периода YYYY-MM-DD (включительно)")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="Показать ФИО которые не сматчились с менеджером (топ-30)")
    args = parser.parse_args()

    since = None
    until = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    if args.until:
        until = datetime.strptime(args.until, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )

    asyncio.run(main(since=since, until=until, show_unmatched=args.show_unmatched))
