"""
Базовые команды: /start, /help
"""
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.keyboards.menus import main_menu

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, is_admin: bool, manager: dict | None):
    name = manager['name'] if manager else message.from_user.first_name
    role = "👑 Администратор" if is_admin else "👤 Менеджер"

    await message.answer(
        f"Используйте кнопки меню для работы.",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin=is_admin)
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
