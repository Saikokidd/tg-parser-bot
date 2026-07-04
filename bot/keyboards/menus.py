from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)


# ════════════════════════════════════════════════════════════
#                    ГЛАВНОЕ МЕНЮ
# ════════════════════════════════════════════════════════════

def main_menu(is_admin: bool = False, is_supervisor: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню (reply-клавиатура)"""
    if is_supervisor:
        rows = [
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="Список лидов")],
        ]
    else:
        rows = [
            [KeyboardButton(text="🔍 Пробить")],
            [KeyboardButton(text="✍️ Заполнить")],
            [KeyboardButton(text="Список лидов")],
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📤 Выгрузить лидов")],
            [KeyboardButton(text="📌 Мои источники")],
        ]
        if is_admin:
            rows.append([KeyboardButton(text="⚙️ Управление ботом")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )


# ════════════════════════════════════════════════════════════
#                    АДМИН: УПРАВЛЕНИЕ
# ════════════════════════════════════════════════════════════

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Менеджеры", callback_data="admin:managers")],
        [InlineKeyboardButton(text="💰 Расход на пробив", callback_data="admin:cost")],
        [InlineKeyboardButton(text="🔎 Поиск лида", callback_data="admin:search")],
    ])


def managers_menu(is_super_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Меню управления менеджерами.

    is_super_admin: если True — показывается дополнительная кнопка
                   '🏢 Сменить офис менеджера' (перенос между офисами).
                   У office_admin её нет — это серьёзное действие.
    """
    rows = [
        [InlineKeyboardButton(text="➕ Добавить менеджера", callback_data="mgr:add")],
        [InlineKeyboardButton(text="🔄 Изменить ID менеджера", callback_data="mgr:edit_id")],
    ]
    if is_super_admin:
        rows.append([InlineKeyboardButton(text="🏢 Сменить офис менеджера",
                                          callback_data="mgr:change_office")])
    rows.extend([
        [InlineKeyboardButton(text="📋 Список менеджеров", callback_data="mgr:list")],
        [InlineKeyboardButton(text="🚫 Отключить менеджера", callback_data="mgr:disable_list")],
        [InlineKeyboardButton(text="✅ Включить менеджера", callback_data="mgr:enable_list")],
        [InlineKeyboardButton(text="❌ Удалить менеджера", callback_data="mgr:delete")],
        [InlineKeyboardButton(text="« Назад", callback_data="admin:back")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_managers() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« К меню менеджеров", callback_data="admin:managers")]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])


def managers_list_kb(managers: list, action: str) -> InlineKeyboardMarkup:
    """Список менеджеров кнопками. action: 'edit_id' | 'delete' | 'change_office'"""
    rows = []
    for m in managers:
        office = m.get("office") or "—"
        rows.append([
            InlineKeyboardButton(
                text=f"[{office}] 👤 {m['name']}",
                callback_data=f"mgr_select:{action}:{m['id']}"
            )
        ])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="admin:managers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def office_choice_kb(context: str, manager_id: int | None = None,
                     restrict_to_office: str | None = None) -> InlineKeyboardMarkup:
    """
    Выбор офиса (pvl / dp / ha).

    context:
        'add'    — при добавлении нового менеджера. manager_id=None.
                   callback_data: 'office:add:pvl' / 'office:add:dp' / 'office:add:ha'
        'change' — при смене офиса существующего. manager_id обязателен.
                   callback_data: 'office:change:{id}:pvl' / 'office:change:{id}:dp' / 'office:change:{id}:ha'

    restrict_to_office: если задан — рисуем кнопку только для этого офиса
                       (используется для office_admin при добавлении: не давать выбор офиса).
    """
    all_offices = ("pvl", "dp", "ha")
    offices = (restrict_to_office,) if restrict_to_office else all_offices

    if context == "add":
        def cb(o): return f"office:add:{o}"
    elif context == "change":
        if manager_id is None:
            raise ValueError("manager_id обязателен для context='change'")
        def cb(o): return f"office:change:{manager_id}:{o}"
    else:
        raise ValueError(f"Неизвестный context: {context!r}")

    row = [InlineKeyboardButton(text=f"🏢 {o}", callback_data=cb(o)) for o in offices]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="« Отмена", callback_data="admin:managers")],
    ])


def confirm_delete_kb(manager_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"mgr_del_confirm:{manager_id}"),
            InlineKeyboardButton(text="« Отмена", callback_data="admin:managers"),
        ]
    ])


