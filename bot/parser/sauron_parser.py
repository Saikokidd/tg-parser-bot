"""
Парсер ответа Sauron API.

Два режима использования:

1. extract_address_relations(result)
   Извлекает раздел "Возможные связи по адресу" — список людей которые
   потенциально связаны с пробиваемым (живут или жили по тому же адресу).
   Возвращает список с указанием года источника.

2. build_relative_template(result)
   Собирает заполненный шаблон родственника из всего ответа API:
   ФИО, ДР, Адрес, Телефон, СНИЛС, ИНН, Паспорт, Почта.
   Стратегия: самое часто встречающееся значение по каждому полю.
"""
import re
from collections import Counter
from datetime import date
from typing import Optional


# ════════════════════════════════════════════════════════════
#       1. ИЗВЛЕЧЕНИЕ "ВОЗМОЖНЫЕ СВЯЗИ ПО АДРЕСУ"
# ════════════════════════════════════════════════════════════

# Имя ключа источника
ADDRESS_RELATION_PREFIX = "Возможные связи по адресу"


def _format_api_date(api_date: str) -> str:
    """ '1961-07-21' → '21.07.1961'. Если формат не тот — вернёт как есть."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', api_date.strip())
    if not m:
        return api_date.strip()
    y, mo, d = m.groups()
    return f"{d}.{mo}.{y}"


def _parse_relation_string(relation_str: str) -> list[dict]:
    """
    Распарсить строку "Связь с лицом". Разделители: ';' или ','.
    Форматы дат: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.

    Примеры строк:
        'Тупикин Иван 1942-08-26; Ковалёва Ольга 1961-07-21'
        'САФОНОВ АНТОН 18.01.1989, ЕЛИШЕВ АЛЕКСЕЙ 28.08.1972'
    """
    # Разбиваем по ';' ИЛИ ',' (берём оба разделителя)
    chunks = re.split(r'\s*[;,]\s*', relation_str)

    persons = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        # Пытаемся найти дату в конце строки в любом из форматов
        birth_str = ""
        full_name = chunk

        # Формат ГГГГ-ММ-ДД
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s*$', chunk)
        if m:
            birth_str = _format_api_date(m.group(1))
            full_name = chunk[:m.start()].strip()
        else:
            # Формат ДД.ММ.ГГГГ
            m = re.search(r'(\d{2}\.\d{2}\.\d{4})\s*$', chunk)
            if m:
                birth_str = m.group(1)  # уже в нужном формате
                full_name = chunk[:m.start()].strip()

        if full_name:
            persons.append({
                "full_name": full_name,
                "birth_date_str": birth_str
            })
    return persons


def _extract_year_from_source(source: str) -> Optional[int]:
    """ 'Возможные связи по адресу 2024' → 2024 """
    m = re.search(r'(\d{4})\s*$', source)
    return int(m.group(1)) if m else None


def extract_address_relations(api_result: dict) -> list[dict]:
    """
    Извлечь все упоминания "Возможные связи по адресу" из ответа API.
    Возвращает список:
    [
        {
            'year': 2024,
            'source': 'Возможные связи по адресу 2024',
            'address': '...',
            'persons': [{'full_name': '...', 'birth_date_str': '...'}, ...]
        },
        ...
    ]
    Сортировка по убыванию года (сначала свежее).
    """
    records = api_result.get("response", {}).get("result", [])
    blocks = []

    for r in records:
        source = r.get("Источник", "")
        if not source.startswith(ADDRESS_RELATION_PREFIX):
            continue

        relation_str = r.get("Связь с лицом", "")
        if not relation_str:
            continue

        persons = _parse_relation_string(relation_str)
        if not persons:
            continue

        blocks.append({
            "year": _extract_year_from_source(source),
            "source": source,
            "address": r.get("Адрес", ""),
            "persons": persons,
        })

    # Сначала свежие
    blocks.sort(key=lambda b: b["year"] or 0, reverse=True)
    return blocks


def format_address_relations(blocks: list[dict]) -> str:
    """
    Компактный вывод "возможных связей".
    Берёт всех людей со всех блоков, дедуплицирует по (ФИО+ДР).
    Простой нумерованный список без адресов и источников.
    ФИО экранируем — иначе Markdown упадёт на спецсимволах.
    """
    if not blocks:
        return "По этому человеку возможных связей по адресу в ответе API нет."

    # Собираем всех уникальных людей
    seen = set()
    persons = []
    for block in blocks:
        for p in block["persons"]:
            key = f"{p['full_name']}|{p['birth_date_str']}"
            if key in seen:
                continue
            seen.add(key)
            persons.append(p)

    if not persons:
        return "По этому человеку возможных связей по адресу в ответе API нет."

    lines = ["*Возможные связи:*\n"]
    for i, p in enumerate(persons, 1):
        name = _md_escape(p['full_name'])
        birth = f" • {p['birth_date_str']}" if p['birth_date_str'] else ""
        lines.append(f"{i}. {name}{birth}")

    return "\n".join(lines)


def _md_escape(text) -> str:
    """
    Экранировать спецсимволы Markdown (legacy mode aiogram).
    Telegram Markdown понимает: _ * ` [
    """
    if text is None:
        return "—"
    s = str(text)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


# ════════════════════════════════════════════════════════════
#       2. СБОРКА ШАБЛОНА РОДСТВЕННИКА (САМОЕ ЧАСТОЕ)
# ════════════════════════════════════════════════════════════

# Какие ключи API использовать для каждого поля шаблона
FIELD_API_KEYS = {
    "full_name":  ["ФИО"],
    "birth_date": ["День рождения"],
    "address":    ["Адрес", "Адрес регистрации", "Фактический адрес", "Контактный адрес"],
    "phone":      ["Телефон"],
    "snils":      ["СНИЛС"],
    "inn":        ["ИНН"],
    "passport":   ["Паспорт"],
    "email":      ["Почта", "Email", "E-mail"],
}


def _normalize_for_count(field: str, value: str) -> str:
    """Нормализация перед подсчётом частоты — чтобы одинаковое не считалось как разное"""
    v = value.strip()
    if not v:
        return ""

    if field == "full_name":
        # Регистр: 'КОВАЛЕВ ИВАН' и 'Ковалев Иван' → одно
        return " ".join(v.split()).lower()

    if field == "address":
        # Убираем индекс, "Россия", повторные пробелы, пунктуацию-разделители
        s = v.lower()
        s = re.sub(r'\b\d{6}\b', '', s)         # индекс
        s = re.sub(r'\bроссия\b,?', '', s)      # упоминание страны
        s = re.sub(r'\b643\b,?', '', s)         # код страны 643
        s = re.sub(r'\b36\b,?', '', s)          # код региона
        s = re.sub(r'[,.\-]', ' ', s)            # пунктуацию в пробел
        s = re.sub(r'\s+', ' ', s)               # сжимаем пробелы
        return s.strip()

    if field in ("phone", "snils", "inn", "passport"):
        # Только цифры
        return re.sub(r'\D', '', v)

    if field == "email":
        return v.lower()

    if field == "birth_date":
        return v.strip()

    return v


def _pick_most_common(records: list[dict], field: str) -> Optional[str]:
    """
    По полю собрать все значения из записей API,
    нормализовать → посчитать частоты → вернуть оригинал самого частого.
    """
    api_keys = FIELD_API_KEYS[field]

    # Собираем (нормализованное значение, оригинал)
    pairs = []
    for r in records:
        for key in api_keys:
            if key in r and r[key]:
                original = str(r[key]).strip()
                norm = _normalize_for_count(field, original)
                if norm:
                    pairs.append((norm, original))

    if not pairs:
        return None

    # Самый частый нормализованный
    norm_counter = Counter(p[0] for p in pairs)
    best_norm, _ = norm_counter.most_common(1)[0]

    # Среди оригиналов с этим нормализованным — самый частый оригинал
    originals = [p[1] for p in pairs if p[0] == best_norm]
    best_orig, _ = Counter(originals).most_common(1)[0]

    return best_orig


def build_relative_template(api_result: dict) -> dict:
    """
    Собрать данные для шаблона родственника на основе всех записей API.
    Возвращает dict с полями (некоторые могут быть None):
        full_name, birth_date_str, address, phone, snils, inn, passport, email
        emails_top — список до 3 уникальных почт (по убыванию частоты).
    """
    records = api_result.get("response", {}).get("result", [])
    if not records:
        return {}

    template = {}
    for field in FIELD_API_KEYS:
        value = _pick_most_common(records, field)
        if not value:
            continue

        if field == "birth_date":
            template["birth_date_str"] = _format_api_date(value)
        else:
            template[field] = value

    # Дополнительно — собираем топ-3 уникальных email для последующей валидации
    template["emails_top"] = _pick_top_n_unique(records, "email", n=3)

    return template


def _pick_top_n_unique(records: list[dict], field: str, n: int = 3) -> list[str]:
    """
    Вернуть до N самых частых уникальных значений поля.
    """
    api_keys = FIELD_API_KEYS[field]

    pairs = []
    for r in records:
        for key in api_keys:
            if key in r and r[key]:
                original = str(r[key]).strip()
                norm = _normalize_for_count(field, original)
                if norm:
                    pairs.append((norm, original))

    if not pairs:
        return []

    norm_counter = Counter(p[0] for p in pairs)
    top_norms = [item[0] for item in norm_counter.most_common(n)]

    # Возвращаем оригиналы (берём самый частый оригинал на каждую норму)
    result = []
    for norm in top_norms:
        originals = [p[1] for p in pairs if p[0] == norm]
        best_orig, _ = Counter(originals).most_common(1)[0]
        result.append(best_orig)
    return result


def format_relative_template(template: dict) -> str:
    """
    Отформатировать собранный шаблон родственника так,
    чтобы менеджер мог его скопировать и через "Заполнить" внести в БД.

    Поля phone_info и valid_emails — результаты обогащения через внешние API:
    - phone_info: {operator, region, old_operator}
    - valid_emails: список рабочих почт
    """
    if not template:
        return "❌ Не удалось собрать данные родственника из ответа API."

    phone = template.get('phone', '') or ''
    phone_info = template.get('phone_info') or {}
    operator = phone_info.get('operator') or ''
    region = phone_info.get('region') or ''

    valid_emails = template.get('valid_emails') or []
    email_str = ", ".join(valid_emails) if valid_emails else '—'

    lines = [
        "Шаблон (не всё обязательно):",
        "",
        "```",
        f"ФИО: {template.get('full_name', '')}",
        f"ДР: {template.get('birth_date_str', '')}",
        f"Адрес: {template.get('address', '')}",
        f"Телефон: {phone}",
        f"Оператор: {operator}",
        f"Регион: {region}",
        f"СНИЛС: {template.get('snils', '')}",
        f"ИНН: {template.get('inn', '')}",
        f"Паспорт: {template.get('passport', '')}",
        f"Почта: {email_str}",
        "```",
        "",
        "_Внесите через кнопку «Заполнить»._"
    ]
    return "\n".join(lines)
