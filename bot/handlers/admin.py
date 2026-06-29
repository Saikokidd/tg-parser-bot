from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.utils.menu_guard import is_menu_button_pressed
from bot.db.queries import (
    list_managers, create_manager, get_manager_by_id,
    add_telegram_id_to_manager, deactivate_manager,
    update_manager_office, move_manager_with_data_to_office,
    disable_manager, enable_manager,
)
from bot.keyboards.menus import (
    admin_menu, managers_menu, back_to_managers, cancel_kb,
    managers_list_kb, confirm_delete_kb, office_choice_kb,
    manager_pick_kb,
)

router = Router()

# ============== FSM СОСТОЯНИЯ ==============

class AddManagerStates(StatesGroup):
    waiting_name = State()
    waiting_office = State()          # выбор офиса (pvl/dp/ha) inline-кнопками
    waiting_telegram_id = State()


class EditManagerIdStates(StatesGroup):
    waiting_telegram_id = State()


# ============== B3: HELPERS ДЛЯ ОФИСНОГО ДОСТУПА ==============

ALLOWED_OFFICES = ('pvl', 'dp', 'ha')


def _is_admin_role(role: str | None) -> bool:
    """Имеет ли пользователь право на админ-меню (любой админ)."""
    return role in ('super_admin', 'office_admin')


def _office_filter_for(role: str | None, office: str | None) -> str | None:
    """
    Какой office_filter применять для текущего пользователя:
    - super_admin → None (видит все офисы)
    - office_admin → свой офис
    """
    if role == 'super_admin':
        return None
    return office  # office_admin видит только свой


def _can_manage_target(target_manager: dict | None, role: str | None,
                       office: str | None) -> bool:
    """
    Может ли текущий пользователь видеть/менять конкретного целевого менеджера.

    - super_admin → может всех
    - office_admin → только менеджеров своего офиса
    Защищает от подделанных callback'ов: даже если пришёл mgr_select:delete:N
    с N из чужого офиса, мы не позволим действие.
    """
    if not target_manager:
        return False
    if role == 'super_admin':
        return True
    if role == 'office_admin':
        return target_manager.get('office') == office
    return False


# ============== ВХОД В МЕНЮ УПРАВЛЕНИЯ ==============

@router.message(F.text == "⚙️ Управление ботом")
async def open_admin_menu(message: Message, role: str = None, office: str = None,
                           is_admin: bool = False):
    if not _is_admin_role(role):
        return
    await message.answer("⚙️ *Управление ботом*", parse_mode="Markdown", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:managers")
async def open_managers_menu(callback: CallbackQuery, state: FSMContext,
                              role: str = None, office: str = None,
                              is_admin: bool = False):
    if not _is_admin_role(role):
        return
    await state.clear()
    await callback.message.edit_text(
        "👥 *Управление менеджерами*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=managers_menu(is_super_admin=(role == 'super_admin'))
    )


@router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery, role: str = None, office: str = None,
                     is_admin: bool = False):
    if not _is_admin_role(role):
        return
    await callback.message.edit_text("⚙️ *Управление ботом*", parse_mode="Markdown", reply_markup=admin_menu())


@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Действие отменено.",
        reply_markup=back_to_managers()
    )


# ============== СПИСОК МЕНЕДЖЕРОВ ==============

MGR_LIST_PAGE_SIZE = 10


def _render_managers_list_text(
    managers: list[dict],
    role: str,
    office: str | None,
    page: int,
    total: int,
) -> str:
    """Рендер одной страницы списка менеджеров в Markdown."""
    header = f"📋 *Список менеджеров* (всего: {total})\n"
    lines = [header]
    current_office = None
    for m in managers:
        m_office = m.get('office') or '—'
        if role == 'super_admin' and m_office != current_office:
            current_office = m_office
            lines.append(f"\n*━━━ Офис: {m_office} ━━━*")

        if not m.get('is_active'):
            status = "🔴"
        elif m.get('is_disabled'):
            status = "🚫"
        else:
            status = "🟢"
        role_mark = " 👑" if m.get('role') == 'admin' else ""
        tg_ids = ", ".join(str(tid) for tid in m['telegram_ids']) if m['telegram_ids'] else "—"
        lines.append(f"{status} *{m['name']}*{role_mark}\n   ID: `{tg_ids}`")

    return "\n\n".join(lines)


