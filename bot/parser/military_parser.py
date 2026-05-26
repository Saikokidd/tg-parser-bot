"""
Парсер шаблона военного.

Менеджер пишет так:
    ФИО: Иванов Иван Иванович
    ДР: 15.03.1985
    Статус: погиб        (либо 200, либо любой свой текст)
    Б/Ч: 1234
    Позывной: Север
    Доп инфа: подробности

Структурные поля → отдельные колонки в БД (full_name, birth_date, status)
Гибкие поля → JSONB extra (Б/Ч, позывной, доп.инфа)
"""
import re
from datetime import date
from typing import Optional


# ────────── Алиасы ключей шаблона ──────────
FIELD_ALIASES = {
    'full_name':  ['фио', 'ф.и.о', 'ф.и.о.'],
    'birth_date': ['др', 'дата рождения', 'д.р.', 'д.р'],
    'status':     ['статус'],
    'unit':       ['б/ч', 'бч', 'боевая часть', 'в/ч'],
    'callsign':   ['позывной', 'позывн'],
    'note':       ['доп инфа', 'доп.инфа', 'доп инфо', 'дополнительно', 'примечание', 'комментарий'],
}


# ────────── Распознавание статуса ──────────
# Алиасы → каноническое значение в БД.
# Если не подошло ни под один алиас — возвращаем свободный текст.
STATUS_ALIASES = {
    'killed':  ['погиб', 'погибший', 'погибшего', 'killed', '200', 'груз 200', 'груз200'],
    'missing': ['пропал', 'пропавший', 'без вести', 'безвести пропавший',
                'бп', 'missing', '500', 'груз 500', 'груз500'],
}


def parse_status(text: str) -> Optional[str]:
    """
    Возвращает:
    - 'killed' / 'missing' если распознан стандартный статус
    - Свободный текст (обрезанный до 50 символов) если не распознан
    - None только если строка пустая
    """
    t = text.strip()
    if not t:
        return None

    t_lower = t.lower()
    for canonical, aliases in STATUS_ALIASES.items():
        if any(alias in t_lower for alias in aliases):
            return canonical

    return t[:50]


# ────────── Парсинг даты ──────────
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
    Парсит шаблон военного.

    Поддерживает два формата:
    1. Старый со ключами:
        ФИО: Иванов Иван Иванович
        ДР: 15.03.1985
        Статус: погиб
        Б/Ч: 1234
        Позывной: Север

    2. Компактный — одна строка:
        - Ковалёв Иван Вячеславович 14.03.1994
        Ковалёв Иван Вячеславович 14.03.1994 200
        Ковалёв Иван Вячеславович, 14.03.1994 БЗ

    Возвращает dict со структурными полями + extra (JSONB).
    """
    result = {
        'full_name': None,
        'birth_date': None,
        'status': None,
        'extra': {}
    }

    # Если в тексте есть строки с метками "ФИО:", "ДР:" и т.п. — старый шаблон
    has_keys = any(
        _find_field(_normalize_key(line.split(':', 1)[0])) is not None
        for line in text.splitlines()
        if ':' in line
    )

    if has_keys:
        # Старая логика по строкам с ключами
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

    # Компактный формат — пытаемся распознать одну строку
    _parse_compact(text, result)
    return result


def _parse_compact(text: str, result: dict) -> None:
    """
    Парсит компактную строку:
        [- ] ФИО [,] ДР [статус]

    Примеры:
        - Ковалёв Иван Вячеславович 14.03.1994
        Ковалёв Иван Вячеславович 14.03.1994
        Ковалёв Иван Вячеславович, 14.03.1994 200
        Ковалёв Иван Вячеславович 14.03.1994 БЗ

    Заполняет result in-place.
    """
    line = text.strip()
    if not line:
        return

    # Если несколько строк — берём первую непустую
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            line = ln
            break

    # Убираем лидирующее тире/звёздочку/булет
    line = re.sub(r'^[\-\*•\u2022]\s*', '', line)

    # Ищем дату в тексте — она будет якорем разделения "ФИО до" / "статус после"
    date_match = None
    for pattern in DATE_PATTERNS:
        # patterns используют ^...$, нам нужны без якорей — внутри строки
        # просто ищем тот же шаблон поиском по подстроке
        m = re.search(pattern.pattern[1:-1], line)  # отрезаем ^ и $
        if m:
            date_match = m
            break

    if not date_match:
        # Даты нет — попробуем взять всю строку как ФИО
        # (минимум 2 слова, иначе считаем что не распарсилось)
        words = line.split()
        if len(words) >= 2:
            result['full_name'] = ' '.join(words)
        return

    # ФИО — всё что до даты, очищаем запятые/двоеточия в конце
    full_name = line[:date_match.start()].strip().rstrip(',:;').strip()
    if full_name:
        result['full_name'] = full_name

    # Дата
    parsed_date = parse_date(date_match.group(0))
    if parsed_date:
        result['birth_date'] = parsed_date

    # После даты — статус (если есть)
    status_text = line[date_match.end():].strip().lstrip(',:;').strip()
    if status_text:
        s = parse_status(status_text)
        if s:
            result['status'] = s


def validate_military(data: dict) -> Optional[str]:
    """
    Проверяет обязательные поля. Возвращает строку с ошибкой или None если ok.

    Обязательно: ФИО.
    Статус — не обязателен (пропустим, добавит при редактировании).
    """
    if not data.get('full_name'):
        return (
            "Не указано ФИО. Примеры формата:\n"
            "• `ФИО: Иванов Иван Иванович`\n"
            "• `Иванов Иван Иванович 15.03.1985`\n"
            "• `- Иванов Иван Иванович 15.03.1985 200`"
        )
    return None


# ────────── Форматирование для показа ──────────

# Метки только для канонических статусов. Свободные показываются как есть.
STATUS_LABELS = {
    'killed':  'Погиб',
    'missing': 'Пропал',
}


def status_label(status: str) -> str:
    """Человекочитаемая метка статуса. Для свободных — возвращает как есть."""
    if not status:
        return '—'
    return STATUS_LABELS.get(status, status)


def format_military(data: dict) -> str:
    """Отформатировать распарсенного военного для показа менеджеру"""
    lines = ["Распознано:\n"]
    lines.append(f"ФИО: {data.get('full_name', '—')}")
    lines.append(f"ДР: {data['birth_date'].strftime('%d.%m.%Y') if data.get('birth_date') else '—'}")

    status = data.get('status')
    lines.append(f"⚐ Статус: {status_label(status)}")

    extra = data.get('extra', {})
    lines.append(f"Б/Ч: {extra.get('unit', '—')}")
    lines.append(f"Позывной: {extra.get('callsign', '—')}")
    if extra.get('note'):
        lines.append(f"Доп.инфа: {extra['note']}")

    return "\n".join(lines)


def format_military_record(record: dict) -> str:
    """Краткая карточка военного из БД (используется в т.ч. при показе дублей)"""
    birth = record.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    extra = record.get('extra') or {}

    parts = [
        f"{record.get('full_name', '—')}",
        f"{birth_str} | {status_label(record.get('status'))}",
    ]
    if extra.get('unit') or extra.get('callsign'):
        parts.append(f"Б/Ч: {extra.get('unit', '—')} | 🎖 {extra.get('callsign', '—')}")

    # Показываем кто внёс (только если поле есть — оно приходит из find_military_duplicates)
    manager_name = record.get('manager_name')
    if manager_name:
        parts.append(f"_Добавил:_ {manager_name}")

    return "\n".join(parts)