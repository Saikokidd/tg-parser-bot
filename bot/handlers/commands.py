"""
Базовые команды: /start, /help
"""
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.keyboards.menus import main_menu

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, is_admin: bool, is_supervisor: bool, manager: dict | None):
    if is_admin:
        role = "👑 Администратор"
    elif is_supervisor:
        role = "🎛 Пульт"
    elif manager:
        role = f"👤 Менеджер ({manager['name']})"
    else:
        role = "—"

    await message.answer(
        f"Роль: {role}\n\n"
        f"Используйте кнопки меню для работы.",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin=is_admin, is_supervisor=is_supervisor)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды и кнопки:*\n\n"
        "🔍 *Пробить* — внести нового военного по шаблону\n"
        "✍️ *Заполнить* — выбрать военного и добавить родственника\n"
        "📋 *Не заполнены* — список лидов по которым ещё не собраны связи\n"
        "📊 *Моя база* — статистика вашей базы\n\n"
        "Команда `/cancel` — отменить текущее действие.",
        parse_mode="Markdown"
    )
