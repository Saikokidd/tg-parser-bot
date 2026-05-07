"""
Хендлер для работы с родственниками:
- Кнопка "✍️ Заполнить родственников" → выбор военного → ввод шаблона
- Callback "rel:start:{military_id}" → сразу к вводу шаблона (после военного)
- Парсинг шаблона + дубль-чек (2 из 4 полей)
- Сохранение + создание связки + флаг relatives_collected = TRUE
- Цикл "Добавить ещё родственника?"
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.parser.relative_parser import (
    parse_relative, validate_relative, format_relative, format_relative_record
)
from bot.db.queries import (
    list_military_without_relatives, list_military_by_manager, get_military_by_id,
    find_relative_duplicates, insert_relative, link_military_relative,
    mark_relatives_collected
)
from bot.parser.military_parser import format_military_record
from bot.keyboards.menus import (
    military_list_kb, confirm_relative_kb, add_more_relatives_kb
)

router = Router()


# ──────────── FSM ────────────

class RelativeStates(StatesGroup):
    waiting_template = State()
    waiting_confirmation = State()


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "✍️ Заполнить родственников")
async def btn_fill_relatives(message: Message, state: FSMContext, manager: dict | None):
    if not manager:
        await message.answer("ℹ️ Доступно только менеджерам.")
        return

    # Сначала показываем тех у кого ещё не собраны родственники
    pending = await list_military_without_relatives(manager_id=manager['id'])

    if pending:
        await message.answer(
            f"👨‍👩‍👧 *Военные без родственников ({len(pending)}):*\n"
            f"Выберите кому добавить:",
            parse_mode="Markdown",
            reply_markup=military_list_kb(pending[:30], action="rel:pick")
        )
    else:
        # Все обработаны — показываем всех военных менеджера
        all_records = await list_military_by_manager(manager['id'])
        if not all_records:
            await message.answer("📭 У вас нет ни одного внесённого военного.")
            return
        await message.answer(
            f"✅ Все военные с собранными родственниками.\n"
            f"Если нужно добавить ещё одного родственника — выберите военного:",
            reply_markup=military_list_kb(all_records[:30], action="rel:pick")
        )


# ──────────── ВЫБОР ВОЕННОГО ────────────

@router.callback_query(F.data.startswith("rel:pick:"))
async def relative_pick_military(callback: CallbackQuery, state: FSMContext, manager: dict):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Военный не найден.")
        return

    await state.set_state(RelativeStates.waiting_template)
    await state.update_data(military_id=military_id, manager_id=manager['id'])

    await callback.message.edit_text(
        f"👤 Военный: *{military['full_name']}*\n\n"
        f"📝 *Шаблон родственника:*\n\n"
        f"```\n"
        f"ФИО: \n"
        f"ДР: \n"
        f"Адрес: \n"
        f"Телефон: \n"
        f"СНИЛС: \n"
        f"ИНН: \n"
        f"Паспорт: \n"
        f"Почта: \n"
        f"```\n\n"
        f"Скопируйте, заполните и отправьте.\n"
        f"_Обязательное поле: ФИО._",
        parse_mode="Markdown"
    )


# ──────────── СТАРТ ИЗ КНОПКИ "Внести сейчас" (после военного) ────────────

@router.callback_query(F.data.startswith("rel:start:"))
async def relative_start_after_military(callback: CallbackQuery, state: FSMContext, manager: dict):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Военный не найден.")
        return

    await state.set_state(RelativeStates.waiting_template)
    await state.update_data(military_id=military_id, manager_id=manager['id'])

    await callback.message.edit_text(
        f"👤 Военный: *{military['full_name']}*\n\n"
        f"📝 *Шаблон родственника:*\n\n"
        f"```\n"
        f"ФИО: \n"
        f"ДР: \n"
        f"Адрес: \n"
        f"Телефон: \n"
        f"СНИЛС: \n"
        f"ИНН: \n"
        f"Паспорт: \n"
        f"Почта: \n"
        f"```\n\n"
        f"Скопируйте, заполните и отправьте.",
        parse_mode="Markdown"
    )


# ──────────── ПРИЁМ ШАБЛОНА ────────────

@router.message(RelativeStates.waiting_template)
async def receive_relative_template(message: Message, state: FSMContext):
    parsed = parse_relative(message.text)

    err = validate_relative(parsed)
    if err:
        await message.answer(
            f"⚠️ {err}\n\nПопробуйте ещё раз или отправьте /cancel."
        )
        return

    # Дубль-чек 2 из 4
    duplicates = await find_relative_duplicates(
        full_name=parsed.get('full_name'),
        birth_date=parsed.get('birth_date'),
        phone=parsed.get('phone'),
        address=parsed.get('address'),
    )

    await state.update_data(parsed=parsed)

    if duplicates:
        dup_text = "\n\n".join([format_relative_record(d) for d in duplicates])
        text = (
            f"⚠️ *Найдены похожие родственники ({len(duplicates)} шт.):*\n\n"
            f"{dup_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{format_relative(parsed)}\n\n"
            f"Всё равно сохранить как нового?"
        )
    else:
        text = f"{format_relative(parsed)}\n\nСохранить?"

    await state.set_state(RelativeStates.waiting_confirmation)
    await message.answer(text, parse_mode="Markdown", reply_markup=confirm_relative_kb())


# ──────────── ПОДТВЕРЖДЕНИЕ ────────────

@router.callback_query(F.data == "rel:save", RelativeStates.waiting_confirmation)
async def save_relative(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed = data['parsed']
    manager_id = data['manager_id']
    military_id = data['military_id']

    # Сохраняем родственника
    relative = await insert_relative(parsed, manager_id)

    # Создаём связку с военным
    await link_military_relative(military_id, relative['id'], manager_id)

    # Помечаем что родственники у этого военного собраны
    await mark_relatives_collected(military_id, value=True)

    await state.clear()

    await callback.message.edit_text(
        f"✅ *Родственник сохранён и привязан к военному.*\n\n{format_relative(parsed)}",
        parse_mode="Markdown"
    )

    # Спрашиваем добавить ещё
    await callback.message.answer(
        "Добавить ещё одного родственника к этому же военному?",
        reply_markup=add_more_relatives_kb(military_id)
    )


@router.callback_query(F.data == "rel:cancel", RelativeStates.waiting_confirmation)
async def cancel_relative(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись родственника отменена.")


# ──────────── ЦИКЛ "Добавить ещё" ────────────

@router.callback_query(F.data.startswith("rel:more:"))
async def relative_more(callback: CallbackQuery, state: FSMContext, manager: dict):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Военный не найден.")
        return

    await state.set_state(RelativeStates.waiting_template)
    await state.update_data(military_id=military_id, manager_id=manager['id'])

    await callback.message.edit_text(
        f"👤 Военный: *{military['full_name']}*\n\n"
        f"📝 Отправьте шаблон следующего родственника:",
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "rel:done")
async def relative_done(callback: CallbackQuery):
    await callback.message.edit_text("✅ Готово. Родственники сохранены.")


# ──────────── ОБЩАЯ ОТМЕНА ────────────

@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.")
