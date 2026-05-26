"""
Хендлер для работы с военными.

Флоу:
- "🔍 Пробить" → шаблон с подсказкой про /cancel
- Менеджер отправляет данные шаблона
- Промежуточные сообщения о статусе обработки
- Если дубли → кнопки "Внести/Отменить"
- Если нет → автосохранение → автопробив через Sauron
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.utils.menu_guard import is_menu_button_pressed

from bot.parser.military_parser import (
    parse_military, validate_military, format_military_record
)
from bot.db.queries import (
    find_military_duplicates, insert_military,
    list_military_without_relatives, list_military_by_manager,
)
from bot.keyboards.menus import confirm_military_with_dups_kb

router = Router()


# ──────────── FSM ────────────

class MilitaryStates(StatesGroup):
    waiting_template = State()
    waiting_dup_decision = State()
    waiting_source = State()  # ввод источника после сохранения, перед пробивом


# ──────────── /cancel ────────────

@router.message(Command("cancel"), MilitaryStates.waiting_template)
@router.message(Command("cancel"), MilitaryStates.waiting_source)
@router.message(Command("cancel"), MilitaryStates.waiting_dup_decision)
async def cancel_military_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "🔍 Пробить")
async def btn_probivat(message: Message, state: FSMContext, manager: dict | None):
    if not manager:
        await message.answer(
            "Только менеджеры могут вносить записи."
        )
        return

    await state.set_state(MilitaryStates.waiting_template)
    await message.answer(
        "*❗️СЮДА ВНОСЯТСЯ ТОЛЬКО ВОЕННЫЕ❗️*\n\n"
        "*Одной строкой:*\n"
        "`Иванов Иван Иванович 15.03.1985 200`\n\n"
        "Можно с тире, запятой и без статуса.\n"
        "Статус: 200 / 500 / БЗ / любой текст.\n\n"
        "*Или с подписями:*\n"
        "ФИО: Иванов Иван Иванович\n"
        "ДР: 15.03.1985\n"
        "Статус: 200\n"
        "Б/Ч: 12345        (опционально)\n"
        "Позывной: Север   (опционально)\n\n"
        "_Отмена — /cancel_",
        parse_mode="Markdown"
    )


# ──────────── ПРИЁМ ШАБЛОНА ────────────

@router.message(MilitaryStates.waiting_template)
async def receive_template(message: Message, state: FSMContext, manager: dict):
    if await is_menu_button_pressed(message, state):
        return
    parsed = parse_military(message.text)

    err = validate_military(parsed)
    if err:
        await message.answer(
            f"⚠️ {err}\n\nПопробуйте ещё раз или отправьте /cancel."
        )
        return

    # Шаг 1: статус — проверка дублей
    status_msg = await message.answer("🔍 Проверка на дубликаты в базе...")

    duplicates = []
    if parsed.get('birth_date'):
        duplicates = await find_military_duplicates(
            full_name=parsed['full_name'],
            birth_date=parsed['birth_date']
        )

    if duplicates:
        # Дубль найден — спрашиваем подтверждения
        await state.update_data(parsed=parsed, manager_id=manager['id'])
        await state.set_state(MilitaryStates.waiting_dup_decision)

        dup_text = "\n\n".join([format_military_record(d) for d in duplicates])
        await status_msg.edit_text(
            f"⚠️ *Найдены дубли в базе ({len(duplicates)} шт.):*\n\n"
            f"{dup_text}\n\n"
            f"Всё равно внести?",
            parse_mode="Markdown",
            reply_markup=confirm_military_with_dups_kb()
        )
        return

    # Дублей нет — сохраняем автоматически
    await status_msg.edit_text("✅ Дубликатов не выявлено")
    await _save_and_probit(message, state, parsed, manager['id'])


# ──────────── ОБРАБОТКА РЕШЕНИЯ ПО ДУБЛЮ ────────────

@router.callback_query(F.data == "mil:save", MilitaryStates.waiting_dup_decision)
async def confirm_save_with_dup(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed = data['parsed']
    manager_id = data['manager_id']

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _save_and_probit(callback.message, state, parsed, manager_id)


@router.callback_query(F.data == "mil:cancel", MilitaryStates.waiting_dup_decision)
async def cancel_save_with_dup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись отменена.")


# ──────────── ОБЩАЯ ЛОГИКА: сохранение + пробив ────────────

async def _save_and_probit(target: Message, state: FSMContext, parsed: dict, manager_id: int):
    """
    Сохраняет военного в БД. После сохранения спрашивает источник.
    После ввода источника (или /skip) — запускает пробив.
    target — сообщение, в чат которого слать ответы.
    """
    record = await insert_military(parsed, manager_id)
    await target.answer(
        f"✅ *Сохранено в базу*\n\n"
        f"{record['full_name']}",
        parse_mode="Markdown"
    )

    # Сохраняем military_id и базовые данные в FSM, чтобы продолжить флоу
    # после ответа менеджера на запрос источника.
    # manager_id нужен для учёта расходов на пробив (probiv_log).
    await state.update_data(
        saved_military_id=record["id"],
        saved_full_name=parsed["full_name"],
        saved_birth_date=parsed.get("birth_date"),
        saved_manager_id=manager_id,
    )
    await state.set_state(MilitaryStates.waiting_source)

    await target.answer(
        "📌 *Откуда взяли данные?*\n\n"
        "Введите источник — ссылка на чат, название группы, или любой текст.\n"
        "Если не нужно — отправьте /skip"
    )


@router.message(Command("skip"), MilitaryStates.waiting_source)
async def skip_source(message: Message, state: FSMContext):
    """Менеджер пропустил ввод источника — сразу к пробиву"""
    await _continue_to_probit(message, state)


@router.message(MilitaryStates.waiting_source)
async def receive_source(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return
    """Менеджер ввёл источник — сохраняем в extra.source и идём в пробив"""
    source_text = message.text.strip()
    if not source_text:
        await message.answer("⚠️ Пустой текст. Введите источник или /skip")
        return

    data = await state.get_data()
    military_id = data["saved_military_id"]

    # Сохраняем источник в extra
    from bot.db.queries import update_military_extra_field
    await update_military_extra_field(military_id, "source", source_text)

    # Без markdown — source может содержать спецсимволы (_*[] и т.д.) из URL
    await message.answer(f"📌 Источник сохранён: {source_text}")
    await _continue_to_probit(message, state)


async def _continue_to_probit(target: Message, state: FSMContext):
    """Запустить автопробив после того как разобрались с источником"""
    data = await state.get_data()
    full_name = data["saved_full_name"]
    birth_date = data.get("saved_birth_date")

    # Сбрасываем active state, но data оставляем (probiv_persons)
    await state.set_state(None)

    from bot.handlers.probiv import run_probiv_after_save
    await run_probiv_after_save(
        target,
        state,
        full_name=full_name,
        birth_date=birth_date,
        military_id=data.get("saved_military_id"),
    )


