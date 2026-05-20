"""
Парсер блокнота Миши.

Формат:
    + ФИО Военного ДД.ММ.ГГГГ
    Без вести пропавший: ДАТА
    Дата смерти: ДАТА
    Позывной ...
    В/ч ...

    Данные родствинников:

    Жена: ФИО ДД.ММ.ГГГГ
    Телефон: ...
    Почта: ...
    Паспорт: ...
    Снилс: ...
    ИНН: ...
    АДРЕС (последняя строка блока)

Возвращает список военных, у каждого — список родственников.
"""
import re
from datetime import date
from typing import Optional


# Метки родственников (могут быть в любом регистре)
RELATIVE_LABELS = (
    "жена", "муж", "мать", "отец", "сын", "дочь",
    "сестра", "брат", "бабушка", "дедушка",
    "тётя", "тетя", "дядя", "племянник", "племянница",
    "отчим", "мачеха",
)

# Регекс на строку начала военного: "+ ФИО ДД.ММ.ГГГГ" или просто "ФИО ДД.ММ.ГГГГ"
# ФИО — 2-4 слова с заглавной буквы, ДР в формате ДД.ММ.ГГГГ
MILITARY_RE = re.compile(
    r"^[+]?\s*([А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z\-]+){1,3})[,\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE
)

# Регекс на строку начала родственника: "Жена: ФИО ДР"
RELATIVE_RE = re.compile(
    r"^\s*([А-ЯЁа-яёA-Za-z]+)\s*:\s*(.+)$",
    re.IGNORECASE
)

DATE_RE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})")


def _parse_date(s: str) -> Optional[date]:
    if not s or s.strip() in ("-", "—", "нет", "даты нет"):
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    d, mo, y = m.groups()
    y = int(y)
    if y < 100:
        y = 2000 + y if y < 30 else 1900 + y
    try:
        return date(y, int(mo), int(d))
    except ValueError:
        return None


def _clean_value(v: str) -> str:
    """Очистить значение: '-' → пусто, .strip()"""
    v = v.strip()
    if v in ("-", "—", "—", ""):
        return ""
    return v