# ════════════════════════════════════════════════════════════
#         ВОЕННЫЙ: ПОДТВЕРЖДЕНИЕ ЗАПИСИ + СБОР РОДСТВЕННИКОВ
# ════════════════════════════════════════════════════════════

def confirm_military_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="mil:save"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="mil:cancel"),
        ]
    ])

def take_over_military_kb(military_id: int) -> InlineKeyboardMarkup:
    """Кнопки для перехвата пустого лида (только pvl)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Забрать себе и заполнить",
                              callback_data=f"mil:takeover:{military_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="mil:cancel")],
    ])
    

def confirm_military_with_dups_kb() -> InlineKeyboardMarkup:
    """Когда есть дубли — отдельные тексты на кнопках"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Всё равно сохранить", callback_data="mil:save"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="mil:cancel"),
        ]
    ])


def ask_relatives_kb(military_id: int) -> InlineKeyboardMarkup:
    """После сохранения военного — спросить про родственников"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Заополнить сейчас",
                              callback_data=f"rel:start:{military_id}")],
        [InlineKeyboardButton(text="⏭ Позже",
                              callback_data=f"rel:later:{military_id}")],
    ])


# ════════════════════════════════════════════════════════════
#         РОДСТВЕННИК: ВЫБОР ВОЕННОГО + ПОДТВЕРЖДЕНИЕ
# ════════════════════════════════════════════════════════════

def military_list_kb(records: list, action: str = "rel:pick") -> InlineKeyboardMarkup:
    """Список военных кнопками для выбора"""
    rows = []
    for r in records:
        birth = r.get('birth_date')
        birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
        label = f"{r['full_name']} • {birth_str}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"{action}:{r['id']}")
        ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fill_action_kb(military_id: int) -> InlineKeyboardMarkup:
    """
    После выбора лида в '✍️ Заполнить' — выбор действия:
    🔍 Пробить через Sauron
    ✍️ Заполнить вручную
    ❌ Отмена
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔍 Пробить через Sauron",
            callback_data=f"rel:probiv:{military_id}"
        )],
        [InlineKeyboardButton(
            text="✍️ Заполнить вручную",
            callback_data=f"rel:manual:{military_id}"
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])


def confirm_relative_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="rel:save"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="rel:cancel"),
        ]
    ])


def add_more_relatives_kb(military_id: int) -> InlineKeyboardMarkup:
    """После сохранения родственника — спросить добавить ещё"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё",
                              callback_data=f"rel:more:{military_id}")],
        [InlineKeyboardButton(text="✅ Готово",
                              callback_data="rel:done")],
    ])
    

# ════════════════════════════════════════════════════════════
#                       ПРОБИВ
# ════════════════════════════════════════════════════════════
# Максимум кнопок в одном сообщении.
# Telegram-лимит на inline_keyboard: до 100 кнопок в массиве,
# callback_data ≤ 64 байт. У нас короткий callback (probiv:next:N),
# держим 30 — покрывает 99% реальных ответов Sauron.
# Если когда-то Sauron вернёт больше — последние имена показаны
# в тексте, но кнопок для них не будет; в таком случае поднять
# лимит или добавить пагинацию.
PROBIV_BUTTONS_MAX = 30


def probiv_persons_kb(persons: list[dict], page: int = 1,
                       page_size: int = 15) -> InlineKeyboardMarkup:
    """
    Постраничная клавиатура людей из 'возможных связей'.

    Принимает уже дедуплицированный плоский список persons (см.
    sauron_parser._dedup_persons_from_blocks).

    На странице — page_size кнопок, под ними — навигация.
    Глобальная сквозная нумерация кнопок (16, 17, ...) совпадает с
    нумерацией в тексте сообщения.

    Навигация (Q2 = вариант c): скрываем неактивные кнопки.
    Если страница одна — нав-ряда нет вообще.
    """
    if not persons:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data="probiv:done")
        ]])

    total = len(persons)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = min(start + page_size, total)

    rows = []
    for i in range(start, end):
        p = persons[i]
        label = p["full_name"]
        if p["birth_date_str"]:
            label += f" • {p['birth_date_str']}"
        if len(label) > 55:
            label = label[:52] + "..."
        # Добавляем номер для соответствия с текстом
        rows.append([
            InlineKeyboardButton(
                text=f"🔍 {i + 1}. {label}",
                callback_data=f"probiv:next:{i}",
            )
        ])

    # Навигация (только если страниц больше одной)
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"probiv:page:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data="probiv:page:noop",  # инфо-кнопка, не делает ничего
        ))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"probiv:page:{page + 1}",
            ))
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="probiv:done")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def attach_relative_kb(person_idx: int, military_label: str) -> InlineKeyboardMarkup:
    """
    Кнопки после показа шаблона родственника:
    📌 Закрепить за {ФИО военного}
    📂 Закрепить позже (просто убирает кнопки)
    """
    # Обрезаем подпись если ФИО длинное
    if len(military_label) > 35:
        military_label = military_label[:32] + "..."

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📌 Закрепить за {military_label}",
            callback_data=f"attach:do:{person_idx}"
        )],
        [InlineKeyboardButton(
            text="📂 Закрепить позже",
            callback_data=f"attach:later:{person_idx}"
        )],
    ])


def attach_duplicate_kb(person_idx: int) -> InlineKeyboardMarkup:
    """
    При обнаружении дубля родственника — два пути:
    - Закрепить как нового (новая запись + связка)
    - Использовать существующего (только новая связка)
    - Отмена
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Закрепить как нового",
            callback_data=f"attach:dup:new:{person_idx}"
        )],
        [InlineKeyboardButton(
            text="♻ Использовать существующего",
            callback_data=f"attach:dup:reuse:{person_idx}"
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="attach:dup:cancel")],
    ])


