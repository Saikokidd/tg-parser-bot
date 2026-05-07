from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.db.queries import get_or_create_manager, get_manager_persons

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Регистрируем менеджера при первом контакте"""
    await get_or_create_manager(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or ""
    )

    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я бот для внесения данных. Просто отправь мне информацию о человеке "
        f"в свободной форме, и я распознаю и сохраню её.\n\n"
        f"*Пример:*\n"
        f"Иванов Иван Иванович, 15.03.1985, 79999688666, позывной Север, б/ч 1234\n\n"
        f"*Доступные команды:*\n"
        f"/help — помощь\n"
        f"/mybase — моя база записей",
        parse_mode="Markdown"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Как вносить данные:*\n\n"
        "Отправьте сообщение с данными в любом удобном формате. "
        "Бот умеет распознавать:\n\n"
        "👤 *ФИО* — минимум Фамилия Имя\n"
        "🎂 *Дата рождения* — форматы: 15.03.1985, 15/03/85\n"
        "📞 *Телефон* — любой формат номера\n"
        "🎖 *Позывной* — после слова 'позывной'\n"
        "🏠 *Б/Ч* — после 'б/ч' или 'боевая часть'\n"
        "📌 *БЗ* — после 'б/з' или 'боевое задание'\n"
        "❓ *БП* — слово 'БП' или 'безвести пропавший'\n\n"
        "*Примеры:*\n"
        "• Иванов Иван Иванович 15.03.1985 79999688666\n"
        "• Петров П.П., дата 01/01/90, тел 79999688666, позывной Гром, б/ч 5544",
        parse_mode="Markdown"
    )


@router.message(Command("mybase"))
async def cmd_mybase(message: Message):
    manager = await get_or_create_manager(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or ""
    )
    persons = await get_manager_persons(manager['id'])

    if not persons:
        await message.answer("📭 Ваша база пустая.")
        return

    await message.answer(f"📊 Ваша база: *{len(persons)} записей*", parse_mode="Markdown")
