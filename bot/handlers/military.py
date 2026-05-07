"""
Хендлер для работы с военными:
- Кнопка "🔍 Пробить военного" → запрос шаблона
- Парсинг шаблона + дубль-чек по ФИО+ДР
- Подтверждение сохранения
- После сохранения — предложить внести родственников
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.parser.military_parser import (
    parse_military, validate_military, format_military, format_military_record
)
from bot.db.queries import (
    find_military_duplicates, insert_military, mark_relatives_collected,
    list_military_without_relatives, list_military_by_manager,
    get_military_by_id
)
from bot.keyboards.menus import (
    confirm_military_kb, confirm_military_with_dups_kb,
    ask_relatives_kb, main_menu
)

router = Router()


# ──────────── FSM ────────────

class MilitaryStates(StatesGroup):
    waiting_template = State()
    waiting_confirmation = State()


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "🔍 Пробить ")
async def btn_probivat(message: Message, state: FSMContext, manager: dict | None):
    if not manager:
        await message.answer(
            "ℹ️ Только менеджеры могут вносить записи.\n"
            "Если вы администратор без своей таблицы — попросите добавить себя как менеджера."
        )
        return

    await state.set_state(MilitaryStates.waiting_template)
    await message.answer(
        "*Шаблон:*\n\n"
        "ФИО: \n"
        "ДР: \n"
        "Б/Ч: \n"
        "Позывной: \n"
        "Статус: \n"
        "Доп инфа: \n"
        "_Обязательные поля: ФИО и Статус (погиб / пропал)._",
        parse_mode="Markdown"
    )


# ──────────── ПРИЁМ ШАБЛОНА ────────────

@router.message(MilitaryStates.waiting_template)
async def receive_template(message: Message, state: FSMContext, manager: dict):
    parsed = parse_military(message.text)

    err = validate_military(parsed)
    if err:
        await message.answer(
            f"⚠️ {err}\n\nПопробуйте ещё раз или отправьте /cancel."
        )
        return

    # Дубль-чек по ФИО + ДР (только если есть ДР)
    duplicates = []
    if parsed.get('birth_date'):
        duplicates = await find_military_duplicates(
            full_name=parsed['full_name'],
            birth_date=parsed['birth_date']
        )

    await state.update_data(parsed=parsed, manager_id=manager['id'])

    if duplicates:
        dup_text = "\n\n".join([format_military_record(d) for d in duplicates])
        text = (
            f"*Найдены похожие записи ({len(duplicates)} шт.):*\n\n"
            f"{dup_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{format_military(parsed)}\n\n"
            f"Всё равно сохранить?"
        )
        kb = confirm_military_with_dups_kb()
    else:
        text = f"{format_military(parsed)}\n\nСохранить?"
        kb = confirm_military_kb()

    await state.set_state(MilitaryStates.waiting_confirmation)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ──────────── ПОДТВЕРЖДЕНИЕ ────────────

@router.callback_query(F.data == "mil:save", MilitaryStates.waiting_confirmation)
async def save_military(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed = data['parsed']
    manager_id = data['manager_id']

    record = await insert_military(parsed, manager_id)

    await callback.message.edit_text(
        f"✅ *Сохранено*\n\n{format_military(parsed)}",
        parse_mode="Markdown"
    )

    # Сбрасываем только состояние FSM (waiting_confirmation), но
    # данные оставляем — там probiv_persons понадобится для кнопок "Пробить далее"
    await state.set_state(None)

    # Запускаем автоматический пробив
    from bot.handlers.probiv import run_probiv_after_save
    await run_probiv_after_save(
        callback,
        state,
        full_name=parsed['full_name'],
        birth_date=parsed.get('birth_date')
    )

    # Предлагаем внести родственников вручную
    await callback.message.answer(
        "Заполнить ?",
        reply_markup=ask_relatives_kb(record['id'])
    )


@router.callback_query(F.data == "mil:cancel", MilitaryStates.waiting_confirmation)
async def cancel_military(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись отменена.")


# ──────────── РЕАКЦИЯ НА "Внести родственников?" ────────────

@router.callback_query(F.data.startswith("rel:later:"))
async def relatives_later(callback: CallbackQuery):
    """Менеджер сказал 'позже' — оставляем relatives_collected = FALSE"""
    military_id = int(callback.data.split(":")[2])
    await mark_relatives_collected(military_id, value=False)
    await callback.message.edit_text(
        "⏭ Хорошо.",
        parse_mode="Markdown"
    )


# ──────────── СПИСКИ ────────────

@router.message(F.text == "📊 Моя база")
async def btn_my_base(message: Message, manager: dict | None):
    if not manager:
        await message.answer(
            "ℹ️ У вас нет личной базы.\n"
            "Маякни айтишнику."
        )
        return

    records = await list_military_by_manager(manager['id'])
    if not records:
        await message.answer("📭 Ваша база пустая.")
        return

    # Считаем сколько с собранными родственниками
    with_relatives = sum(1 for r in records if r.get('relatives_collected'))

    await message.answer(
        f"📊 База менеджера *{manager['name']}*:\n\n"
        f"Всего: *{len(records)}*\n"
        f"Заполнены: *{with_relatives}*\n"
        f"Осталось: *{len(records) - with_relatives}*",
        parse_mode="Markdown"
    )


@router.message(F.text == "📋 Без родственников")
async def btn_without_relatives(message: Message, manager: dict | None):
    if not manager:
        await message.answer("ℹ️ Доступно только менеджерам.")
        return

    records = await list_military_without_relatives(manager_id=manager['id'])
    if not records:
        await message.answer("✅ Все заполненно")
        return

    lines = [f"📋 *Военные без родственников ({len(records)}):*\n"]
    for r in records[:30]:  # лимит чтобы не перегрузить сообщение
        birth = r.get('birth_date')
        birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
        lines.append(f"• {r['full_name']} ({birth_str})")

    if len(records) > 30:
        lines.append(f"\n_...и ещё {len(records) - 30}_")

    lines.append("\nнажмите *Заполнить")

    await message.answer("\n".join(lines), parse_mode="Markdown")
