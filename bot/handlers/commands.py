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
        f"👋 Привет, *{name}*!\n"
        f"Роль: {role}\n\n"
        f"Используйте кнопки меню для работы.",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin=is_admin)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды и кнопки:*\n\n"
        "🔍 *Пробить военного* — внести нового военного по шаблону\n"
        "✍️ *Заполнить родственников* — выбрать военного и добавить родственника\n"
        "📋 *Без родственников* — список военных по которым ещё не собраны родственники\n"
        "📊 *Моя база* — статистика вашей базы\n\n"
        "Команда `/cancel` — отменить текущее действие.",
        parse_mode="Markdown"
    )
