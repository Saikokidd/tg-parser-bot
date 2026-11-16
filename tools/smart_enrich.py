#!/usr/bin/env python3
"""
Умное обогащение родственников.

На каждого: query_person → шаблон (адрес, СНИЛС, ИНН, паспорт) и кандидаты
номеров. Каждый кандидат проверяется query_phone и оценивается по источнику:
банк или госуслуги с совпадающим ФИО — высший балл, мессенджер — средний,
остальное отбрасывается. Пишем только лучший номер.

Прогон прерываемый: результат каждого человека пишется сразу, а отметка
в enrich_done ставится ТОЛЬКО при настоящем исходе. Сетевой сбой отметки
не оставляет, поэтому после перезапуска такой человек вернётся в работу.

    venv/bin/python -m tools.smart_enrich --limit 20
    venv/bin/python -m tools.smart_enrich --limit 20 --apply
    venv/bin/python -m tools.smart_enrich --apply --budget 60
"""
import argparse
import asyncio
import json
import re
from collections import Counter

from bot.db.queries import get_pool, insert_probiv_log
from bot.parser import sauron_parser
from bot.services.sauron_api import query_person, query_phone

OWNER, OFFICE = 111, "pvl"
CONCURRENCY = 3
MAX_FAILS = 10          # столько сбоев подряд считаем падением сервиса

BANK = ['alfabank', 'sberbank', 'мобильный банк', 'русский стандарт', 'совкомбанк',
        'тинькофф', 'втб', 'газпромбанк', 'spasibosberbank', 'райффайзен',
        'росбанк', 'почта банк', 'озон банк']
GOS = ['gosuslugi']
MSG = ['telegram', 'whatsapp', 'viber']


class Ctl:
    """Общее состояние прогона: траты, сбои, остановка."""

    def __init__(self, budget):
        self.budget = budget
        self.spent = 0.0
        self.calls = 0
        self.fails = 0
        self.done = 0
        self.total = 0
        self.stop = False
        self.reason = ''

    def pay(self, cost):
        self.spent += cost
        self.calls += 1
        if self.spent >= self.budget:
            self.halt('выбран потолок трат $%.2f' % self.budget)

    def good(self):
        self.fails = 0

    def bad(self):
        self.fails += 1
        if self.fails >= MAX_FAILS:
            self.halt('%d сбоев подряд — похоже, сервис недоступен' % self.fails)

    def halt(self, why):
        if not self.stop:
            self.stop = True
            self.reason = why
            print('\n!!! ОСТАНОВКА: %s\n' % why, flush=True)


def year_ok(src):
    m = re.search(r'20(\d\d)', src or '')
    return int('20' + m.group(1)) >= 2023 if m else True


def fio_match(a, b):
    x, y = str(a or '').lower().split(), str(b or '').lower().split()
    return len(x) >= 2 and len(y) >= 2 and x[0] == y[0] and x[1] == y[1]


def score_phone(recs, fio):
    live = strong = False
    for rec in recs:
        src = str(rec.get('Источник', '')).lower()
        if not year_ok(src):
            continue
        bank = any(k in src for k in BANK)
        gos = any(k in src for k in GOS)
        if bank or any(k in src for k in MSG):
            live = True
        if (bank or gos) and fio_match(fio, rec.get('ФИО', '')):
            strong = True
    return 2 if strong else (1 if live else 0)


async def pay_log(ctl, res, fio, bd):
    cost = float(res.get('cost') or 0) if isinstance(res, dict) else 0.0
    ctl.pay(cost)
    await insert_probiv_log(provider="sauron", context="tool", manager_id=None,
                            full_name=str(fio)[:255], birth_date=bd, cost=cost,
                            currency="USD", success=True, office=OFFICE)


async def save(pool, rid, phone, tpl, result):
    """Записать результат и пометить обработанным. Только при настоящем исходе."""
    patch = {k: (tpl or {}).get(k) for k in ('snils', 'inn', 'passport', 'email')
             if (tpl or {}).get(k)}
    addr = (tpl or {}).get('address') or ''
    async with pool.acquire() as conn:
        async with conn.transaction():
            if phone:
                await conn.execute(
                    """
                    UPDATE relatives
                    SET phone = $2,
                        address = COALESCE(NULLIF($3, ''), address),
                        extra = COALESCE(extra, '{}'::jsonb) || $4::text::jsonb,
                        updated_at = now()
                    WHERE id = $1
                    """, rid,
                    phone if str(phone).startswith('+') else '+' + str(phone),
                    addr, json.dumps(patch, ensure_ascii=False))
            elif patch or addr:
                await conn.execute(
                    """
                    UPDATE relatives
                    SET address = COALESCE(NULLIF($2, ''), address),
                        extra = COALESCE(extra, '{}'::jsonb) || $3::text::jsonb,
                        updated_at = now()
                    WHERE id = $1
                    """, rid, addr, json.dumps(patch, ensure_ascii=False))
            await conn.execute(
                "INSERT INTO enrich_done (relative_id, result) VALUES ($1, $2) "
                "ON CONFLICT (relative_id) DO NOTHING", rid, result)


