import re
from datetime import date
from typing import Optional


# ФИО: 2-3 слова с заглавной буквы, поддержка дефисов
FIO_PATTERN = re.compile(
    r'\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?:\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)?)\b'
)

# Дата: разные форматы
DATE_PATTERNS = [
    re.compile(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})\b'),  # 15.03.1985
    re.compile(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2})\b'),   # 15.03.85
    re.compile(r'\b(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})\b'),   # 1985-03-15
]

# Телефон: украинские/российские форматы
PHONE_PATTERN = re.compile(
    r'(?:тел[.:])?\s*(\+?[78]?\s*[\-\(]?\d{3}[\-\)]?\s*\d{3}[\-\s]?\d{2}[\-\s]?\d{2})'
)

# Позывной
CALLSIGN_PATTERN = re.compile(
    r'(?:позывной|позывн\.?|call\s*sign)[:\s]+([А-ЯЁа-яёA-Za-z0-9\-]+)',
    re.IGNORECASE
)

# Боевая часть
MILITARY_UNIT_PATTERN = re.compile(
    r'(?:б[./]?ч\.?|боевая\s+часть|в/ч)[:\s]+([А-ЯЁа-яёA-Za-z0-9\s\-]+?)(?:\n|,|$)',
    re.IGNORECASE
)

# Боевое задание
COMBAT_MISSION_PATTERN = re.compile(
    r'(?:б[./]?з\.?|боевое\s+задание)[:\s]+(.+?)(?:\n|,|бп|позывной|б[./]?ч|$)',
    re.IGNORECASE
)

# Безвести пропавший
MISSING_PATTERN = re.compile(
    r'\b(бп|безвести\s+пропавший|пропавший\s+без\s+вести)\b',
    re.IGNORECASE
)


def parse_date(text: str) -> Optional[date]:
    """Распарсить дату из текста, поддержка нескольких форматов"""
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            groups = m.groups()
            try:
                if len(groups[2]) == 4:
                    if int(groups[0]) > 31:  # YYYY-MM-DD
                        return date(int(groups[0]), int(groups[1]), int(groups[2]))
                    else:  # DD.MM.YYYY
                        return date(int(groups[2]), int(groups[1]), int(groups[0]))
                else:  # DD.MM.YY
                    year = int(groups[2])
                    year = 2000 + year if year < 30 else 1900 + year
                    return date(year, int(groups[1]), int(groups[0]))
            except ValueError:
                continue
    return None


def normalize_phone(phone: str) -> str:
    """Привести телефон к формату +380XXXXXXXXX"""
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('8') and len(digits) == 11:
        digits = '7' + digits[1:]
    if digits.startswith('380') and len(digits) == 12:
        return '+' + digits
    if digits.startswith('7') and len(digits) == 11:
        return '+' + digits
    if len(digits) == 10:
        return '+38' + digits
    return '+' + digits


def parse_text(text: str) -> dict:
    """Распарсить свободный текст и вернуть структурированные данные"""
    result = {}

    fio_match = FIO_PATTERN.search(text)
    if fio_match:
        result['full_name'] = fio_match.group(1).strip()

    parsed_date = parse_date(text)
    if parsed_date:
        result['birth_date'] = parsed_date

    phone_match = PHONE_PATTERN.search(text)
    if phone_match:
        result['phone'] = normalize_phone(phone_match.group(1))

    callsign_match = CALLSIGN_PATTERN.search(text)
    if callsign_match:
        result['callsign'] = callsign_match.group(1).strip()

    unit_match = MILITARY_UNIT_PATTERN.search(text)
    if unit_match:
        result['military_unit'] = unit_match.group(1).strip()

    mission_match = COMBAT_MISSION_PATTERN.search(text)
    if mission_match:
        result['combat_mission'] = mission_match.group(1).strip()

    if MISSING_PATTERN.search(text):
        result['missing'] = True

    return result


def format_parsed(data: dict) -> str:
    """Отформатировать распознанные данные для показа менеджеру"""
    lines = ["📋 *Распознанные данные:*\n"]
    lines.append(f"👤 ФИО: {data.get('full_name', '—')}")
    lines.append(f"🎂 Дата рождения: {data['birth_date'].strftime('%d.%m.%Y') if data.get('birth_date') else '—'}")
    lines.append(f"📞 Телефон: {data.get('phone', '—')}")
    lines.append(f"🎖 Позывной: {data.get('callsign', '—')}")
    lines.append(f"🏠 Боевая часть: {data.get('military_unit', '—')}")
    lines.append(f"📌 Боевое задание: {data.get('combat_mission', '—')}")
    lines.append(f"❓ Безвести пропавший: {'Да' if data.get('missing') else 'Нет'}")
    return "\n".join(lines)


def format_person_record(record: dict) -> str:
    """Отформатировать запись из БД для показа"""
    birth = record.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    created = record.get('created_at')
    created_str = created.strftime('%d.%m.%Y %H:%M') if created else '—'

    return (
        f"👤 {record.get('full_name', '—')}\n"
        f"🎂 {birth_str} | 📞 {record.get('phone', '—')}\n"
        f"🎖 {record.get('callsign', '—')} | 🏠 {record.get('military_unit', '—')}\n"
        f"📅 Добавлено: {created_str}"
    )
