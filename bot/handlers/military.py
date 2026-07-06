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
    find_military_global_dup, insert_military_v2,
    military_has_relatives, take_over_military, military_was_taken_over,
    update_military_extra_field,
    # sources
    list_sources_by_manager, count_sources_by_manager,
    find_source_by_normalized_name, create_source,
    get_source_by_id, attach_source_to_military,
)
from bot.keyboards.menus import (
    confirm_military_with_dups_kb,
    take_over_military_kb,
    source_pick_kb,
)

router = Router()


# ──────────── FSM ────────────

class MilitaryStates(StatesGroup):
    waiting_template = State()
    waiting_dup_decision = State()
    waiting_extra_info = State()         # доп. инфа (БЧ/позывной/статус — свободный текст)
    waiting_source_choice = State()      # выбор источника кнопками
    waiting_source_text = State()        # ввод текста для нового источника (после "Свой вариант")


# ──────────── /cancel ────────────

@router.message(Command("cancel"), MilitaryStates.waiting_template)
@router.message(Command("cancel"), MilitaryStates.waiting_extra_info)
@router.message(Command("cancel"), MilitaryStates.waiting_source_choice)
@router.message(Command("cancel"), MilitaryStates.waiting_source_text)
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

        # pvl может ЗАБРАТЬ пустой лид (без родственников) ЛЮБОГО офиса — перехват.
        if my_office == 'pvl' and not await military_has_relatives(dup['id']):
            # Забрать можно только ОДИН раз за всё время — иначе пустые лиды
            # крутились бы по кругу между менеджерами.
            if await military_was_taken_over(dup['id']):
                await state.clear()
                await status_msg.edit_text(
                    "⛔ Этот погибший уже забирался ранее, родственники не найдены.\n"
                    "Повторный забор недоступен."
                )
                return
            await state.update_data(
                parsed=parsed,
                manager_id=manager['id'],
                manager_office=my_office,
                takeover_military_id=dup['id'],
            )
            await state.set_state(MilitaryStates.waiting_dup_decision)
            dup_text = format_military_record(dup)
            await status_msg.edit_text(
                f"⚠️ *Этот погибший уже в базе, но без родственников* "
                f"(офис: {dup_office or '—'}).\n\n{dup_text}\n\n"
                f"Можно забрать его себе и заполнить.",
                parse_mode="Markdown",
                reply_markup=take_over_military_kb(dup['id']),
            )
            return

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


@router.callback_query(F.data.startswith("mil:takeover:"))
async def take_over_military_lead(callback: CallbackQuery, state: FSMContext, manager: dict):
    """📥 Перехват пустого лида (только pvl): забрать себе и сразу к пробиву.
    Не зависит от FSM-state — id берём из callback_data, менеджера из middleware."""
    if not manager or manager.get("office") != "pvl":
        await callback.answer("Действие доступно только офису pvl.", show_alert=True)
        return
    await callback.answer()

    try:
        mil_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return
    manager_id = manager["id"]

    # Транзакционный забор с перепроверкой пустоты (защита от гонки).
    try:
        taken = await take_over_military(mil_id, manager_id, "pvl")
    except Exception:
        logging.getLogger("takeover").exception("take_over_military FAILED mil_id=%s", mil_id)
        await callback.message.answer("Ошибка при заборе — см. логи.")
        return
    if not taken:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "⛔ Не получилось забрать: за это время лид заполнили или забрали. Начните заново."
        )
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Впрыгиваем в обычный поток пробива по забранному лиду,
    # минуя доп-инфу и источник (Q5). saved_office='pvl' — счёт Sauron забравшего.
    await state.update_data(
        saved_full_name=taken["full_name"],
        saved_birth_date=taken.get("birth_date"),
        saved_military_id=taken["id"],
        saved_manager_id=manager_id,
        saved_office="pvl",
    )
    await callback.message.answer(
        f"📥 Погибший {taken['full_name']} закреплён за вами. Запускаю пробив..."
    )
    await _continue_to_probit(callback.message, state)


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
    """
    Переводит флоу в выбор источника. Если у менеджера есть свои
    источники — показываем кнопки. Если нет — сразу просим ввести
    свой вариант (или /skip).
    """
    manager_id = state_data.get("saved_manager_id") if (
        state_data := await state.get_data()
    ) else None

    if not manager_id:
        # Нет manager_id (это супер-админ или странный кейс) — без источника
        await _continue_to_probit(target, state)
        return

    total = await count_sources_by_manager(manager_id)
    if total == 0:
        # У менеджера нет ни одного источника — сразу текстовый ввод
        await state.set_state(MilitaryStates.waiting_source_text)
        await target.answer(
            "📌 *Откуда взяли данные?*\n\n"
            "У вас пока нет сохранённых источников.\n"
            "Введите название канала, ссылку или любой текст — он сохранится в ваш список.\n"
            "Без источника — отправьте /skip",
            parse_mode="Markdown",
        )
        return

    # Есть источники — показываем кнопочный выбор
    PAGE_SIZE = 5
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    sources = await list_sources_by_manager(manager_id, page=1, page_size=PAGE_SIZE)

    await state.set_state(MilitaryStates.waiting_source_choice)
    await state.update_data(source_page_size=PAGE_SIZE)

    text = (
        "📌 *Откуда взяли данные?*\n\n"
        f"Выберите один из ваших источников или добавьте новый "
        f"(всего: {total})."
    )
    await target.answer(
        text,
        parse_mode="Markdown",
        reply_markup=source_pick_kb(sources, page=1, total_pages=total_pages),
    )


