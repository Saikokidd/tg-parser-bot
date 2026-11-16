#!/usr/bin/env python3
"""
Сборка базы по недействительным ИНН.

Берёт незаполненных военных (без привязанной родни) с датой рождения,
от самых свежих. Пробивает ИНН в Сауроне, проверяет в реестре ФНС.
Подошёл — переносим Соне, не подошёл — Драйверу. Результат пишем в extra,
чтобы второй раз за того же человека не платить.

    venv/bin/python -m tools.inn_harvest --limit 300
    venv/bin/python -m tools.inn_harvest --limit 300 --apply
"""
import argparse
import asyncio
import datetime as dt
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request

from bot.db.queries import get_pool, insert_probiv_log
from bot.services.sauron_api import query_person, split_full_name

SONYA, DRIVER = 111, 139
OFFICE = "pvl"
GOOD_YEARS = {2025, 2026}
OUT = "/root/exports/sonya_base.txt"
PAGE = "https://service.nalog.ru/invalid-inn-fl.html"
API = "https://service.nalog.ru/invalid-inn-proc.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
PAUSE_SAURON, PAUSE_FNS = 1.0, 0.4

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", UA)]


def valid_inn(d: str) -> bool:
    if len(d) != 12 or not d.isdigit():
        return False
    n = [int(c) for c in d]
    c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k11 = sum(a * b for a, b in zip(c11, n[:10])) % 11 % 10
    k12 = sum(a * b for a, b in zip(c12, n[:11])) % 11 % 10
    return k11 == n[10] and k12 == n[11]


def collect_inn(node, found: set) -> None:
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


def _fns_once(inn: str) -> dict:
    body = urllib.parse.urlencode({"k": "fl", "inn": inn}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
        "Referer": PAGE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fns_date(inn: str):
    """Дата аннулирования или None. Сессия ФНС протухает — обновляем и повторяем."""
    for attempt in (1, 2):
        try:
            resp = _fns_once(inn)
            raw = resp.get("date")
            return dt.datetime.strptime(raw.split()[0], "%d.%m.%Y").date() if raw else None
        except Exception:
            if attempt == 2:
                raise
            opener.open(PAGE, timeout=30).read()
    return None


async def run(limit: int, apply: bool,
              select_office: str, pay_office: str) -> None:
    opener.open(PAGE, timeout=30).read()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            r"""
            SELECT pm.id,
                   regexp_replace(pm.full_name, '\s+', ' ', 'g') AS full_name,
                   pm.birth_date
            FROM persons_military pm
            WHERE pm.office = $1
              AND pm.birth_date IS NOT NULL
              AND pm.exported_at IS NULL
              AND COALESCE(pm.extra->>'inn_checked_at', '') = ''
              AND NOT EXISTS (SELECT 1 FROM military_relatives mr
                               WHERE mr.military_id = pm.id)
            ORDER BY pm.created_at DESC
            LIMIT $2
            """,
            select_office, limit,
        )

    work = []
    for r in rows:
        last, first, middle = split_full_name(r["full_name"])
        if last and first:
            work.append((r, last, first, middle))

    print(f"Отобрано {len(rows)}, пригодны к пробиву {len(work)}")
    if not apply:
        print("Сухой прогон. Для записи добавь --apply")
        return

    today = dt.date.today()
    to_sonya, to_driver, spent, errs = [], 0, 0.0, 0

    for i, (r, last, first, middle) in enumerate(work, 1):
        bd = r["birth_date"]
        inns, best = set(), None
        try:
            res = await query_person(last, first, middle, day=bd.day,
                                     month=bd.month, year=bd.year, office=pay_office)
            collect_inn(res.get("response", {}).get("result", []), inns)
            cost = float(res.get("cost") or 0)
            spent += cost
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=cost,
                currency="USD", military_id=r["id"], success=True, office=pay_office)
        except Exception as e:
            errs += 1
            await insert_probiv_log(
                provider="sauron", context="tool", manager_id=None,
                full_name=r["full_name"][:255], birth_date=bd, cost=0,
                currency="USD", military_id=r["id"], success=False,
                error=str(e), office=pay_office)
            print(f"  [{i}/{len(work)}] саурон упал id={r['id']}: {e}")
            await asyncio.sleep(PAUSE_SAURON)
            continue

        dates = {}
        for inn in sorted(inns):
            try:
                d = await asyncio.to_thread(fns_date, inn)
            except Exception as e:
                print(f"  фнс упал {inn}: {e}")
                d = None
            if d:
                dates[inn] = d
            await asyncio.sleep(PAUSE_FNS)

        if dates:
            best = max(dates.values())          # берём самую позднюю
        good = best is not None and best.year in GOOD_YEARS
        owner = SONYA if good else DRIVER

        patch = {
            "inn_checked_at": today.isoformat(),
            "inn": ", ".join(sorted(inns)) if inns else "",
            "inn_invalid_date": best.isoformat() if best else "",
        }
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE persons_military
                SET extra = COALESCE(extra, '{}'::jsonb) || $2::text::jsonb,
                    added_by = $3, office = $4
                WHERE id = $1
                """,
                r["id"], json.dumps(patch, ensure_ascii=False), owner, OFFICE)

        if good:
            to_sonya.append(f"{r['full_name']} {bd.strftime('%d.%m.%Y')}")
        else:
            to_driver += 1

        mark = "СОНЕ" if good else "драйверу"
        print(f"  [{i}/{len(work)}] id={r['id']:<7} инн:{len(inns)} "
              f"дата:{best or '—'} → {mark}   ${spent:.2f}")
        await asyncio.sleep(PAUSE_SAURON)

    with open(OUT, "a", encoding="utf-8") as f:
        for line in to_sonya:
            f.write(line + "\n")

    print(f"\nСоне: {len(to_sonya)}   Драйверу: {to_driver}   "
          f"ошибок саурона: {errs}   потрачено ${spent:.2f}")
    print(f"Дописано в {OUT}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--office", default="pvl",
                   help="чьи лиды берём: pvl / dp / ha")
    p.add_argument("--pay-office", default="pvl",
                   help="с какого счёта Саурона платим")
    a = p.parse_args()
    asyncio.run(run(a.limit, a.apply, a.office, a.pay_office))


if __name__ == "__main__":
    main()
