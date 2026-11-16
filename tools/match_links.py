#!/usr/bin/env python3
"""
Матч связей Саурона к незаполненным лидам Сони.

Фильтр отбирает настоящую родню по отчеству, фамилии и возрасту, потому что
Саурон связывает людей по фамилии и адресу и регулярно записывает в сёстры
однофамилиц, а в отцы — братьев.

    venv/bin/python -m tools.match_links /root/exports/файл.xlsx
    venv/bin/python -m tools.match_links /root/exports/файл.xlsx --apply
"""
import argparse
import asyncio
import datetime as dt
import json
import re
from collections import Counter, defaultdict

from openpyxl import load_workbook

from bot.db.queries import get_pool

SONYA, OFFICE = 111, "pvl"

PATR_SUF = ('инична', 'ична', 'овична', 'евична', 'ьевна', 'овна', 'евна',
            'ьевич', 'иевич', 'ович', 'евич', 'ич')
NAME_SUF = ('ий', 'ей', 'ай', 'й', 'я', 'а')
FEM_SUF = ('овна', 'евна', 'ична', 'инична', 'ьевна')


def norm(s):
    s = str(s or '').lower().replace('ё', 'е')
    s = re.sub(r'[^а-яa-z\s-]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def parts(fio):
    return (norm(fio).split() + ['', '', ''])[:3]


def _strip(w, sufs):
    for s in sufs:
        if w.endswith(s) and len(w) - len(s) >= 2:
            return w[:-len(s)]
    return w


def patr_stem(p):
    return _strip(norm(p), PATR_SUF)


def name_stem(n):
    return _strip(norm(n), NAME_SUF)


def canon(s):
    return s.replace('ь', '').replace('й', 'и')


def lev(a, b):
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
        if min(prev) > 1:
            return 2
    return prev[-1]


def close(a, b):
    """Основы совпадают с точностью до беглой гласной, й/и или мягкого знака."""
    if not a or not b:
        return None
    ca, cb = canon(a), canon(b)
    if ca == cb:
        return True
    if min(len(ca), len(cb)) <= 3:
        return False
    return lev(ca, cb) <= 1


def patr_from_name(patr, name):
    return close(patr_stem(patr), name_stem(name))


def is_female(patr, name):
    p = norm(patr)
    if any(p.endswith(s) for s in FEM_SUF):
        return True
    if p.endswith('ич'):
        return False
    return norm(name).endswith(('а', 'я')) if name else None


def pdate(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', str(v or '').strip())
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def page(v):
    """Возраст приходит то числом, то строкой, то пустым."""
    try:
        return float(str(v).replace(',', '.').strip())
    except (TypeError, ValueError):
        return None


def classify(subj_fio, subj_dr, rel_fio, rel_dr, role_raw, age):
    """→ (роль, приоритет) либо (None, причина отсева)."""
    role = norm(role_raw)
    if role == 'нет связей':
        return None, 'нет связей'
    sf, sn, sp = parts(subj_fio)
    rf, rn, rp = parts(rel_fio)
    if (sf, sn, sp) == (rf, rn, rp):
        return None, 'сам субъект'
    if len(rn) < 2 or len(rf) < 2:
        return None, 'ФИО неполное'

    d = ((subj_dr - rel_dr).days / 365.25) if (subj_dr and rel_dr) else None
    hd = d is not None
    fem = is_female(rp, rn)

    same_patr = bool(sp) and bool(rp) and close(patr_stem(sp), patr_stem(rp)) is True
    child_patr = patr_from_name(rp, sn) is True
    father_patr = patr_from_name(sp, rn) is True
    older = hd and 16 <= d <= 55
    younger = (not hd) or (hd and -60 <= d <= -16)
    peer = (not hd) or abs(d) <= 25

    if same_patr and peer and role not in ('брат', 'сестра'):
        role = 'сестра' if fem else 'брат'
    elif child_patr and younger and role not in ('сын', 'дочь'):
        role = 'дочь' if fem else 'сын'
    elif role in ('старший родственник', 'старшее поколение', 'связь',
                  'младший родственник'):
        if father_patr and older:
            role = 'отец'
        elif older and fem:
            role = 'мать'
        elif younger and child_patr:
            role = 'дочь' if fem else 'сын'
        else:
            return None, 'роль не определилась'

    if role in ('мать', 'отец'):
        if role == 'отец' and patr_from_name(sp, rn) is False:
            return None, 'отец: имя не от отчества субъекта'
        if age is not None and age > 70:
            return None, 'родитель старше 70'
        if hd and not (16 <= d <= 55):
            return None, 'родитель вне вилки'
        return role, 1

    if role.startswith('супруг'):
        if hd and abs(d) > 20:
            return None, 'супруг: разница больше 20'
        return 'супруга' if fem else 'супруг', 1

    if role in ('сын', 'дочь'):
        if patr_from_name(rp, sn) is False:
            return None, 'ребёнок: отчество не от имени субъекта'
        if hd and not (-60 <= d <= -16):
            return None, 'ребёнок вне вилки'
        return role, (1 if (age is not None and age >= 19) else 2)

    if role in ('брат', 'сестра'):
        if sp and rp and close(patr_stem(sp), patr_stem(rp)) is False:
            return None, 'брат/сестра: разные отчества'
        if hd and abs(d) > 25:
            return None, 'брат/сестра: разница больше 25'
        return role, 2

    if role in ('бабушка', 'дедушка'):
        if age is not None and age > 70:
            return None, 'бабушка/дедушка старше 70'
        if hd and d < 35:
            return None, 'бабушка/дедушка: мало разницы'
        return role, 2

    if role in ('внук', 'внучка'):
        if hd and d > -35:
            return None, 'внук: мало разницы'
        return role, 2

    return None, f'прочее: {role}'


async def main(path: str, apply: bool) -> None:
    ws = load_workbook(path, read_only=True, data_only=True)['Связи']
    rows = ws.iter_rows(min_row=2, values_only=True)

    good = defaultdict(list)       # (норм_фио, дата) -> [(фио, др, роль, приоритет)]
    stats, seen = Counter(), set()
    for subj, sdr, fio, rdr, age, role, _grp in rows:
        key = (subj, sdr, fio, rdr, role)
        if key in seen:
            stats['дубль строки'] += 1
            continue
        seen.add(key)
        sd, rd = pdate(sdr), pdate(rdr)
        r, p = classify(subj, sd, fio, rd, role, page(age))
        if r is None:
            stats['отсев: ' + p] += 1
            continue
        stats[f'принят приоритет {p}'] += 1
        good[(norm(subj), sd)].append((str(fio).strip(), rd, r, p))

    print(f'субъектов со связями: {len(good)}')
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f'{v:6d}  {k}')

    pool = await get_pool()
    async with pool.acquire() as conn:
        leads = await conn.fetch(
            """
            SELECT pm.id, pm.full_name, pm.birth_date
            FROM persons_military pm
            WHERE pm.added_by = $1 AND pm.exported_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM military_relatives mr
                               WHERE mr.military_id = pm.id)
            """, SONYA)

    index, ambiguous = {}, set()
    for l in leads:
        k = (norm(l['full_name']), l['birth_date'])
        if k in index:
            ambiguous.add(k)
        index[k] = l['id']

    plan = []
    miss = 0
    for k, rels in good.items():
        if k in ambiguous:
            stats['субъект: несколько лидов с таким ФИО+ДР'] += 1
            continue
        mid = index.get(k)
        if mid is None:
            miss += 1
            continue
        for fio, rd, role, prio in rels:
            plan.append((mid, fio, rd, role))

    print(f'\nлидов Сони пустых: {len(leads)}')
    print(f'субъектов не нашлось среди них: {miss}')
    print(f'лидов получат родню: {len({p[0] for p in plan})}')
    print(f'родственников к вставке: {len(plan)}')

    # пересечения с уже существующими родственниками
    async with pool.acquire() as conn:
        dups = 0
        owners = Counter()
        dup_rows = []
        for mid, fio, rd, role in plan:
            row = await conn.fetchrow(
                """
                SELECT r.id, m.name AS owner, r.office
                FROM relatives r LEFT JOIN managers m ON m.id = r.added_by
                WHERE lower(r.full_name) = lower($1)
                  AND r.birth_date IS NOT DISTINCT FROM $2
                LIMIT 1
                """, fio, rd)
            if row:
                dups += 1
                owners[f"{row['owner']} / {row['office']}"] += 1
                info = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS лидов,
                           COUNT(*) FILTER (WHERE p.exported_at IS NOT NULL) AS выгруженных,
                           COUNT(*) FILTER (WHERE p.added_by = 111) AS уже_сонины
                    FROM military_relatives mr
                    JOIN persons_military p ON p.id = mr.military_id
                    WHERE mr.relative_id = $1
                    """, row['id'])
                dup_rows.append((row['id'], fio, str(rd or ''), row['owner'],
                                 row['office'], info['лидов'],
                                 info['выгруженных'], info['уже_сонины']))
        print(f'\nуже есть в базе: {dups}')
        for k, v in owners.most_common(10):
            print(f'   {v:5d}  {k}')
        import csv as _csv
        with open('/root/exports/match_dups.csv', 'w', encoding='utf-8', newline='') as f:
            w = _csv.writer(f)
            w.writerow(['relative_id', 'фио', 'др', 'владелец', 'офис',
                        'лидов', 'выгруженных', 'уже_сонины'])
            w.writerows(dup_rows)
        print('   детали: /root/exports/match_dups.csv')
        print(f"   из них висят больше чем на одном лиде: "
              f"{sum(1 for r in dup_rows if r[5] > 1)}")
        print(f"   привязаны к выгруженным лидам: "
              f"{sum(1 for r in dup_rows if r[6] > 0)}")

    if not apply:
        print('\nСухой прогон. Для записи добавь --apply')
        return

    ins_new = ins_link = reused = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for mid, fio, rd, role in plan:
                # Существующие записи не переиспользуем: все они привязаны
                # к выгруженным лидам чужих офисов. Общая родня ломает переносы
                # и счёт, поэтому у Сони заводим свою запись.
                if True:
                    rid = await conn.fetchval(
                        """
                        INSERT INTO relatives (full_name, birth_date, office,
                                               added_by, extra)
                        VALUES ($1, $2, $3, $4, $5::text::jsonb)
                        RETURNING id
                        """, fio, rd, OFFICE, SONYA,
                        json.dumps({'role': role}, ensure_ascii=False))
                    ins_new += 1
                res = await conn.execute(
                    """
                    INSERT INTO military_relatives (military_id, relative_id, added_by)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (military_id, relative_id) DO NOTHING
                    """, mid, rid, SONYA)
                if res.endswith('1'):
                    ins_link += 1

    print(f'\nсоздано родственников: {ins_new}')
    print(f'переиспользовано существующих: {reused}')
    print(f'создано связей: {ins_link}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('xlsx')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    asyncio.run(main(a.xlsx, a.apply))
