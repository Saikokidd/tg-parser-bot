"""
Парсер шаблона родственника.

Менеджер пишет так:
    ФИО: Ковалёва Ольга Алексеевна
    ДР: 21.07.1961
    Адрес: Воронежская обл., Таловский р-н, п.Веревкин 2й, ул.Луговая, дом 23
    Телефон: +79204664864
    СНИЛС: 04532173530
    ИНН: 362900736403
    Паспорт: 2006749266
    Почта: example@mail.ru

Структурные поля → колонки в БД (full_name, birth_date, phone, address)
Гибкие поля → JSONB extra (СНИЛС, ИНН, паспорт, почта, любые новые)
"""
import re
from datetime import date
from typing import Optional


# ────────── Алиасы ключей ──────────
FIELD_ALIASES = {
    'full_name':  ['фио', 'ф.и.о', 'ф.и.о.'],
    'birth_date': ['др', 'дата рождения', 'д.р.', 'д.р'],
    'address':    ['адрес', 'адресс', 'место жительства'],
    'phone':      ['телефон', 'тел', 'тел.', 'номер'],
    'snils':      ['снилс'],
    'inn':        ['инн'],
    'passport':   ['паспорт'],
    'email':      ['почта', 'email', 'e-mail', 'эл.почта', 'мейл'],
}


# ────────── Парсинг даты (тот же набор что у военных) ──────────
DATE_PATTERNS = [
    re.compile(r'^(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})$'),
    re.compile(r'^(\d{1,2})[./\-](\d{1,2})[./\-](\d{2})$'),
    re.compile(r'^(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})$'),
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


# ────────── Нормализация телефона ──────────

def normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return phone.strip()
    if digits.startswith('8') and len(digits) == 11:
        digits = '7' + digits[1:]
    if digits.startswith('380') and len(digits) == 12:
        return '+' + digits
    if digits.startswith('7') and len(digits) == 11:
        return '+' + digits
    if len(digits) == 10:
        return '+38' + digits
    return '+' + digits


# ────────── Главный парсер ──────────

def _normalize_key(key: str) -> str:
    return key.strip().lower().rstrip(':').strip()


def _find_field(key_normalized: str) -> Optional[str]:
    for field, aliases in FIELD_ALIASES.items():
        if key_normalized in aliases:
            return field
    return None


def parse_relative(text: str) -> dict:
    result = {
        'full_name': None,
        'birth_date': None,
        'phone': None,
        'address': None,
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
        elif field == 'phone':
            result['phone'] = normalize_phone(value)
        elif field == 'address':
            result['address'] = value
        elif field == 'snils':
            result['extra']['snils'] = re.sub(r'\D', '', value)
        elif field == 'inn':
            result['extra']['inn'] = re.sub(r'\D', '', value)
        elif field == 'passport':
            result['extra']['passport'] = re.sub(r'\D', '', value)
        elif field == 'email':
            result['extra']['email'] = value.lower()

    return result


def validate_relative(data: dict) -> Optional[str]:
    """Возвращает строку с ошибкой или None если ok"""
    if not data.get('full_name'):
        return "Не указано ФИО (формат: 'ФИО: Иванова Мария Петровна')"
    return None


# ────────── Форматирование ──────────

def format_relative(data: dict) -> str:
    lines = ["📋 *Распознано (родственник):*\n"]
    lines.append(f"👤 ФИО: {data.get('full_name', '—')}")
    lines.append(f"🎂 ДР: {data['birth_date'].strftime('%d.%m.%Y') if data.get('birth_date') else '—'}")
    lines.append(f"🏠 Адрес: {data.get('address', '—')}")
    lines.append(f"📞 Телефон: {data.get('phone', '—')}")

    extra = data.get('extra', {})
    if extra.get('snils'):
        lines.append(f"🆔 СНИЛС: {extra['snils']}")
    if extra.get('inn'):
        lines.append(f"🆔 ИНН: {extra['inn']}")
    if extra.get('passport'):
        lines.append(f"🆔 Паспорт: {extra['passport']}")
    if extra.get('email'):
        lines.append(f"📧 Почта: {extra['email']}")

    return "\n".join(lines)


def format_relative_record(record: dict) -> str:
    """Краткая карточка родственника из БД"""
    birth = record.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    return (
        f"👤 {record.get('full_name', '—')}\n"
        f"🎂 {birth_str} | 📞 {record.get('phone', '—')}\n"
        f"🏠 {record.get('address', '—')}"
    )