# ════════════════════════════════════════════════════════════
#                       СТАТИСТИКА
# ════════════════════════════════════════════════════════════

def stats_period_kb(period: str = None, page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """
    Клавиатура статистики.
    Сверху — кнопки переключения страниц (только если total_pages > 1).
    Внизу — кнопки переключения периода (с маркером текущего).
    """
    rows = []

    # Пагинация (только когда страниц больше одной)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀", callback_data=f"stats:page:{period}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="stats:noop"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶", callback_data=f"stats:page:{period}:{page + 1}"
            ))
        rows.append(nav)

    # Кнопки периодов с маркером
    def label(text, p):
        return f"• {text} •" if p == period else text

    rows.append([
        InlineKeyboardButton(
            text=label("За сегодня", "today"),
            callback_data="stats:today"
        ),
        InlineKeyboardButton(
            text=label("За неделю", "week"),
            callback_data="stats:week"
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text=label("За всё время", "all"),
            callback_data="stats:all"
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
    
    
# ════════════════════════════════════════════════════════════
#                       СПИСОК ЛИДОВ
# ════════════════════════════════════════════════════════════

def leads_list_kb(records: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """
    Список военных кнопками с пагинацией.
    Каждая кнопка — карточка лида.
    """
    rows = []
    for r in records:
        birth = r.get('birth_date')
        birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
        rel_count = r.get('relatives_count', 0)
        marker = '✓' if rel_count > 0 else '○'
        label = f"{marker} {r['full_name']} • {birth_str} ({rel_count})"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"lead:show:{r['id']}")
        ])

    # Пагинация
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"lead:page:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="lead:noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="Далее »", callback_data=f"lead:page:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def lead_card_kb(military_id: int, relatives: list) -> InlineKeyboardMarkup:
    """
    Кнопки в карточке лида:
    - на каждого родственника: ✏️ редактировать / 🗑 удалить
    - ➕ Дополнить (добавить ещё родственника)
    - 🗑 Удалить лида
    - « Назад к списку
    """
    rows = []
    for r in relatives:
        name = r.get('full_name', '—')
        if len(name) > 35:
            name = name[:32] + "..."
        rows.append([
            InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"rel:edit:{r['id']}:{military_id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"rel:del:{r['id']}:{military_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="➕ Дополнить", callback_data=f"lead:addrel:{military_id}")
    ])
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить лида", callback_data=f"lead:del:{military_id}")
    ])
    rows.append([
        InlineKeyboardButton(text="« К списку", callback_data="lead:back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_lead_kb(military_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"lead:del_yes:{military_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"lead:show:{military_id}"),
        ]
    ])


def confirm_delete_relative_kb(relative_id: int, military_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"rel:del_yes:{relative_id}:{military_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"lead:show:{military_id}"),
        ]
    ])


# ════════════════════════════════════════════════════════════
#                  РЕДАКТИРОВАНИЕ РОДСТВЕННИКА
# ════════════════════════════════════════════════════════════

