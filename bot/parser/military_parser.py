"""
Парсер шаблона военного.

Менеджер пишет так:
    ФИО: Иванов Иван Иванович
    ДР: 15.03.1985
    Б/Ч: 1234
    Позывной: Север
    Статус: погиб
    Доп инфа: подробности

Структурные поля → отдельные колонки в БД (full_name, birth_date, status)
Гибкие поля → JSONB extra (Б/Ч, позывной, доп.инфа)
"""
import re
from datetime import date
from typing import Optional


# ────────── Алиасы ключей шаблона ──────────
# В нижнем регистре, без двоеточия. Парсер нормализует ввод и ищет совпадения.
FIELD_ALIASES = {
    'full_name':  ['фио', 'ф.и.о', 'ф.и.о.'],
    'birth_date': ['др', 'дата рождения', 'д.р.', 'д.р'],
    'status':     ['статус'],
    'unit':       ['б/ч', 'бч', 'боевая часть', 'в/ч'],
    'callsign':   ['позывной', 'позывн'],
    'note':       ['доп инфа', 'доп.инфа', 'доп инфо', 'дополнительно', 'примечание', 'комментарий'],
}


# ────────── Распознавание статуса ──────────
STATUS_KILLED = {'погиб', 'погибший', 'погибшего', 'killed', '200', 'груз 200'}
STATUS_MISSING = {'пропал', 'пропавший', 'без вести', 'безвести пропавший', 'бп', 'missing'}


def parse_status(text: str) -> Optional[str]:
    t = text.lower().strip()
    if any(k in t for k in STATUS_KILLED):
        return 'killed'
    if any(k in t for k in STATUS_MISSING):
        return 'missing'
    return None


# ────────── Парсинг даты ──────────
DATE_PATTERNS = [
    re.compile(r'^(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})$'),  # 15.03.1985
    re.compile(r'^(\d{1,2})[./\-](\d{1,2})[./\-](\d{2})$'),   # 15.03.85
    re.compile(r'^(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})$'),   # 1985-03-15
]


def parse_date(text: str) -> Optional[date]:
    text = text.strip()
    for pattern in DATE_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g[2]) == 4:
                if int(g[0]) > 31:  # YYYY-MM-DD
                    return date(int(g[0]), int(g[1]), int(g[2]))
                return date(int(g[2]), int(g[1]), int(g[0]))
            year = int(g[2])
            year = 2000 + year if year < 30 else 1900 + year
            return date(year, int(g[1]), int(g[0]))
        except ValueError:
            continue
    return None


# ────────── Главный парсер ──────────

def _normalize_key(key: str) -> str:
    return key.strip().lower().rstrip(':').strip()


def _find_field(key_normalized: str) -> Optional[str]:
    """По нормализованному ключу определить какому полю он соответствует"""
    for field, aliases in FIELD_ALIASES.items():
        if key_normalized in aliases:
            return field
    return None


def parse_military(text: str) -> dict:
    """
    Парсит шаблон военного построчно.
    Возвращает dict со структурными полями + extra (JSONB).
    """
    result = {
        'full_name': None,
        'birth_date': None,
        'status': None,
        'extra': {}
    }

    for line in text.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue

        key, value = line.split(':', 1)
        value = value.strip()
        if not value:
            continue

        field = _find_field(_normalize_key(key))
        if not field:
            continue

        if field == 'full_name':
            result['full_name'] = value
        elif field == 'birth_date':
            d = parse_date(value)
            if d:
                result['birth_date'] = d
        elif field == 'status':
            s = parse_status(value)
            if s:
                result['status'] = s
        elif field == 'unit':
            result['extra']['unit'] = value
        elif field == 'callsign':
            result['extra']['callsign'] = value
        elif field == 'note':
            result['extra']['note'] = value

    return result


def validate_military(data: dict) -> Optional[str]:
    """
    Проверяет обязательные поля. Возвращает строку с ошибкой или None если ok.
    """
    if not data.get('full_name'):
        return "Не указано ФИО (формат: 'ФИО: Иванов Иван Иванович')"
    if not data.get('status'):
        return "Не указан или не распознан статус (нужно: 'погиб' или 'пропал')"
    return None


# ────────── Форматирование для показа ──────────

STATUS_LABELS = {
    'killed':  '☠️ Погиб',
    'missing': '❓ Пропал без вести',
}


def format_military(data: dict) -> str:
    """Отформатировать распарсенного военного для показа менеджеру"""
    lines = ["📋 *Распознано (военный):*\n"]
    lines.append(f"👤 ФИО: {data.get('full_name', '—')}")
    lines.append(f"🎂 ДР: {data['birth_date'].strftime('%d.%m.%Y') if data.get('birth_date') else '—'}")

    status = data.get('status')
    lines.append(f"⚐ Статус: {STATUS_LABELS.get(status, '—')}")

    extra = data.get('extra', {})
    lines.append(f"🏠 Б/Ч: {extra.get('unit', '—')}")
    lines.append(f"🎖 Позывной: {extra.get('callsign', '—')}")
    if extra.get('note'):
        lines.append(f"📝 Доп.инфа: {extra['note']}")

    return "\n".join(lines)


def format_military_record(record: dict) -> str:
    """Краткая карточка военного из БД"""
    birth = record.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    extra = record.get('extra') or {}
    status_label = STATUS_LABELS.get(record.get('status'), '—')

    parts = [
        f"👤 {record.get('full_name', '—')}",
        f"🎂 {birth_str} | {status_label}",
    ]
    if extra.get('unit') or extra.get('callsign'):
        parts.append(f"🏠 Б/Ч: {extra.get('unit', '—')} | 🎖 {extra.get('callsign', '—')}")
    return "\n".join(parts)
