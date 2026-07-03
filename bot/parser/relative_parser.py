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


# ══════════════════════════════════════════════════════════════════
# Формат "сводка Саурона" — тестовая фича (просили dp).
# Триггер: слово "Личности". Всё до него отбрасывается.
# Откат: env-флаг SAURON_SUMMARY_INPUT_ENABLED (роутинг в хендлере).
# ══════════════════════════════════════════════════════════════════

SAURON_KEY = "личности"
SAURON_MAX_VALUES = 3

# нормализованный ярлык -> поле
SAURON_WHITELIST = {
    'личности': 'full_name', 'фио': 'full_name',
    'др': 'birth_date', 'дата рождения': 'birth_date', 'др:': 'birth_date',
    'адрес': 'address',
    'телефон': 'phone', 'тел': 'phone', 'тел.': 'phone', 'номер': 'phone', 'телефоны': 'phone',
    'снилс': 'snils',
    'инн': 'inn',
    'паспорт': 'passport',
    'email': 'email', 'e-mail': 'email', 'почта': 'email', 'мейл': 'email', 'эл.почта': 'email',
}
# ярлыки, которые надо игнорировать (но распознавать как границу поля)
SAURON_IGNORE_STARTS = ('автомобили', 'водительское удостоверение', 'ву')
# заглушки пустого значения
SAURON_EMPTY_TOKENS = {'', '—', '-', '–', 'нет', 'не найдено', 'не указано'}


def has_sauron_key(text: str) -> bool:
    return SAURON_KEY in (text or "").lower()


def _snorm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').strip().lower())


def _split_name_date(line: str):
    line = line.strip()
    m = re.search(r'(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\s*$', line)
    if m:
        d = parse_date(m.group(1))
        return d, (line[:m.start()].strip() or None)
    return None, (line or None)


def _sauron_match_header(line: str):
    """
    Определяет, является ли строка заголовком секции.
    Возвращает (field | 'IGNORE' | None, inline_value).
    """
    if ':' in line:
        key_part, inline = line.split(':', 1)
        if len(key_part) <= 40 and not re.search(r'\d', key_part):
            field = SAURON_WHITELIST.get(_snorm(key_part))
            if field:
                return field, inline.strip()
            return 'IGNORE', inline.strip()   # любая другая "Метка:" — граница, значение отбрасываем
        return None, None                     # двоеточие есть, но это значение
    norm = _snorm(line)
    if norm in SAURON_WHITELIST:              # голый ярлык без двоеточия (вариант 1)
        return SAURON_WHITELIST[norm], ''
    for lbl in SAURON_IGNORE_STARTS:
        if norm.startswith(lbl):
            return 'IGNORE', ''
    return None, None


def _sauron_values(raw_list):
    """Плоский список значений: режем по запятым/точкам-с-запятой/переносам, чистим заглушки."""
    out = []
    for rv in raw_list:
        for part in re.split(r'[;,\n]', rv):
            part = part.strip()
            if part and _snorm(part) not in SAURON_EMPTY_TOKENS:
                out.append(part)
    return out


def parse_sauron_summary(text: str) -> dict:
    """
    Разбирает сводку Саурона в один dict родственника (всегда 1 человек).
    Белый список полей; всё вне списка (Автомобили, ВУ, соцсети...) игнорируется.
    По каждому многозначному полю — первые SAURON_MAX_VALUES значений.
    """
    result = {'full_name': None, 'birth_date': None, 'phone': None,
              'address': None, 'extra': {}}
    if not text:
        return result
    idx = text.lower().find(SAURON_KEY)
    if idx == -1:
        return result
    region = text[idx:]

    fields = {}          # field -> list[str] сырых значений
    cur_field = None
    cur_values = []

    def _flush():
        if cur_field and cur_field != 'IGNORE':
            fields.setdefault(cur_field, []).extend(cur_values)

    for raw_line in region.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        field, inline = _sauron_match_header(line)
        if field is not None:
            _flush()
            cur_field = field
            cur_values = [inline] if inline else []
        elif cur_field is not None:
            cur_values.append(line)
    _flush()

    # ── ФИО (+ дата в хвосте) ──
    if fields.get('full_name'):
        d, name = _split_name_date(fields['full_name'][0])
        result['full_name'] = name
        if d:
            result['birth_date'] = d
    # ── отдельная ДР, если ФИО без даты ──
    if not result['birth_date'] and fields.get('birth_date'):
        for v in _sauron_values(fields['birth_date']):
            d = parse_date(v)
            if d:
                result['birth_date'] = d
                break
    # ── адрес ──
    if fields.get('address'):
        addr = _sauron_values(fields['address'])
        if addr:
            result['address'] = ", ".join(addr)

    # ── телефоны: первые 3, основной в phone, остальные в phones_other ──
    phones = []
    for v in _sauron_values(fields.get('phone', [])):
        p = normalize_phone(v)
        if p and p not in phones:
            phones.append(p)
    phones = phones[:SAURON_MAX_VALUES]
    if phones:
        result['phone'] = phones[0]
        if len(phones) > 1:
            result['extra']['phones_other'] = ", ".join(phones[1:])

    # ── email: списком ──
    emails = [v.lower() for v in _sauron_values(fields.get('email', []))][:SAURON_MAX_VALUES]
    if emails:
        result['extra']['emails'] = emails

    # ── СНИЛС / ИНН: только цифры ──
    for key in ('snils', 'inn'):
        digits = [re.sub(r'\D', '', v) for v in _sauron_values(fields.get(key, []))]
        digits = [d for d in digits if d][:SAURON_MAX_VALUES]
        if digits:
            result['extra'][key] = ", ".join(digits)

    # ── Паспорт: как есть (могут быть серии типа XII-БА...), не режем цифры ──
    passports = [re.sub(r'\s+', ' ', v) for v in _sauron_values(fields.get('passport', []))][:SAURON_MAX_VALUES]
    if passports:
        result['extra']['passport'] = ", ".join(passports)

    return result