# Поля редактирования: (callback_id, лейбл, тип)
EDIT_FIELDS = [
    ("full_name", "ФИО", "structural"),
    ("birth_date", "ДР", "structural"),
    ("phone", "Телефон", "structural"),
    ("address", "Адрес", "structural"),
    ("snils", "СНИЛС", "extra"),
    ("inn", "ИНН", "extra"),
    ("passport", "Паспорт", "extra"),
    ("email", "Почта", "extra"),
]


def edit_relative_fields_kb(relative_id: int, military_id: int) -> InlineKeyboardMarkup:
    """Меню выбора поля для редактирования"""
    rows = []
    # По 2 кнопки в ряд
    for i in range(0, len(EDIT_FIELDS), 2):
        row = []
        for field_id, label, _ in EDIT_FIELDS[i:i+2]:
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"rel:editfield:{relative_id}:{military_id}:{field_id}"
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(
            text="➕ Своё поле",
            callback_data=f"rel:editfield:{relative_id}:{military_id}:_custom"
        )
    ])
    rows.append([
        InlineKeyboardButton(text="« Назад", callback_data=f"lead:show:{military_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ════════════════════════════════════════════════════════════
#                       ВЫГРУЗКА ЛИДОВ
# ════════════════════════════════════════════════════════════

def export_count_kb(available: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора количества для выгрузки"""
    rows = [
        [InlineKeyboardButton(
            text=f"📤 Выгрузить всё ({available})",
            callback_data="export:all"
        )],
        [InlineKeyboardButton(
            text="🔢 Указать количество",
            callback_data="export:custom"
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="export:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ════════════════════════════════════════════════════════════
#                  РАСХОД НА ПРОБИВ (АДМИН)
# ════════════════════════════════════════════════════════════

def cost_menu_kb(show_noattach: bool = True) -> InlineKeyboardMarkup:
    """
    Главное меню раздела 'Расход на пробив'.

    show_noattach: показывать кнопку '🤖 Без привязки' (расход cron/enricher
                   без менеджера). True — для super_admin, False — для office_admin
                   (служебный расход к офису не относится).
    """
    rows = [
        [InlineKeyboardButton(text="📊 Общий", callback_data="cost:total")],
        [InlineKeyboardButton(text="👤 По менеджерам", callback_data="cost:by_mgr")],
    ]
    if show_noattach:
        rows.append([InlineKeyboardButton(text="🤖 Без привязки", callback_data="cost:noattach")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cost_period_kb(section: str, period: str = "week",
                   page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """
    Клавиатура раздела расхода.

    section: 'total' / 'by_mgr' / 'noattach'
    period:  'week' / 'month' / 'all'
    page, total_pages: для пагинации (используется только в by_mgr)
    """
    rows = []

    # Пагинация (только для by_mgr и только если страниц больше одной)
    if section == "by_mgr" and total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀",
                callback_data=f"cost:by_mgr:{period}:{page - 1}",
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="cost:noop",
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶",
                callback_data=f"cost:by_mgr:{period}:{page + 1}",
            ))
        rows.append(nav)

    # Кнопки периодов с маркером текущего
    def label(text, p):
        return f"• {text} •" if p == period else text

    # Для каждого периода — свой callback в зависимости от раздела.
    # by_mgr → cost:by_mgr:{period}:0  (страница всегда сбрасывается на 0)
    # остальные → cost:{section}:{period}
    def cb(p):
        if section == "by_mgr":
            return f"cost:by_mgr:{p}:0"
        return f"cost:{section}:{p}"

    rows.append([
        InlineKeyboardButton(text=label("Сегодня", "today"), callback_data=cb("today")),
        InlineKeyboardButton(text=label("Неделя", "week"), callback_data=cb("week")),
    ])
    rows.append([
        InlineKeyboardButton(text=label("Месяц", "month"), callback_data=cb("month")),
        InlineKeyboardButton(text=label("Всё время", "all"), callback_data=cb("all")),
    ])
    rows.append([
        InlineKeyboardButton(text="« К меню расхода", callback_data="admin:cost"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────── Поиск лида (Q-Найти лида) ────────────

def search_results_kb(military: list[dict], relatives: list[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура с результатами поиска.
    Кнопки:
        🪖 ФИО ДД.ММ.ГГГГ            → откроет карточку военного
        👨‍👩 ФИО ДД.ММ.ГГГГ           → откроет карточку лида к которому привязан родственник
        « Закрыть
    Каждая кнопка с коротким callback (search:m:ID или search:r:ID).
    """
    rows = []

    for m in military:
        birth = m.get("birth_date")
        birth_str = birth.strftime("%d.%m.%Y") if birth else "—"
        label = f"[ВОЕН] {m['full_name']} • {birth_str}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"search:m:{m['id']}",
            )
        ])

    for r in relatives:
        birth = r.get("birth_date")
        birth_str = birth.strftime("%d.%m.%Y") if birth else "—"
        label = f"[РОДСТ] {r['full_name']} • {birth_str}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"search:r:{r['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="« Закрыть", callback_data="search:close")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


    # ════════════════════════════════════════════════════════════════
#  Sources — реестр источников лидов
# ════════════════════════════════════════════════════════════════

def source_pick_kb(
    sources: list[dict],
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """
    Выбор источника при создании лида (вместо текстового ввода).
    
    Кнопки:
        📌 <name>  ← по 1 на строку
        ◀ N/M ▶    ← если total_pages > 1, скрытие неактивных
        ✏️ Свой вариант
        ❌ Без источника
    
    callback_data:
        src:pick:<id>     — выбран источник из списка
        src:page:<N>      — переключить страницу
        src:page:noop     — инфо-кнопка N/M
        src:custom        — открыть ввод своего варианта
        src:none          — без источника
    """
    rows = []

    for s in sources:
        name = s["name"]
        label = f"📌 {name}"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"src:pick:{s['id']}",
            )
        ])

    # Навигация (только если страниц больше одной)
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"src:page:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data="src:page:noop",
        ))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"src:page:{page + 1}",
            ))
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="✏️ Свой вариант", callback_data="src:custom")
    ])
    rows.append([
        InlineKeyboardButton(text="❌ Без источника", callback_data="src:none")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_sources_list_kb(
    sources: list[dict],
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """
    Меню "📌 Мои источники" — список с пагинацией.
    Клик на источник → карточка.

    callback_data:
        mysrc:open:<id>
        mysrc:page:<N>
        mysrc:page:noop
        mysrc:close
    """
    rows = []

    for s in sources:
        name = s["name"]
        label = f"📌 {name}"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"mysrc:open:{s['id']}",
            )
        ])

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"mysrc:page:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data="mysrc:page:noop",
        ))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"mysrc:page:{page + 1}",
            ))
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="« Закрыть", callback_data="mysrc:close")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_card_kb(source_id: int) -> InlineKeyboardMarkup:
    """
    Карточка источника в "Мои источники".
    Кнопки: переименовать / удалить / назад.

    callback_data:
        mysrc:rename:<id>
        mysrc:delete:<id>
        mysrc:back
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать",
                              callback_data=f"mysrc:rename:{source_id}")],
        [InlineKeyboardButton(text="❌ Удалить",
                              callback_data=f"mysrc:delete:{source_id}")],
        [InlineKeyboardButton(text="« Назад к списку",
                              callback_data="mysrc:back")],
    ])


def confirm_delete_source_kb(source_id: int) -> InlineKeyboardMarkup:
    """
    Подтверждение удаления источника.
    Показывается с текстом сколько лидов на нём.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"mysrc:delete_confirm:{source_id}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"mysrc:open:{source_id}")],
    ])


def manager_pick_kb(
    managers: list[dict],
    page: int,
    total_pages: int,
    action: str,
) -> InlineKeyboardMarkup:
    """
    Список менеджеров с кнопками для одиночного действия.
    
    managers — окно по странице (10 шт max)
    action — 'disable' или 'enable' (используется в callback_data)
    callback_data:
        mgr:<action>_pick:<id>      — клик на менеджера
        mgr:<action>_page:<N>       — переключить страницу
        mgr:<action>_page:noop      — инфо-кнопка N/M
        admin:managers              — назад
    """
    rows = []
    for m in managers:
        office = m.get("office") or "—"
        role_mark = " 👑" if m.get("role") == "admin" else ""
        name = m["name"]
        label = f"[{office}] {name}{role_mark}"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"mgr:{action}_pick:{m['id']}",
            )
        ])
    
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"mgr:{action}_page:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data=f"mgr:{action}_page:noop",
        ))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"mgr:{action}_page:{page + 1}",
            ))
        rows.append(nav_row)
    
    rows.append([InlineKeyboardButton(
        text="« К меню менеджеров",
        callback_data="admin:managers",
    )])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)