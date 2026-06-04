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
    parse_military, validate_military, format_military_record,
    parse_military_strict, MilitaryStrictError,
)
from bot.db.queries import (
    list_military_by_manager,
    find_military_global_dup, insert_military_v2,         # этап B1: глобальный дубль + office при создании
    update_military_extra_field,                          # для записи доп.инфы и источника в extra
)
from bot.keyboards.menus import confirm_military_with_dups_kb

router = Router()


# ──────────── FSM ────────────

class MilitaryStates(StatesGroup):
    waiting_template = State()
    waiting_dup_decision = State()
    waiting_extra_info = State()  # доп. инфа (БЧ/позывной/статус — свободный текст), идёт перед источником
    waiting_source = State()      # ввод источника, после доп.инфы, перед пробивом


# ──────────── /cancel ────────────

@router.message(Command("cancel"), MilitaryStates.waiting_template)
@router.message(Command("cancel"), MilitaryStates.waiting_extra_info)
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
        "<b>❗️СЮДА ВНОСЯТСЯ ТОЛЬКО ВОЕННЫЕ❗️</b>\n\n"
        "Отправьте <b>ФИО и дату рождения</b> в одну строку:\n"
        "<code>Иванов Иван Иванович 15.03.1985</code>\n\n"
        "Можно с запятой или с тире в начале:\n"
        "<code>- Иванов Иван Иванович, 15.03.1985</code>\n\n"
        "<i>На следующем шаге сможете добавить статус, БЧ, позывной и любую другую информацию.</i>\n\n"
        "<i>Отмена — /cancel</i>",
        parse_mode="HTML"
    )


# ──────────── ПРИЁМ ШАБЛОНА ────────────

@router.message(MilitaryStates.waiting_template)
async def receive_template(message: Message, state: FSMContext, manager: dict):
    if await is_menu_button_pressed(message, state):
        return

    # Новый строгий парсер: только ФИО + ДР, лишнее → отказ с подсказкой
    try:
        parsed = parse_military_strict(message.text)
    except MilitaryStrictError as e:
        await message.answer(
            f"⚠️ {e.args[0]}\n\nОтмена — /cancel",
            parse_mode="HTML",
        )
        return

    # Шаг 1: глобальный дубль-чек (этап B1 — мульти-офисность)
    status_msg = await message.answer("🔍 Проверка на дубликаты в базе...")

    dup = await find_military_global_dup(
        full_name=parsed['full_name'],
        birth_date=parsed.get('birth_date'),
    )

    if dup:
        dup_office = dup.get('office')
        my_office = manager.get('office')

        # Дубль из чужого офиса (и наш офис известен и не совпадает) — жёсткий отказ.
        # Если office_dup или my_office == None — считаем "своим" (исторические записи / не назначен).
        if dup_office and my_office and dup_office != my_office:
            await state.clear()
            mgr_name = dup.get('manager_name') or '—'
            await status_msg.edit_text(
                f"⛔ Этот человек уже в работе у офиса <b>{dup_office}</b> "
                f"(менеджер: <b>{mgr_name}</b>).\n\n"
                f"Внесение запрещено.",
                parse_mode="HTML",
            )
            return

        # Дубль из своего офиса — показываем как раньше, спрашиваем подтверждения
        await state.update_data(
            parsed=parsed,
            manager_id=manager['id'],
            manager_office=manager.get('office'),
        )
        await state.set_state(MilitaryStates.waiting_dup_decision)

        # Используем формат старой карточки — он уже умеет показывать
        dup_text = format_military_record(dup)
        await status_msg.edit_text(
            f"⚠️ *Найден дубль в вашем офисе:*\n\n"
            f"{dup_text}\n\n"
            f"Всё равно внести?",
            parse_mode="Markdown",
            reply_markup=confirm_military_with_dups_kb()
        )
        return

    # Дублей нет — сохраняем автоматически
    await status_msg.edit_text("✅ Дубликатов не выявлено")
    await _save_and_probit(message, state, parsed, manager['id'], manager.get('office'))