async def enrich(pool, rel, sem, ctl, stats):
    async with sem:
        if ctl.stop:
            return
        rid, fio, bd = rel['id'], rel['full_name'], rel['birth_date']
        p = str(fio).split()

        try:
            res = await query_person(
                lastname=p[0] if p else '', firstname=p[1] if len(p) > 1 else '',
                middlename=p[2] if len(p) > 2 else '',
                day=bd.day if bd else None, month=bd.month if bd else None,
                year=bd.year if bd else None, office=OFFICE)
        except Exception as e:
            # Сервис не ответил. Отметку НЕ ставим — человек вернётся
            # в следующий прогон.
            stats['сбой запроса'] += 1
            ctl.bad()
            return
        ctl.good()
        await pay_log(ctl, res, fio, bd)

        records = res.get('response', {}).get('result', []) if isinstance(res, dict) else []
        if not records:
            stats['нет данных'] += 1
            await save(pool, rid, None, None, 'нет данных')
            ctl.done += 1
            return

        try:
            tpl = sauron_parser.build_relative_template(res)
            cands = sauron_parser._pick_phone_candidates(records) or []
        except Exception as e:
            stats['разбор не удался'] += 1
            await save(pool, rid, None, None, 'разбор не удался: %s' % str(e)[:80])
            ctl.done += 1
            return

        if not cands:
            stats['нет номеров'] += 1
            await save(pool, rid, None, tpl, 'нет номеров')
            ctl.done += 1
            return

        best, best_sc, checked = None, -1, 0
        for ph in cands:
            if ctl.stop:
                break
            try:
                pr = await query_phone(ph, office=OFFICE)
            except Exception:
                ctl.bad()
                continue
            ctl.good()
            await pay_log(ctl, pr, ph, None)
            checked += 1
            try:
                sc = score_phone(pr.get('response', {}).get('result', []), fio)
            except Exception:
                sc = 0
            if sc > best_sc:
                best_sc, best = sc, ph
            if sc == 2:
                break

        if checked == 0:
            # Ни один номер проверить не удалось — исхода нет, отметку не ставим.
            stats['номера не проверились'] += 1
            return

        if best_sc >= 1:
            stats['номер найден'] += 1
            stats['  высший балл' if best_sc == 2 else '  средний балл'] += 1
            await save(pool, rid, best, tpl, 'ok score=%d' % best_sc)
        else:
            stats['номера слабые'] += 1
            await save(pool, rid, None, tpl, 'слабые номера')

        ctl.done += 1
        if ctl.done % 50 == 0:
            print('  обработано %d из %d, запросов %d, потрачено $%.2f'
                  % (ctl.done, ctl.total, ctl.calls, ctl.spent), flush=True)


async def run(limit, apply, budget):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT r.id, r.full_name, r.birth_date
            FROM relatives r
            JOIN military_relatives mr ON mr.relative_id = r.id
            JOIN persons_military pm ON pm.id = mr.military_id
            WHERE pm.added_by = $1 AND pm.exported_at IS NULL
              AND COALESCE(r.phone, '') = ''
              AND NOT EXISTS (SELECT 1 FROM enrich_done e WHERE e.relative_id = r.id)
            ORDER BY r.id
            LIMIT $2
            """, OWNER, limit)

    print(f"к обогащению: {len(rows)}   потолок трат: ${budget}", flush=True)
    if not apply:
        print("Сухой прогон. Для записи добавь --apply")
        return

    stats = Counter()
    ctl = Ctl(budget)
    ctl.total = len(rows)
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*[enrich(pool, r, sem, ctl, stats) for r in rows],
                         return_exceptions=True)

    print("\n=== ИТОГ ===")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"{v:6d}  {k}")
    print(f"\nобработано и помечено: {ctl.done} из {ctl.total}")
    print(f"запросов в Саурон: {ctl.calls}   потрачено ${ctl.spent:.2f}")
    if ctl.stop:
        print(f"прогон остановлен: {ctl.reason}")
        print("непомеченные вернутся в работу при следующем запуске")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--budget", type=float, default=60.0)
    a = p.parse_args()
    asyncio.run(run(a.limit, a.apply, a.budget))


if __name__ == "__main__":
    main()