def _normalize_phone(p: str) -> str:
    """Только цифры + код страны"""
    digits = re.sub(r"\D", "", p)
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def parse_file(filepath: str) -> list[dict]:
    """
    Распарсить файл. Возвращает список:
    [
        {
            'full_name': str,
            'birth_date': date|None,
            'status': str,           # свободный текст, типа 'погиб'
            'extra': {'unit': ..., 'callsign': ..., 'note': ...},
            'relatives': [
                {'full_name', 'birth_date', 'phone', 'address',
                 'extra': {'snils', 'inn', 'passport', 'email', 'phones_other', 'role'}}
            ],
        },
        ...
    ]
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Разбиваем на блоки военных по началу строки с "+ Фамилия ..." или начало файла
    # Сначала находим все стартовые позиции
    lines = content.splitlines()
    military_blocks = []
    current_block = []

    for line in lines:
        # Определяем — начало нового военного?
        stripped = line.strip()
        # Эвристика: строка начинается с "+ " ИЛИ выглядит как "ФИО ДД.ММ.ГГГГ"
        # без префиксов типа "Жена:", "Телефон:" и т.д.
        is_new_military = False
        if stripped.startswith("+"):
            is_new_military = True
        else:
            # Проверим есть ли это ФИО+ДР без меток
            if (MILITARY_RE.match(stripped) and
                    ":" not in stripped[:30] and
                    not stripped.lower().startswith(("телефон", "почта", "паспорт",
                                                     "снилс", "инн", "адрес",
                                                     "данные", "без вести", "дата"))):
                # И не похоже на строку родственника (нет метки "Жена:" и т.д.)
                first_word = stripped.split()[0].lower().rstrip(":")
                if first_word not in RELATIVE_LABELS:
                    is_new_military = True

        if is_new_military and current_block:
            military_blocks.append(current_block)
            current_block = [line]
        else:
            current_block.append(line)

    if current_block:
        military_blocks.append(current_block)

    # Парсим каждый блок
    result = []
    for block in military_blocks:
        parsed = _parse_military_block(block)
        if parsed:
            result.append(parsed)

    return result


def _parse_military_block(lines: list[str]) -> Optional[dict]:
    """Распарсить блок одного военного со всеми его родственниками"""
    if not lines:
        return None

    # Первая значимая строка — ФИО + ДР военного
    first = None
    start_idx = 0
    for i, line in enumerate(lines):
        s = line.strip().lstrip("+").strip()
        if s and MILITARY_RE.match(s):
            first = s
            start_idx = i
            break

    if not first:
        return None

    m = MILITARY_RE.match(first)
    full_name = m.group(1).strip()
    birth_date = _parse_date(m.group(2))

    military = {
        "full_name": full_name,
        "birth_date": birth_date,
        "status": "missing",   # по умолчанию для всех Миши — пропавшие/погибшие
        "extra": {},
        "relatives": [],
    }

    # Собираем поля до первого родственника
    notes_parts = []
    i = start_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        # Проверяем — началась ли секция родственников?
        first_word = stripped.split(":")[0].strip().lower() if ":" in stripped else ""
        if first_word in RELATIVE_LABELS:
            break
        if stripped.lower().startswith("данные родств"):
            i += 1
            continue

        # Поля военного
        low = stripped.lower()
        if low.startswith("без вести"):
            val = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if val and val != "-":
                notes_parts.append(f"Без вести: {val}")
                military["status"] = "missing"
        elif low.startswith("дата смерти"):
            val = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if val and val != "-":
                notes_parts.append(f"Дата смерти: {val}")
                military["status"] = "killed"
        elif low.startswith("позывной") or "позывной" in low:
            # Позывной может быть в строке "позывной X" или "Позывной: X"
            cs = re.sub(r"^.*позывной[:\s]*", "", stripped, flags=re.IGNORECASE).strip()
            cs = cs.strip(".,«»\"' ")
            if cs:
                military["extra"]["callsign"] = cs
        elif low.startswith("в/ч") or low.startswith("вч") or "в/ч" in low or " вч " in low:
            military["extra"]["unit"] = stripped
        else:
            # Любая другая строка — добавляем в note
            notes_parts.append(stripped)

        i += 1

    if notes_parts:
        military["extra"]["note"] = " | ".join(notes_parts)

    # Парсим родственников до конца блока
    while i < len(lines):
        # Ищем строку с меткой "Жена:" / "Мать:" / ...
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        first_word = line.split(":")[0].strip().lower() if ":" in line else ""
        if first_word in RELATIVE_LABELS:
            rel, consumed = _parse_relative_block(lines, i, role=first_word)
            if rel:
                military["relatives"].append(rel)
            i += consumed
        else:
            i += 1

    return military


def _parse_relative_block(lines: list[str], start: int, role: str) -> tuple[Optional[dict], int]:
    """
    Распарсить блок одного родственника начиная с строки 'Жена: ФИО ДР'.
    Возвращает (relative_dict | None, сколько строк съели).
    """
    header_line = lines[start].strip()
    # Извлекаем ФИО+ДР после двоеточия
    parts = header_line.split(":", 1)
    if len(parts) != 2:
        return None, 1

    rest = parts[1].strip()

    # Может быть "ФИО ДД.ММ.ГГГГ" или просто "Имя"
    m = MILITARY_RE.match(rest)
    if m:
        full_name = m.group(1).strip()
        birth_date = _parse_date(m.group(2))
    else:
        # Пробуем взять только ФИО до конца строки, если есть несколько слов с заглавной
        full_name = rest
        # Если в этой же строке есть телефон — обрезаем
        m2 = re.search(r"(\d{10,})", full_name)
        if m2:
            full_name = full_name[:m2.start()].strip()
        birth_date = None
        # Может ДР на следующей строке
        if start + 1 < len(lines):
            next_line = lines[start + 1].strip()
            d = _parse_date(next_line)
            if d:
                birth_date = d

    rel = {
        "full_name": full_name,
        "birth_date": birth_date,
        "phone": "",
        "address": "",
        "extra": {"role": role},
        "_role": role,
    }

    # Идём по следующим строкам пока не упрёмся в новый блок (новый родственник или новый военный)
    i = start + 1
    consumed = 1
    address_lines = []

    while i < len(lines):
        s = lines[i].strip()

        if not s:
            i += 1
            consumed += 1
            continue

        # Конец блока — следующий родственник
        first_word = s.split(":")[0].strip().lower() if ":" in s else ""
        if first_word in RELATIVE_LABELS:
            break

        # Конец блока — следующий военный (начинается с "+" или похож на новую запись)
        if s.startswith("+"):
            break

        # Конец блока — пустые строки идут (>= 2 пустых) и след строка — заголовок
        # это уже обработали через RELATIVE_LABELS

        low = s.lower()
        if low.startswith("телефон"):
            val = s.split(":", 1)[1].strip() if ":" in s else ""
            phones = re.findall(r"\d{10,}", val)
            if phones:
                rel["phone"] = _normalize_phone(phones[0])
                if len(phones) > 1:
                    rel["extra"]["phones_other"] = ", ".join(_normalize_phone(p) for p in phones[1:])
        elif low.startswith("почта") or low.startswith("email"):
            val = s.split(":", 1)[1].strip() if ":" in s else ""
            val = _clean_value(val)
            if val:
                rel["extra"]["email"] = val.lower()
        elif low.startswith("паспорт"):
            val = s.split(":", 1)[1].strip() if ":" in s else ""
            val = re.sub(r"\D", "", val)
            if val:
                rel["extra"]["passport"] = val
        elif low.startswith("снилс"):
            val = s.split(":", 1)[1].strip() if ":" in s else ""
            val = re.sub(r"\D", "", val)
            if val:
                rel["extra"]["snils"] = val
        elif low.startswith("инн"):
            val = s.split(":", 1)[1].strip() if ":" in s else ""
            val = re.sub(r"\D", "", val)
            if val:
                rel["extra"]["inn"] = val
        else:
            # Иначе — это часть адреса или дополнительная инфа
            # Адрес часто без префикса в конце блока
            if any(kw in low for kw in ["обл", "респ", "край", "г.", " г ", "ул", "д.", "кв", "р-н"]):
                address_lines.append(s)
            elif s.startswith(("1)", "2)", "3)")):  # например "1) ..." адреса
                address_lines.append(re.sub(r"^\d+\)\s*", "", s))
            else:
                # Пометки типа "Есть 2 ребенка", "2 младших сестры"
                rel["extra"].setdefault("note", "")
                if rel["extra"]["note"]:
                    rel["extra"]["note"] += " | " + s
                else:
                    rel["extra"]["note"] = s

        i += 1
        consumed += 1

    if address_lines:
        rel["address"] = "; ".join(address_lines)

    return rel, consumed


# ────────── Точка входа для отладки ──────────

if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Миша.txt"
    result = parse_file(path)

    print(f"Распарсено военных: {len(result)}\n")
    for i, m in enumerate(result, 1):
        bd = m['birth_date'].strftime('%d.%m.%Y') if m['birth_date'] else '—'
        print(f"{i}. {m['full_name']} ({bd}) — {m['status']}")
        if m['extra'].get('callsign'):
            print(f"   Позывной: {m['extra']['callsign']}")
        if m['extra'].get('unit'):
            print(f"   В/ч: {m['extra']['unit']}")
        if m['extra'].get('note'):
            print(f"   Note: {m['extra']['note'][:80]}...")
        for r in m['relatives']:
            rbd = r['birth_date'].strftime('%d.%m.%Y') if r['birth_date'] else '—'
            print(f"   • [{r['_role']}] {r['full_name']} ({rbd}) — phone={r['phone']}")
        print()