# ──────────── ОБРАБОТКА РЕШЕНИЯ ПО ДУБЛЮ ────────────

@router.callback_query(F.data == "mil:save", MilitaryStates.waiting_dup_decision)
async def confirm_save_with_dup(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed = data['parsed']
    manager_id = data['manager_id']
    manager_office = data.get('manager_office')

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _save_and_probit(callback.message, state, parsed, manager_id, manager_office)


@router.callback_query(F.data == "mil:cancel", MilitaryStates.waiting_dup_decision)
async def cancel_save_with_dup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись отменена.")


# ──────────── ОБЩАЯ ЛОГИКА: сохранение + пробив ────────────

async def _save_and_probit(target: Message, state: FSMContext, parsed: dict,
                            manager_id: int, manager_office: str | None):
    """
    Сохраняет военного в БД. После сохранения спрашивает источник.
    После ввода источника (или /skip) — запускает пробив.
    target — сообщение, в чат которого слать ответы.
    manager_office — 'pvl' / 'dp', нужен для выбора счёта Sauron.
    """
    # v2: автоматически проставляет office из manager.office
    record = await insert_military_v2(parsed, manager_id)
    await target.answer(
        f"✅ *Сохранено в базу*\n\n"
        f"{record['full_name']}",
        parse_mode="Markdown"
    )

    # Сохраняем military_id и базовые данные в FSM, чтобы продолжить флоу:
    # сначала спросим доп.инфу, потом источник, потом пробив.
    # manager_id и office нужны для пробива (учёт + выбор счёта Sauron).
    await state.update_data(
        saved_military_id=record["id"],
        saved_full_name=parsed["full_name"],
        saved_birth_date=parsed.get("birth_date"),
        saved_manager_id=manager_id,
        saved_office=manager_office,
    )
    await state.set_state(MilitaryStates.waiting_extra_info)

    await target.answer(
        "📝 *Доп. информация*\n\n"
        "Введите любую дополнительную информацию о военном:\n"
        "статус (200/500/БЗ), Б/Ч, позывной, заметки.\n\n"
        "Можно одной строкой или абзацем.\n"
        "Если нечего добавить — отправьте /skip",
        parse_mode="Markdown",
    )

# ──────────── ШАГ 2: ввод доп. информации (свободный текст) ────────────

@router.message(Command("skip"), MilitaryStates.waiting_extra_info)
async def skip_extra_info(message: Message, state: FSMContext):
    """Менеджер пропустил доп. инфу — переходим к вопросу про источник"""
    await _ask_for_source(message, state)


@router.message(MilitaryStates.waiting_extra_info)
async def receive_extra_info(message: Message, state: FSMContext):
    """
    Менеджер ввёл свободный текст с доп.инфой (статус/БЧ/позывной/заметки).
    Сохраняем в extra.note у военного и переходим к вопросу про источник.
    """
    if await is_menu_button_pressed(message, state):
        return

    extra_text = (message.text or "").strip()
    if not extra_text:
        await message.answer(
            "⚠️ Пустое сообщение. Введите доп. информацию или /skip"
        )
        return

    data = await state.get_data()
    military_id = data.get("saved_military_id")
    if not military_id:
        # Не должно произойти, но защищаемся
        await message.answer("⚠️ Что-то пошло не так. Начните заново через 🔍 Пробить.")
        await state.clear()
        return

    await update_military_extra_field(military_id, "note", extra_text)
    await message.answer("✅ Доп. информация сохранена.")
    await _ask_for_source(message, state)


async def _ask_for_source(target: Message, state: FSMContext):
    """Переводит флоу в state waiting_source и спрашивает откуда данные."""
    await state.set_state(MilitaryStates.waiting_source)
    await target.answer(
        "📌 *Откуда взяли данные?*\n\n"
        "Введите источник — ссылка на чат, название группы, или любой текст.\n"
        "Если не нужно — отправьте /skip",
        parse_mode="Markdown",
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


