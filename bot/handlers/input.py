from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.parser.text_parser import parse_text, format_parsed, format_person_record
from bot.db.queries import find_duplicates, insert_person
from bot.keyboards.menus import confirm_record_kb

router = Router()


class InputStates(StatesGroup):
    waiting_confirmation = State()


# Кнопки главного меню — игнорируем (их обрабатывает commands.py)
MENU_BUTTONS = {"📝 Внести запись", "📊 Моя база", "⚙️ Управление ботом"}


@router.message(F.text & ~F.text.startswith('/') & ~F.text.in_(MENU_BUTTONS))
async def handle_text_input(message: Message, state: FSMContext, manager: dict | None):
    # Только менеджеры могут вносить записи
    if not manager:
        await message.answer(
            "ℹ️ Только менеджеры могут вносить записи.\n"
            "Если вы администратор без своей таблицы — попросите добавить себя как менеджера."
        )
        return

    parsed = parse_text(message.text)

    if not parsed:
        await message.answer(
            "❌ Не удалось распознать данные.\n\n"
            "Пример формата:\n"
            "Иванов Иван Иванович, 15.03.1985, +380991234567, позывной Север, б/ч 1234"
        )
        return

    duplicates = await find_duplicates(
        full_name=parsed.get('full_name'),
        birth_date=parsed.get('birth_date'),
        phone=parsed.get('phone')
    )

    await state.update_data(parsed=parsed, manager_id=manager['id'])

    if duplicates:
        dup_text = "\n\n".join([format_person_record(d) for d in duplicates])
        text = (
            f"⚠️ *Найдены похожие записи ({len(duplicates)} шт.):*\n\n"
            f"{dup_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{format_parsed(parsed)}\n\n"
            f"Всё равно добавить?"
        )
    else:
        text = f"{format_parsed(parsed)}\n\nДобавить запись?"

    await state.set_state(InputStates.waiting_confirmation)
    await message.answer(text, parse_mode="Markdown", reply_markup=confirm_record_kb())


@router.callback_query(F.data == "confirm_add", InputStates.waiting_confirmation)
async def confirm_add(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed = data['parsed']
    manager_id = data['manager_id']

    await insert_person(parsed, manager_id)
    await state.clear()

    await callback.message.edit_text(
        f"✅ Запись добавлена!\n\n{format_parsed(parsed)}",
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "cancel_add", InputStates.waiting_confirmation)
async def cancel_add(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Операция отменена.")