from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)


# ============== ГЛАВНОЕ МЕНЮ ==============

def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню (reply-клавиатура)"""
    rows = [
        [KeyboardButton(text="📝 Внести запись")],
        [KeyboardButton(text="📊 Моя база")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Управление ботом")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или отправьте данные..."
    )


# ============== АДМИН: УПРАВЛЕНИЕ ==============

def admin_menu() -> InlineKeyboardMarkup:
    """Меню управления ботом для админа"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Менеджеры", callback_data="admin:managers")],
    ])


def managers_menu() -> InlineKeyboardMarkup:
    """Меню управления менеджерами"""
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
    """
    Список менеджеров кнопками для выбора.
    action: 'edit_id' | 'delete'
    """
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


# ============== ПОДТВЕРЖДЕНИЕ ЗАПИСИ ==============

def confirm_record_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add"),
        ]
    ])