def _managers_list_nav_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Навигация для страничного списка менеджеров."""
    rows = []
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"mgr:list_page:{page - 1}",
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data="mgr:list_page:noop",
        ))
        if page < total_pages:
            nav.append(InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"mgr:list_page:{page + 1}",
            ))
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text="« К меню менеджеров",
        callback_data="admin:managers",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_managers_list_page(
    callback: CallbackQuery,
    role: str,
    office: str | None,
    page: int = 1,
):
    """Показать страницу списка менеджеров с пагинацией."""
    office_filter = _office_filter_for(role, office)
    all_managers = await list_managers(
        only_active=True,
        office_filter=office_filter,
    )
    total = len(all_managers)

    if total == 0:
        scope = "по всем офисам" if role == 'super_admin' else f"в офисе {office}"
        try:
            await callback.message.edit_text(
                f"📭 Менеджеров {scope} пока нет.",
                reply_markup=back_to_managers(),
            )
        except Exception:
            await callback.message.answer(
                f"📭 Менеджеров {scope} пока нет.",
                reply_markup=back_to_managers(),
            )
        return

    total_pages = max(1, (total + MGR_LIST_PAGE_SIZE - 1) // MGR_LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * MGR_LIST_PAGE_SIZE
    end = start + MGR_LIST_PAGE_SIZE
    window = all_managers[start:end]

    text = _render_managers_list_text(window, role, office, page, total)
    kb = _managers_list_nav_kb(page, total_pages)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "mgr:list")
async def show_managers_list(callback: CallbackQuery, role: str = None,
                              office: str = None, is_admin: bool = False):
    if not _is_admin_role(role):
        return
    await callback.answer()
    await _render_managers_list_page(callback, role, office, page=1)


@router.callback_query(F.data.startswith("mgr:list_page:"))
async def show_managers_list_page(callback: CallbackQuery, role: str = None,
                                    office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    action = callback.data.split(":")[2]
    if action == "noop":
        return
    try:
        page = int(action)
    except ValueError:
        return
    await _render_managers_list_page(callback, role, office, page=page)


# ============== ДОБАВЛЕНИЕ МЕНЕДЖЕРА ==============

@router.callback_query(F.data == "mgr:add")
async def add_manager_start(callback: CallbackQuery, state: FSMContext,
                             role: str = None, office: str = None,
                             is_admin: bool = False):
    if not _is_admin_role(role):
        return
    # Запоминаем роль и офис админа, который добавляет — пригодится на шаге офиса
    await state.update_data(_actor_role=role, _actor_office=office)
    await state.set_state(AddManagerStates.waiting_name)

    hint = ""
    if role == 'super_admin':
        hint = "\n_(потом выберете офис)_"
    else:
        hint = f"\n_(будет добавлен в ваш офис: {office})_"

    await callback.message.edit_text(
        f"➕ *Добавление менеджера*{hint}\n\n"
        f"Введите *имя сотрудника* (например: Иван Петров):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@router.message(AddManagerStates.waiting_name)
async def add_manager_name(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("⚠️ Имя слишком короткое. Попробуйте ещё раз:")
        return
    await state.update_data(name=name)

    data = await state.get_data()
    actor_role = data.get('_actor_role')
    actor_office = data.get('_actor_office')

    if actor_role == 'super_admin':
        # super_admin выбирает офис кнопками
        await state.set_state(AddManagerStates.waiting_office)
        await message.answer(
            f"Имя: *{name}*\n\nВыберите офис сотрудника:",
            parse_mode="Markdown",
            reply_markup=office_choice_kb("add")
        )
    else:
        # office_admin: офис = его собственный, шаг выбора пропускаем
        await state.update_data(office=actor_office)
        await state.set_state(AddManagerStates.waiting_telegram_id)
        await message.answer(
            f"Имя: *{name}*\n\n"
            f"Теперь введите *Telegram ID* сотрудника (число):",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )


@router.callback_query(AddManagerStates.waiting_office, F.data.startswith("office:add:"))
async def add_manager_office(callback: CallbackQuery, state: FSMContext,
                              role: str = None, office: str = None,
                              is_admin: bool = False):
    # Этот шаг только для super_admin (office_admin сюда не попадает)
    if role != 'super_admin':
        await callback.answer("Недоступно", show_alert=True)
        return
    chosen = callback.data.split(":")[2]  # 'pvl' / 'dp' / 'ha'
    if chosen not in ALLOWED_OFFICES:
        await callback.answer("Неизвестный офис", show_alert=True)
        return
    await state.update_data(office=chosen)
    data = await state.get_data()
    await state.set_state(AddManagerStates.waiting_telegram_id)
    await callback.message.edit_text(
        f"Имя: *{data['name']}*\nОфис: *{chosen}*\n\n"
        f"Теперь введите *Telegram ID* сотрудника (число):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )
    await callback.answer()


@router.message(AddManagerStates.waiting_telegram_id)
async def add_manager_telegram_id(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return
    tid_text = message.text.strip()
    if not tid_text.isdigit():
        await message.answer("⚠️ Telegram ID должен быть числом. Попробуйте ещё раз:")
        return
    telegram_id = int(tid_text)
    data = await state.get_data()
    name = data['name']
    office = data.get('office')  # сохранён на шаге waiting_office
    try:
        manager = await create_manager(name=name, telegram_id=telegram_id, office=office)
        await message.answer(
            f"✅ Менеджер добавлен!\n\n"
            f"👤 Имя: *{manager['name']}*\n"
            f"🏢 Офис: *{office or '—'}*\n"
            f"🆔 Telegram ID: `{telegram_id}`",
            parse_mode="Markdown",
            reply_markup=back_to_managers()
        )
    except Exception as e:
        err = str(e)
        if "managers_name_office_unique" in err or "managers_name_unique" in err:
            # Кто-то с таким именем уже есть в этом офисе.
            # Может быть удалён (is_active=false) — тогда админ должен
            # обратиться к super_admin для восстановления.
            await message.answer(
                f"⚠️ Менеджер с именем *{name}* уже существует в этом офисе.\n\n"
                f"Возможно, он был ранее удалён, но запись осталась в БД. "
                f"Обратитесь к super-админу — он может восстановить старую запись.",
                parse_mode="Markdown",
                reply_markup=back_to_managers()
            )
        elif "manager_telegram_ids_telegram_id_key" in err:
            await message.answer(
                f"⚠️ Telegram ID `{telegram_id}` уже привязан к другому менеджеру.",
                parse_mode="Markdown",
                reply_markup=back_to_managers()
            )
        else:
            # На случай неизвестных ошибок — логируем и показываем
            # сокращённое сообщение, не сырой traceback
            import logging
            logging.getLogger(__name__).exception(
                f"create_manager error: name={name}, tg_id={telegram_id}, office={office}"
            )
            await message.answer(
                f"❌ Не удалось добавить менеджера. Обратитесь к разработчику.",
                reply_markup=back_to_managers()
            )
    await state.clear()


# ============== ИЗМЕНЕНИЕ / ДОБАВЛЕНИЕ ID ==============

@router.callback_query(F.data == "mgr:edit_id")
async def edit_id_start(callback: CallbackQuery, role: str = None,
                         office: str = None, is_admin: bool = False):
    if not _is_admin_role(role):
        return
    office_filter = _office_filter_for(role, office)
    managers = await list_managers(only_active=True, office_filter=office_filter)
    if not managers:
        await callback.message.edit_text(
            "📭 Активных менеджеров нет.",
            reply_markup=back_to_managers()
        )
        return

    await callback.message.edit_text(
        "🔄 *Привязка дополнительного Telegram ID*\n\nВыберите менеджера:",
        parse_mode="Markdown",
        reply_markup=managers_list_kb(managers, action="edit_id")
    )


@router.callback_query(F.data.startswith("mgr_select:edit_id:"))
async def edit_id_select(callback: CallbackQuery, state: FSMContext,
                          role: str = None, office: str = None,
                          is_admin: bool = False):
    if not _is_admin_role(role):
        return
    manager_id = int(callback.data.split(":")[2])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return

    # B3: защита — нельзя трогать менеджера чужого офиса
    if not _can_manage_target(manager, role, office):
        await callback.answer("Этот менеджер не из вашего офиса.", show_alert=True)
        return

    await state.set_state(EditManagerIdStates.waiting_telegram_id)
    await state.update_data(manager_id=manager_id, manager_name=manager['name'])
    await callback.message.edit_text(
        f"Менеджер: *{manager['name']}*\n\nВведите новый *Telegram ID* для привязки:",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@router.message(EditManagerIdStates.waiting_telegram_id)
async def edit_id_save(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return
    tid_text = message.text.strip()
    if not tid_text.isdigit():
        await message.answer("⚠️ Telegram ID должен быть числом. Попробуйте ещё раз:")
        return

    telegram_id = int(tid_text)
    data = await state.get_data()
    manager_id = data['manager_id']
    manager_name = data['manager_name']

    success = await add_telegram_id_to_manager(manager_id, telegram_id)
    if success:
        await message.answer(
            f"✅ ID `{telegram_id}` привязан к менеджеру *{manager_name}*.",
            parse_mode="Markdown",
            reply_markup=back_to_managers()
        )
    else:
        await message.answer(
            f"⚠️ Этот Telegram ID уже привязан к другому менеджеру.",
            reply_markup=back_to_managers()
        )

    await state.clear()


# ============== УДАЛЕНИЕ МЕНЕДЖЕРА ==============

@router.callback_query(F.data == "mgr:delete")
async def delete_manager_start(callback: CallbackQuery, role: str = None,
                                office: str = None, is_admin: bool = False):
    if not _is_admin_role(role):
        return
    office_filter = _office_filter_for(role, office)
    managers = await list_managers(only_active=True, office_filter=office_filter)
    if not managers:
        await callback.message.edit_text(
            "📭 Активных менеджеров нет.",
            reply_markup=back_to_managers()
        )
        return

    await callback.message.edit_text(
        "❌ *Удаление менеджера*\n\nВыберите менеджера для деактивации:",
        parse_mode="Markdown",
        reply_markup=managers_list_kb(managers, action="delete")
    )


@router.callback_query(F.data.startswith("mgr_select:delete:"))
async def delete_manager_confirm(callback: CallbackQuery, role: str = None,
                                  office: str = None, is_admin: bool = False):
    if not _is_admin_role(role):
        return
    manager_id = int(callback.data.split(":")[2])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return

    # B3: защита — нельзя удалить менеджера чужого офиса
    if not _can_manage_target(manager, role, office):
        await callback.answer("Этот менеджер не из вашего офиса.", show_alert=True)
        return

    warn = ""
    if role == 'super_admin':
        warn = f"\n🏢 Офис: *{manager.get('office') or '—'}*"
    await callback.message.edit_text(
        f"⚠️ Удалить менеджера *{manager['name']}*?{warn}\n\n"
        f"_Запись будет деактивирована, его данные сохранятся в БД._",
        parse_mode="Markdown",
        reply_markup=confirm_delete_kb(manager_id)
    )


@router.callback_query(F.data.startswith("mgr_del_confirm:"))
async def delete_manager_do(callback: CallbackQuery, role: str = None,
                             office: str = None, is_admin: bool = False):
    if not _is_admin_role(role):
        return
    manager_id = int(callback.data.split(":")[1])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return

    # B3: защита — нельзя удалить менеджера чужого офиса
    if not _can_manage_target(manager, role, office):
        await callback.answer("Этот менеджер не из вашего офиса.", show_alert=True)
        return

    await deactivate_manager(manager_id)
    await callback.message.edit_text(
        f"✅ Менеджер *{manager['name']}* деактивирован.",
        parse_mode="Markdown",
        reply_markup=back_to_managers()
    )
    
    
# ============== СМЕНА ОФИСА МЕНЕДЖЕРА ==============

@router.callback_query(F.data == "mgr:change_office")
async def change_office_start(callback: CallbackQuery, role: str = None,
                               office: str = None, is_admin: bool = False):
    """Шаг 1: показать список менеджеров для выбора. Только super_admin."""
    if role != 'super_admin':
        await callback.answer("Только для super-admin.", show_alert=True)
        return
    managers = await list_managers(only_active=True)  # все офисы
    if not managers:
        await callback.message.edit_text(
            "📭 Активных менеджеров нет.",
            reply_markup=back_to_managers()
        )
        return
    await callback.message.edit_text(
        "🏢 *Смена офиса менеджера*\n\n"
        "Выберите менеджера. В скобках текущий офис.\n"
        "_Переносятся и все его лиды/родственники._",
        parse_mode="Markdown",
        reply_markup=managers_list_kb(managers, action="change_office")
    )


@router.callback_query(F.data.startswith("mgr_select:change_office:"))
async def change_office_pick_office(callback: CallbackQuery, role: str = None,
                                     office: str = None, is_admin: bool = False):
    """Шаг 2: выбран менеджер — показываем выбор офиса. Только super_admin."""
    if role != 'super_admin':
        await callback.answer("Только для super-admin.", show_alert=True)
        return
    manager_id = int(callback.data.split(":")[2])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return
    current_office = manager.get("office") or "—"
    await callback.message.edit_text(
        f"👤 Менеджер: *{manager['name']}*\n"
        f"🏢 Текущий офис: *{current_office}*\n\n"
        f"Выберите новый офис:",
        parse_mode="Markdown",
        reply_markup=office_choice_kb("change", manager_id=manager_id)
    )


@router.callback_query(F.data.startswith("office:change:"))
async def change_office_do(callback: CallbackQuery, role: str = None,
                            office: str = None, is_admin: bool = False):
    """
    Шаг 3: выбран новый офис — применяем перенос (вместе с данными).
    callback_data формат: 'office:change:{manager_id}:{office}'
    Только super_admin.
    """
    if role != 'super_admin':
        await callback.answer("Только для super-admin.", show_alert=True)
        return
    parts = callback.data.split(":")
    # ['office', 'change', '{id}', '{office}']
    if len(parts) != 4:
        await callback.answer("Неверный формат данных", show_alert=True)
        return
    manager_id = int(parts[2])
    new_office = parts[3]
    if new_office not in ALLOWED_OFFICES:
        await callback.answer("Неизвестный офис", show_alert=True)
        return
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return
    old_office = manager.get("office") or "—"

    if old_office == new_office:
        await callback.answer("Менеджер уже в этом офисе.", show_alert=True)
        return

    # Полный перенос: менеджер + его военные + его родственники (в транзакции)
    result = await move_manager_with_data_to_office(manager_id, new_office)

    await callback.message.edit_text(
        f"✅ Офис обновлён (с переносом данных).\n\n"
        f"👤 Менеджер: *{manager['name']}*\n"
        f"🏢 Было: *{old_office}* → Стало: *{new_office}*\n\n"
        f"📋 Перенесено военных: *{result['military_moved']}*\n"
        f"👥 Перенесено родственников: *{result['relatives_moved']}*",
        parse_mode="Markdown",
        reply_markup=back_to_managers()
    )
    await callback.answer()


# ============== ОТКЛЮЧИТЬ / ВКЛЮЧИТЬ МЕНЕДЖЕРА ==============

DISABLE_PAGE_SIZE = 10


async def _render_disable_list(
    callback: CallbackQuery,
    role: str,
    office: str | None,
    page: int = 1,
    edit: bool = True,
):
    """
    Показать список активных НЕотключённых менеджеров для отключения.
    """
    office_filter = _office_filter_for(role, office)
    all_active = await list_managers(
        only_active=True,
        office_filter=office_filter,
        is_disabled_filter=False,
    )
    total = len(all_active)
    if total == 0:
        scope = "по всем офисам" if role == "super_admin" else f"в офисе {office}"
        text = f"🚫 *Отключить менеджера*\n\nАктивных менеджеров {scope} нет."
        try:
            await callback.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=back_to_managers(),
            )
        except Exception:
            await callback.message.answer(
                text, parse_mode="Markdown",
                reply_markup=back_to_managers(),
            )
        return
    
    total_pages = max(1, (total + DISABLE_PAGE_SIZE - 1) // DISABLE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * DISABLE_PAGE_SIZE
    end = start + DISABLE_PAGE_SIZE
    window = all_active[start:end]
    
    text = (
        f"🚫 *Отключить менеджера*\n\n"
        f"Выберите кого отключить (всего активных: {total}).\n"
        f"Менеджер сразу теряет доступ к боту — можно включить обратно."
    )
    kb = manager_pick_kb(window, page=page, total_pages=total_pages, action="disable")
    if edit:
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)


async def _render_enable_list(
    callback: CallbackQuery,
    role: str,
    office: str | None,
    page: int = 1,
    edit: bool = True,
):
    """
    Показать список активных ОТКЛЮЧЁННЫХ менеджеров для включения.
    """
    office_filter = _office_filter_for(role, office)
    all_disabled = await list_managers(
        only_active=True,
        office_filter=office_filter,
        is_disabled_filter=True,
    )
    total = len(all_disabled)
    if total == 0:
        scope = "по всем офисам" if role == "super_admin" else f"в офисе {office}"
        text = f"✅ *Включить менеджера*\n\nОтключённых менеджеров {scope} нет."
        try:
            await callback.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=back_to_managers(),
            )
        except Exception:
            await callback.message.answer(
                text, parse_mode="Markdown",
                reply_markup=back_to_managers(),
            )
        return
    
    total_pages = max(1, (total + DISABLE_PAGE_SIZE - 1) // DISABLE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * DISABLE_PAGE_SIZE
    end = start + DISABLE_PAGE_SIZE
    window = all_disabled[start:end]
    
    text = (
        f"✅ *Включить менеджера*\n\n"
        f"Выберите кого включить обратно (всего отключённых: {total})."
    )
    kb = manager_pick_kb(window, page=page, total_pages=total_pages, action="enable")
    if edit:
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "mgr:disable_list")
async def disable_list(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    await _render_disable_list(callback, role, office, page=1)


@router.callback_query(F.data.startswith("mgr:disable_page:"))
async def disable_page(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    action = callback.data.split(":")[2]
    if action == "noop":
        return
    try:
        page = int(action)
    except ValueError:
        return
    await _render_disable_list(callback, role, office, page=page)


@router.callback_query(F.data.startswith("mgr:disable_pick:"))
async def disable_pick(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    try:
        manager_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return
    
    # Проверяем доступ — office_admin может отключать только своего офиса
    m = await get_manager_by_id(manager_id)
    if not m or not m.get("is_active"):
        await callback.message.answer("⚠️ Менеджер недоступен.")
        return
    if role == "office_admin" and m.get("office") != office:
        await callback.message.answer("⚠️ Менеджер из другого офиса.")
        return
    
    ok = await disable_manager(manager_id)
    if not ok:
        await callback.message.answer("⚠️ Не удалось отключить менеджера.")
        return
    
    # Перерисовываем список (отключённый исчез)
    await _render_disable_list(callback, role, office, page=1)


@router.callback_query(F.data == "mgr:enable_list")
async def enable_list(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    await _render_enable_list(callback, role, office, page=1)


@router.callback_query(F.data.startswith("mgr:enable_page:"))
async def enable_page(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    action = callback.data.split(":")[2]
    if action == "noop":
        return
    try:
        page = int(action)
    except ValueError:
        return
    await _render_enable_list(callback, role, office, page=page)


@router.callback_query(F.data.startswith("mgr:enable_pick:"))
async def enable_pick(callback: CallbackQuery, role: str = None, office: str = None):
    if not _is_admin_role(role):
        return
    await callback.answer()
    try:
        manager_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return
    
    m = await get_manager_by_id(manager_id)
    if not m or not m.get("is_active"):
        await callback.message.answer("⚠️ Менеджер недоступен.")
        return
    if role == "office_admin" and m.get("office") != office:
        await callback.message.answer("⚠️ Менеджер из другого офиса.")
        return
    
    ok = await enable_manager(manager_id)
    if not ok:
        await callback.message.answer("⚠️ Не удалось включить менеджера.")
        return
    
    await _render_enable_list(callback, role, office, page=1)