from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.parser.text_parser import parse_text, format_parsed, format_person_record
from bot.db.queries import get_or_create_manager, find_duplicates, insert_person

router = Router()


class InputStates(StatesGroup):
    waiting_confirmation = State()


@router.message(F.text & ~F.text.startswith('/'))
async def handle_text_input(message: Message, state: FSMContext):
    # Регистрируем/получаем менеджера
    manager = await get_or_create_manager(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or ""
    )

    # Парсим текст
    parsed = parse_text(message.text)

    if not parsed:
        await message.answer(
            "❌ Не удалось распознать данные.\n\n"
            "Пример формата:\n"
            "Иванов Иван Иванович, 15.03.1985, +380991234567, позывной Север, б/ч 1234"
        )
        return

    # Ищем дубли
    duplicates = await find_duplicates(
        full_name=parsed.get('full_name'),
        birth_date=parsed.get('birth_date'),
        phone=parsed.get('phone')
    )

    # Сохраняем в FSM-состоянии
    await state.update_data(
        parsed=parsed,
        manager_id=manager['id']
    )

    if duplicates:
        # Показываем дубли и просим подтверждения
        dup_text = "\n\n".join([format_person_record(d) for d in duplicates])
        text = (
            f"⚠️ *Найдены похожие записи ({len(duplicates)} шт.):*\n\n"
            f"{dup_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{format_parsed(parsed)}\n\n"
            f"Всё равно добавить?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, добавить", callback_data="confirm_add"),
                InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add"),
            ]
        ])
    else:
        # Дублей нет — показываем и просим подтвердить
        text = (
            f"{format_parsed(parsed)}\n\n"
            f"Добавить запись?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add"),
                InlineKeyboardButton(text="✏️ Отменить", callback_data="cancel_add"),
            ]
        ])

    await state.set_state(InputStates.waiting_confirmation)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


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
