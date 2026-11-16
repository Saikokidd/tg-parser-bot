#!/usr/bin/env python3
"""
Разбор ha-простыней через Haiku.

Менеджеры ha вставляют пост из канала целиком в поле ФИО. Из-за этого лид
непригоден ни к пробиву, ни к проверке на дубликаты: детектор сравнивает имена,
а там абзац текста. Регулярками берётся меньше половины — даты пишут через
запятые, ФИО стоит посреди фразы.

Отдаём текст модели, просим вернуть строго JSON и ничего не выдумывать.
Разобранное кладём в нормальные поля, исходный текст сохраняем в extra.note.

    venv/bin/python -m tools.ha_parse_texts --limit 40
    venv/bin/python -m tools.ha_parse_texts --limit 40 --apply
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import re

import aiohttp
from dotenv import load_dotenv

from bot.db.queries import get_pool

load_dotenv()

API = "https://api.anthropic.com/v1/messages"
MODEL = os.getenv("HA_PARSE_MODEL", "claude-haiku-4-5")
KEY = os.getenv("ANTHROPIC_API_KEY", "")
BATCH = 20

SYSTEM = (
    "Ты разбираешь объявления о поиске военнослужащих. "
    "Возвращай ТОЛЬКО JSON-массив, без пояснений и без markdown. "
    "Для каждого элемента верни объект с полями: "
    "id (число, как во входных данных), "
    "fio (ФИО самого военнослужащего, которого ищут, в именительном падеже, "
    "или null), "
    "dr (дата рождения военнослужащего в формате ДД.ММ.ГГГГ или null), "
    "callsign (позывной или null), "
    "unit (номер воинской части или подразделение, строкой, или null). "
    "Ничего не додумывай: если в тексте нет значения, ставь null. "
    "Не путай дату рождения с датами ухода на задание и пропажи без вести — "
    "те относятся к 2024-2026 годам. "
    "Если ФИО принадлежит не военнослужащему, а тому, кто его ищет, ставь null."
)


def parse_json(text: str):
    t = text.strip()
    t = re.sub(r'^```(?:json)?|```$', '', t, flags=re.M).strip()
    m = re.search(r'\[.*\]', t, re.S)
    return json.loads(m.group(0)) if m else []


def pdate(s):
    m = re.match(r'(\d{1,2})[.,/-](\d{1,2})[.,/-](\d{4})$', str(s or '').strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1940 <= y <= 2010):
        return None
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


async def ask(session, items):
    payload = {
        "model": MODEL,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{
            "role": "user",
            "content": json.dumps(
                [{"id": i, "text": t} for i, t in items], ensure_ascii=False),
        }],
    }
    async with session.post(API, json=payload, headers={
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, timeout=aiohttp.ClientTimeout(total=120)) as r:
        body = await r.json()
    if "content" not in body:
        raise RuntimeError(str(body)[:300])
    return parse_json("".join(c.get("text", "") for c in body["content"]))


async def run(limit: int, apply: bool, office: str) -> None:
    if not KEY:
        raise SystemExit("ANTHROPIC_API_KEY не задан")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(r"""
            SELECT pm.id, regexp_replace(pm.full_name, '\s+', ' ', 'g') AS t
            FROM persons_military pm
            WHERE pm.office = $2 AND pm.exported_at IS NULL
              AND pm.birth_date IS NULL AND length(pm.full_name) > 60
              AND NOT (pm.extra ? 'inn_checked_at')
              AND NOT EXISTS (SELECT 1 FROM military_relatives mr
                               WHERE mr.military_id = pm.id)
            ORDER BY pm.id
            LIMIT $1
        """, limit, office)

    print(f"текстов: {len(rows)}   офис: {office}   модель: {MODEL}")
    texts = {r['id']: r['t'] for r in rows}
    items = list(texts.items())

    got, stats = [], {'фио+дата': 0, 'только фио': 0, 'только дата': 0, 'пусто': 0}
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            try:
                res = await ask(session, chunk)
            except Exception as e:
                print(f"  пачка {i // BATCH + 1}: ошибка {str(e)[:120]}")
                continue
            for o in res:
                lid = o.get('id')
                if lid not in texts:
                    continue
                fio = (o.get('fio') or '').strip() or None
                # Женское отчество у «военного» — почти всегда модель взяла
                # того, кто ищет, а не того, кого ищут.
                if fio and re.search(r'(овна|евна|ична|инична)\s*$', fio, re.I):
                    fio = None
                d = pdate(o.get('dr'))
                if fio and d:
                    stats['фио+дата'] += 1
                elif fio:
                    stats['только фио'] += 1
                elif d:
                    stats['только дата'] += 1
                else:
                    stats['пусто'] += 1
                got.append((lid, fio, d, (o.get('callsign') or '').strip() or None,
                            (o.get('unit') or '').strip() or None))
            print(f"  разобрано {min(i + BATCH, len(items))} из {len(items)}",
                  flush=True)

    for k, v in stats.items():
        print(f"{v:6d}  {k}")

    sample = [g for g in got if g[1] and g[2]][:8]
    print("\nпримеры:")
    for lid, fio, d, cs, unit in sample:
        print(f"  {lid}  {fio}  {d}  позывной={cs}  вч={unit}")

    if not apply:
        print("\nСухой прогон. Для записи добавь --apply")
        return

    wrote = 0
    async with pool.acquire() as conn:
        for lid, fio, d, cs, unit in got:
            if not (fio and d):
                continue
            patch = {"note": texts[lid]}
            if cs:
                patch["callsign"] = cs
            if unit:
                patch["unit"] = unit
            await conn.execute("""
                UPDATE persons_military
                SET full_name = $2, birth_date = $3,
                    extra = COALESCE(extra, '{}'::jsonb) || $4::text::jsonb
                WHERE id = $1
            """, lid, fio, d, json.dumps(patch, ensure_ascii=False))
            wrote += 1
    print(f"\nзаписано: {wrote}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=3000)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--office", default="ha")
    a = p.parse_args()
    asyncio.run(run(a.limit, a.apply, a.office))


if __name__ == "__main__":
    main()
