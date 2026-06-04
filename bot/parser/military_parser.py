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
# ────────── Исключения строгого парсера ──────────

class MilitaryStrictError(Exception):
    """
    Исключение для нового строгого ввода 'ФИО + ДР'.
    Сообщение в `args[0]` — готовый текст для менеджера.
    """
    pass


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

    Обязательно: ФИО + дата рождения.
    Без ДР: пробив через Sauron возвращает 70+ совпадений (мусор),
    выгрузка получается неполной — запись бесполезна.
    Статус не обязателен — добавит позже редактированием.
    """
    if not data.get('full_name'):
        return (
            "Не указано ФИО. Примеры формата:\n"
            "• `ФИО: Иванов Иван Иванович`\n"
            "• `Иванов Иван Иванович 15.03.1985`\n"
            "• `- Иванов Иван Иванович 15.03.1985 200`"
        )
    if not data.get('birth_date'):
        return (
            "Не указана дата рождения. Без ДР пробив возвращает много "
            "лишних связей. Укажите ДР в одном из форматов:\n"
            "• `15.03.1985`\n"
            "• `15/03/1985`\n"
            "• `15 03 1985`\n\n"
            "Примеры полного шаблона:\n"
            "• `Иванов Иван Иванович 15.03.1985`\n"
            "• `Иванов Иван Иванович, 15.03.1985 200`"
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


# ────────── СТРОГИЙ ПАРСЕР (новый флоу) ──────────

# Минимальная длина ФИО — 2 слова (фамилия + имя), 3 рекомендовано.
# Слова разделены одним или несколькими пробелами; допустим дефис (двойные фамилии).
_FULLNAME_WORD = r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-']*"
_DATE_PART = r"\d{1,2}[./\-\s]\d{1,2}[./\-\s]\d{2,4}"


def parse_military_strict(text: str) -> dict:
    """
    Строгий парсер для нового упрощённого флоу.

    Принимаем ТОЛЬКО:
        - 'Фамилия Имя Отчество ДД.ММ.ГГГГ'
        - С запятой: 'Фамилия Имя Отчество, ДД.ММ.ГГГГ'
        - С тире: '- Фамилия Имя Отчество ДД.ММ.ГГГГ'
        - Разделители даты: '.', '/', '-', пробел

    ОТКЛОНЯЕМ всё остальное:
        - двоеточия (старый шаблон с подписями)
        - несколько строк
        - лишние слова после даты (статус/БЧ/позывной)

    Возвращает {'full_name': str, 'birth_date': date, 'status': None, 'extra': {}}

    Бросает MilitaryStrictError с понятным сообщением для менеджера.
    """
    if not text or not text.strip():
        raise MilitaryStrictError(
            "Пустое сообщение. Отправьте: <b>Фамилия Имя Отчество ДД.ММ.ГГГГ</b>"
        )

    raw = text.strip()

    # Склеиваем переносы строк в один пробел — менеджеры часто шлют
    # ФИО и дату с переносом (автоперенос мобильной клавиатуры или Enter).
    # Пустые строки игнорируем.
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        raise MilitaryStrictError(
            "Пустое сообщение. Отправьте: <b>Фамилия Имя Отчество ДД.ММ.ГГГГ</b>"
        )
    line = " ".join(lines)

    # Отклоняем шаблон с подписями (ФИО:, ДР:, Статус: и т.д.)
    if ':' in line:
        raise MilitaryStrictError(
            "Не нужно писать подписи (ФИО:, ДР: и т.п.).\n\n"
            "Просто: <code>Иванов Иван Иванович 15.03.1985</code>\n\n"
            "Доп. информацию внесёте на следующем шаге."
        )

    # Убираем лидирующее тире/буллет/звёздочку
    line = re.sub(r"^[\-\*•·]\s+", "", line)
    # Убираем запятую перед датой: "Иванов Иван Иванович, 15.03.1985"
    line = re.sub(r",\s*", " ", line)
    # Нормализуем множественные пробелы
    line = re.sub(r"\s+", " ", line).strip()

    # Ищем дату в конце строки
    date_match = re.search(rf"\b({_DATE_PART})$", line)
    if not date_match:
        raise MilitaryStrictError(
            "Не нашёл дату рождения в конце сообщения.\n\n"
            "Формат: <code>Иванов Иван Иванович 15.03.1985</code>"
        )

    date_str = date_match.group(1)
    name_part = line[:date_match.start()].strip()

    # Парсим дату
    birth_date = parse_date(date_str)
    if birth_date is None:
        raise MilitaryStrictError(
            f"Не смог разобрать дату <b>{date_str}</b>.\n\n"
            "Формат: <code>ДД.ММ.ГГГГ</code> (например 15.03.1985)"
        )

    # Проверяем ФИО — минимум 2 слова, только буквы/дефис/апостроф
    name_words = name_part.split()
    if len(name_words) < 2:
        raise MilitaryStrictError(
            "ФИО должно содержать минимум 2 слова (фамилия + имя).\n\n"
            "Пример: <code>Иванов Иван Иванович 15.03.1985</code>"
        )

    # Каждое слово должно состоять только из букв/дефиса/апострофа
    for w in name_words:
        if not re.fullmatch(_FULLNAME_WORD, w):
            raise MilitaryStrictError(
                f"Слово <b>{w}</b> не похоже на часть имени.\n\n"
                "В ФИО допустимы только буквы (и дефис в двойных фамилиях).\n"
                "Пример: <code>Иванов Иван Иванович 15.03.1985</code>"
            )

    full_name = " ".join(name_words)

    return {
        "full_name": full_name,
        "birth_date": birth_date,
        "status": None,
        "extra": {},
    }