# ──── waiting_source_choice — кнопочный выбор ────

@router.callback_query(
    F.data.startswith("src:page:"),
    MilitaryStates.waiting_source_choice,
)
async def source_choice_page(callback: CallbackQuery, state: FSMContext):
    """Переключение страницы списка источников."""
    await callback.answer()
    try:
        action = callback.data.split(":")[2]
    except IndexError:
        return
    if action == "noop":
        return

    try:
        page = int(action)
    except ValueError:
        return

    data = await state.get_data()
    manager_id = data.get("saved_manager_id")
    page_size = data.get("source_page_size", 5)
    if not manager_id:
        return

    total = await count_sources_by_manager(manager_id)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    sources = await list_sources_by_manager(manager_id, page=page, page_size=page_size)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=source_pick_kb(sources, page=page, total_pages=total_pages)
        )
    except Exception:
        pass


@router.callback_query(
    F.data.startswith("src:pick:"),
    MilitaryStates.waiting_source_choice,
)
async def source_choice_pick(callback: CallbackQuery, state: FSMContext):
    """Менеджер выбрал источник из своего списка."""
    await callback.answer()
    try:
        source_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    data = await state.get_data()
    manager_id = data.get("saved_manager_id")
    military_id = data.get("saved_military_id")
    if not (manager_id and military_id):
        return

    src = await get_source_by_id(source_id)
    if not src or not src["is_active"] or src["owner_manager_id"] != manager_id:
        # Дополнительная защита — источник чужой или удалён
        await callback.message.answer("⚠️ Источник недоступен. Выберите другой или /cancel.")
        return

    await attach_source_to_military(military_id, source_id)
    await callback.message.edit_text(
        f"📌 Источник: {src['name']}",
    )
    await _continue_to_probit(callback.message, state)


@router.callback_query(
    F.data == "src:custom",
    MilitaryStates.waiting_source_choice,
)
async def source_choice_custom(callback: CallbackQuery, state: FSMContext):
    """Менеджер хочет ввести свой вариант — переходим в текстовый ввод."""
    await callback.answer()
    await state.set_state(MilitaryStates.waiting_source_text)
    await callback.message.edit_text(
        "📌 *Свой источник*\n\n"
        "Введите название канала, ссылку или любой текст.\n"
        "Источник сохранится в ваш список.\n"
        "Отмена — /cancel или /skip без источника",
        parse_mode="Markdown",
    )


@router.callback_query(
    F.data == "src:none",
    MilitaryStates.waiting_source_choice,
)
async def source_choice_none(callback: CallbackQuery, state: FSMContext):
    """Без источника — сразу к пробиву."""
    await callback.answer()
    try:
        await callback.message.edit_text("📌 Без источника.")
    except Exception:
        pass
    await _continue_to_probit(callback.message, state)


# ──── waiting_source_text — ручной ввод ────

@router.message(Command("skip"), MilitaryStates.waiting_source_text)
async def skip_source_text(message: Message, state: FSMContext):
    """Менеджер пропустил ввод источника — сразу к пробиву"""
    await _continue_to_probit(message, state)


@router.message(MilitaryStates.waiting_source_text)
async def receive_source_text(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return

    source_text = (message.text or "").strip()
    if not source_text:
        await message.answer("⚠️ Пустой текст. Введите источник или /skip")
        return

    data = await state.get_data()
    manager_id = data.get("saved_manager_id")
    military_id = data.get("saved_military_id")
    manager_office = data.get("saved_office")
    if not (manager_id and military_id):
        await message.answer("⚠️ Состояние утеряно. /cancel и заново.")
        return

    # Проверяем что нормализованное имя ещё не занято кем-то ещё
    existing = await find_source_by_normalized_name(source_text)
    if existing:
        if existing["owner_manager_id"] == manager_id:
            # Это твой собственный источник — просто прикрепляем без нового INSERT
            await attach_source_to_military(military_id, existing["id"])
            await message.answer(f"📌 Источник: {existing['name']}")
            await _continue_to_probit(message, state)
            return
        # Чужой источник — жёсткий отказ
        owner_office = existing.get("office") or "—"
        await message.answer(
            f"❌ Источник <b>{source_text}</b> уже занят менеджером "
            f"из офиса <b>{owner_office}</b>.\n\n"
            f"Введите другой источник или /skip без источника.",
            parse_mode="HTML",
        )
        return

    # Создаём новый источник за менеджером
    new_id = await create_source(source_text, manager_id, manager_office)
    if not new_id:
        # Очень маловероятно — race condition или ошибка БД
        await message.answer(
            "⚠️ Не удалось сохранить источник. Попробуйте ещё раз или /skip."
        )
        return

    await attach_source_to_military(military_id, new_id)
    # Источник может содержать спецсимволы — без markdown
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


