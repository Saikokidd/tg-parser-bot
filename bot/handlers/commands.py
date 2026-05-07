from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.db.queries import get_manager_persons
from bot.keyboards.menus import main_menu

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, is_admin: bool, manager: dict | None):
    name = manager['name'] if manager else message.from_user.first_name
    role = "👑 Администратор" if is_admin else "👤 Менеджер"

    await message.answer(
        f"👋 Привет, *{name}*!\n"
        f"Роль: {role}\n\n"
        f"Используйте кнопки меню или просто отправьте данные о человеке "
        f"в свободной форме.",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin=is_admin)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Как вносить данные:*\n\n"
        "Отправьте сообщение с данными в любом удобном формате. "
        "Бот распознаёт:\n\n"
        "👤 *ФИО* — минимум Фамилия Имя\n"
        "🎂 *Дата рождения* — 15.03.1985, 15/03/85\n"
        "📞 *Телефон* — украинские/российские номера\n"
        "🎖 *Позывной* — после слова 'позывной'\n"
        "🏠 *Б/Ч* — после 'б/ч' или 'боевая часть'\n"
        "📌 *БЗ* — после 'б/з' или 'боевое задание'\n"
        "❓ *БП* — слово 'БП' или 'безвести пропавший'\n\n"
        "*Примеры:*\n"
        "• Иванов Иван Иванович 15.03.1985 +380991234567\n"
        "• Петров П.П., 01/01/90, 0671234567, позывной Гром, б/ч 5544",
        parse_mode="Markdown"
    )


# ============== КНОПКИ ГЛАВНОГО МЕНЮ ==============

from aiogram import F


@router.message(F.text == "📊 Моя база")
async def btn_my_base(message: Message, manager: dict | None, is_admin: bool):
    if not manager:
        await message.answer(
            "ℹ️ У вас нет личной базы — вы администратор без записей.\n"
            "Чтобы вносить данные, попросите добавить себя в БД как менеджера."
        )
        return

    persons = await get_manager_persons(manager['id'])
    if not persons:
        await message.answer("📭 Ваша база пустая.")
        return

    await message.answer(
        f"📊 База менеджера *{manager['name']}*: *{len(persons)} записей*",
        parse_mode="Markdown"
    )


@router.message(F.text == "📝 Внести запись")
async def btn_add_record(message: Message):
    await message.answer(
        "✍️ Отправьте данные о человеке в свободной форме.\n\n"
        "*Пример:*\n"
        "Иванов Иван Иванович, 15.03.1985, +380991234567, позывной Север, б/ч 1234",
        parse_mode="Markdown"
    )