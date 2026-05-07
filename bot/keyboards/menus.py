from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)


# ════════════════════════════════════════════════════════════
#                    ГЛАВНОЕ МЕНЮ
# ════════════════════════════════════════════════════════════

def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню (reply-клавиатура)"""
    rows = [
        [KeyboardButton(text="🔍 Пробить военного")],
        [KeyboardButton(text="✍️ Заполнить родственников")],
        [KeyboardButton(text="📋 Без родственников")],
        [KeyboardButton(text="📊 Моя база")],
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
    ])


def managers_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить менеджера", callback_data="mgr:add")],
        [InlineKeyboardButton(text="🔄 Изменить ID менеджера", callback_data="mgr:edit_id")],
        [InlineKeyboardButton(text="📋 Список менеджеров", callback_data="mgr:list")],
        [InlineKeyboardButton(text="❌ Удалить менеджера", callback_data="mgr:delete")],
        [InlineKeyboardButton(text="« Назад", callback_data="admin:back")],
    ])


def back_to_managers() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« К меню менеджеров", callback_data="admin:managers")]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])


def managers_list_kb(managers: list, action: str) -> InlineKeyboardMarkup:
    """Список менеджеров кнопками. action: 'edit_id' | 'delete'"""
    rows = []
    for m in managers:
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {m['name']}",
                callback_data=f"mgr_select:{action}:{m['id']}"
            )
        ])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="admin:managers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        [InlineKeyboardButton(text="✍️ Внести родственников сейчас",
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
        [InlineKeyboardButton(text="➕ Добавить ещё родственника",
                              callback_data=f"rel:more:{military_id}")],
        [InlineKeyboardButton(text="✅ Готово",
                              callback_data="rel:done")],
    ])
    

# ════════════════════════════════════════════════════════════
#                       ПРОБИВ
# ════════════════════════════════════════════════════════════

def probiv_persons_kb(blocks: list[dict]) -> InlineKeyboardMarkup:
    """
    Список людей из 'возможных связей' кнопками.
    Каждая кнопка → пробить этого человека дальше.
    Дедуплицируем по (ФИО + ДР) чтобы один человек не дублировался между годами.
    """
    seen = set()
    rows = []
    idx = 0  # короткий ID для callback_data (Telegram лимит 64 байта)

    for block in blocks:
        for p in block["persons"]:
            key = f"{p['full_name']}|{p['birth_date_str']}"
            if key in seen:
                continue
            seen.add(key)

            label = p["full_name"]
            if p["birth_date_str"]:
                label += f" • {p['birth_date_str']}"
            if len(label) > 60:
                label = label[:57] + "..."

            rows.append([
                InlineKeyboardButton(
                    text=f"🔍 {label}",
                    callback_data=f"probiv:next:{idx}"
                )
            ])
            idx += 1

    rows.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="probiv:done")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)