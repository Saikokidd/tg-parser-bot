#!/usr/bin/env python3
"""Разбор ha-простыней регулярками и проверка на дубликаты в базе."""
import asyncio
import datetime as dt
import re
from collections import Counter

from bot.db.queries import get_pool

PATR = re.compile(r'^[А-ЯЁа-яё-]+(ович|евич|ьевич|ич|овна|евна|ьевна|ична|инична)$',
                  re.IGNORECASE)
DATE = re.compile(r'\b(\d{1,2})[.,/\s-](\d{1,2})[.,/\s-](\d{4})\b')
WORD = re.compile(r'[А-ЯЁа-яё][А-ЯЁа-яё-]{1,}')


def norm(s):
    s = str(s or '').lower().replace('ё', 'е')
    s = re.sub(r'[^а-я\s-]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def grab_date(text):
    """Дата рождения: год 1940–2008. Даты БЗ и БП (2024+) отбрасываем."""
    for d, m, y in DATE.findall(text):
        y, m, d = int(y), int(m), int(d)
        if 1940 <= y <= 2008 and 1 <= m <= 12 and 1 <= d <= 31:
            try:
                return dt.date(y, m, d)
            except ValueError:
                continue
    return None


def grab_fio(text):
    """Фамилия Имя Отчество — якорь по отчеству."""
    w = WORD.findall(text)
    for i, token in enumerate(w):
        if i >= 2 and PATR.match(token):
            return f"{w[i-2]} {w[i-1]} {token}"
    return None


async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(r"""
            SELECT pm.id, regexp_replace(pm.full_name, '\s+', ' ', 'g') AS t
            FROM persons_military pm
            WHERE pm.office = 'ha' AND pm.exported_at IS NULL
              AND pm.birth_date IS NULL AND length(pm.full_name) > 60
              AND NOT EXISTS (SELECT 1 FROM military_relatives mr
                               WHERE mr.military_id = pm.id)
        """)

        st = Counter()
        parsed = []
        for r in rows:
            d, f = grab_date(r['t']), grab_fio(r['t'])
            if d and f:
                parsed.append((r['id'], f, d)); st['разобрано полностью'] += 1
            elif d:
                st['есть дата, нет ФИО'] += 1
            elif f:
                st['есть ФИО, нет даты'] += 1
            else:
                st['ничего не вытащить'] += 1

        print(f'текстов: {len(rows)}')
        for k, v in st.most_common():
            print(f'{v:6d}  {k}')

        dup = Counter(); examples = []
        for lid, fio, d in parsed:
            hit = await conn.fetch("""
                SELECT pm.id, pm.office, pm.exported_at IS NOT NULL AS выгружен,
                       m.name AS менеджер
                FROM persons_military pm LEFT JOIN managers m ON m.id = pm.added_by
                WHERE pm.birth_date = $2 AND pm.id <> $3
                  AND regexp_replace(lower(translate(pm.full_name,'Ёё','Ее')),
                                     '[^а-я ]', ' ', 'g') ~ $1
            """, r'\m' + norm(fio).split()[0] + r'\M', d, lid)
            if hit:
                dup['дубликат'] += 1
                for h in hit:
                    dup[f"  у {h['менеджер']} / {h['office']}"
                        + (' (выгружен)' if h['выгружен'] else '')] += 1
                if len(examples) < 5:
                    examples.append(f"{fio} {d} → лид {hit[0]['id']} "
                                    f"у {hit[0]['менеджер']}")
            else:
                dup['новый'] += 1

        print(f'\nиз {len(parsed)} разобранных:')
        for k, v in dup.most_common(15):
            print(f'{v:6d}  {k}')
        print()
        for e in examples:
            print('  ', e)


if __name__ == '__main__':
    asyncio.run(main())
