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
    # Поддерживаем разделители: . / - и пробел
    # 15.03.1985, 15/03/1985, 15-03-1985, 15 03 1985
    re.compile(r'^(\d{1,2})[./\-\s](\d{1,2})[./\-\s](\d{4})$'),
    re.compile(r'^(\d{1,2})[./\-\s](\d{1,2})[./\-\s](\d{2})$'),
    re.compile(r'^(\d{4})[./\-\s](\d{1,2})[./\-\s](\d{1,2})$'),
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

def _normalize_single_phone(digits: str) -> str:
    """
    Привести один номер (только цифры) к виду '+...'.
    Дефолт — российский (+7), украинские номера в БД не используются.
    """
    if not digits:
        return ""
    if digits.startswith('8') and len(digits) == 11:
        digits = '7' + digits[1:]
    if digits.startswith('380') and len(digits) == 12:
        return '+' + digits
    if digits.startswith('7') and len(digits) == 11:
        return '+' + digits
    if len(digits) == 10:
        # 10 цифр без кода страны — считаем российским мобильным
        return '+7' + digits
    return '+' + digits


def normalize_phone(phone) -> str | None:
    """
    Нормализовать строку телефона до '+...'.

    Поддерживает любые форматы: с пробелами, тире, скобками, без них.
    Если в строке несколько номеров — возвращает первый.
    Для извлечения всех номеров используй extract_all_phones().

    Возвращает None если на вход пришло None, пустая строка или строка
    из которой не удалось извлечь ни одного валидного номера.

    Логика:
    1. Если в строке после удаления нецифровых символов получается ровно один
       номер по длине (10/11/12 цифр) — это один телефон, нормализуем сразу.
       Покрывает '+7 912 345-67-89', '+7(912)345-67-89', '89123456789' и т.п.
    2. Иначе вызываем extract_all_phones — он разделит несколько номеров
       по разделителям и слипшиеся.
    """
    if not phone:
        return None
    s = str(phone).strip()
    if not s:
        return None
    # Если вся строка — один телефон по количеству цифр, нормализуем сразу.
    # Это покрывает форматы с пробелами/тире/скобками внутри одного номера.
    digits = re.sub(r'\D', '', s)
    if len(digits) in (10, 11, 12):
        return _normalize_single_phone(digits)
    # Иначе пробуем извлечь несколько номеров через разделители.
    phones = extract_all_phones(s)
    return phones[0] if phones else None


def extract_all_phones(phone_str: str) -> list[str]:
    """
    Извлечь все номера телефонов из строки.

    Поддерживает любые разделители (пробелы, запятые, слэши, переносы)
    + случай когда несколько номеров слиплись без разделителей.

    Возвращает список нормализованных номеров (с '+').
    """
    if not phone_str:
        return []

    # Разбиваем по любым разделителям (пробел, запятая, точка с запятой, слэш, перенос)
    chunks = re.split(r'[\s,;/\n]+', phone_str.strip())

    phones = []
    for chunk in chunks:
        digits = re.sub(r'\D', '', chunk)
        if not digits:
            continue

        # Если в одном куске сразу несколько слипшихся номеров — режем
        # Российский с кодом: 11 цифр (начинается с 7 или 8)
        # Украинский с кодом: 12 цифр (начинается с 380)
        # Без кода: 10 цифр
        while digits:
            if len(digits) <= 12 and 10 <= len(digits) <= 12:
                # Один номер — добавляем и выходим
                phones.append(_normalize_single_phone(digits))
                break
            elif digits.startswith('380') and len(digits) >= 12:
                phones.append(_normalize_single_phone(digits[:12]))
                digits = digits[12:]
            elif (digits.startswith('7') or digits.startswith('8')) and len(digits) >= 11:
                phones.append(_normalize_single_phone(digits[:11]))
                digits = digits[11:]
            elif len(digits) >= 10:
                # Без кода — берём первые 10
                phones.append(_normalize_single_phone(digits[:10]))
                digits = digits[10:]
            else:
                # Огрызок меньше 10 цифр — игнорируем
                break

    return phones


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
            phones = extract_all_phones(value)
            if phones:
                result['phone'] = phones[0]
                if len(phones) > 1:
                    # Дополнительные номера — в extra.phones_other через запятую
                    result['extra']['phones_other'] = ", ".join(phones[1:])
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


def parse_relatives_batch(text: str) -> list[dict]:
    """
    Распарсить текст из нескольких блоков родственников.
    Блоки разделяются одной или несколькими пустыми строками.

    Возвращает список dict (даже если в тексте один блок).
    Каждый блок проходит через parse_relative — невалидные тоже попадают
    в результат, фильтрация в хендлере по validate_relative.
    """
    # Разбиваем по двойному (или более) переносу строки — это надёжнее одного.
    # Но менеджер может разделять одной пустой строкой, поэтому делаем регулярку
    # которая ловит "\n\n+" (одна и более пустых строк подряд)
    import re as _re
    blocks = _re.split(r"\n\s*\n+", text.strip())

    results = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        results.append(parse_relative(block))
    return results


def validate_relative(data: dict) -> Optional[str]:
    """Возвращает строку с ошибкой или None если ok"""
    if not data.get('full_name'):
        return "Не указано ФИО (формат: 'ФИО: Иванова Мария Петровна')"
    return None


# ────────── Форматирование ──────────

def format_relative(data: dict) -> str:
    lines = ["Распознано:\n"]
    lines.append(f"ФИО: {data.get('full_name', '—')}")
    lines.append(f"ДР: {data['birth_date'].strftime('%d.%m.%Y') if data.get('birth_date') else '—'}")
    lines.append(f"Адрес: {data.get('address', '—')}")
    lines.append(f"Телефон: {data.get('phone', '—')}")

    extra = data.get('extra', {})
    if extra.get('snils'):
        lines.append(f"СНИЛС: {extra['snils']}")
    if extra.get('inn'):
        lines.append(f"ИНН: {extra['inn']}")
    if extra.get('passport'):
        lines.append(f"Паспорт: {extra['passport']}")
    if extra.get('email'):
        lines.append(f"Почта: {extra['email']}")

    return "\n".join(lines)


def format_relative_record(record: dict) -> str:
    """Краткая карточка родственника из БД"""
    birth = record.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    return (
        f"{record.get('full_name', '—')}\n"
        f"{birth_str} | 📞 {record.get('phone', '—')}\n"
        f"{record.get('address', '—')}"
    )


def parse_relatives_batch(text: str) -> list[dict]:
    """
    Распарсить текст из одного или нескольких блоков родственников.
    Блоки разделяются одной или несколькими пустыми строками.
    
    Возвращает список dict — даже если в тексте один блок.
    Каждый блок проходит через parse_relative; невалидные тоже попадают
    в результат, фильтрация по validate_relative делается в хендлере.
    """
    blocks = re.split(r"\n\s*\n+", text.strip())
    results = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        results.append(parse_relative(block))
    return results