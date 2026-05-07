from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.db.queries import (
    list_managers, create_manager, get_manager_by_id,
    add_telegram_id_to_manager, deactivate_manager
)
from bot.keyboards.menus import (
    admin_menu, managers_menu, back_to_managers, cancel_kb,
    managers_list_kb, confirm_delete_kb
)

router = Router()


# ============== FSM СОСТОЯНИЯ ==============

class AddManagerStates(StatesGroup):
    waiting_name = State()
    waiting_telegram_id = State()


class EditManagerIdStates(StatesGroup):
    waiting_telegram_id = State()


# ============== ВХОД В МЕНЮ УПРАВЛЕНИЯ ==============

@router.message(F.text == "⚙️ Управление ботом")
async def open_admin_menu(message: Message, is_admin: bool):
    if not is_admin:
        return
    await message.answer("⚙️ *Управление ботом*", parse_mode="Markdown", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:managers")
async def open_managers_menu(callback: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin:
        return
    await state.clear()
    await callback.message.edit_text(
        "👥 *Управление менеджерами*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=managers_menu()
    )


@router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    await callback.message.edit_text(
        "⚙️ *Управление ботом*",
        parse_mode="Markdown",
        reply_markup=admin_menu()
    )


@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Действие отменено.",
        reply_markup=back_to_managers()
    )


# ============== СПИСОК МЕНЕДЖЕРОВ ==============

@router.callback_query(F.data == "mgr:list")
async def show_managers_list(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    managers = await list_managers(only_active=False)
    if not managers:
        await callback.message.edit_text(
            "📭 Менеджеров пока нет.",
            reply_markup=back_to_managers()
        )
        return

    lines = ["📋 *Список менеджеров:*\n"]
    for m in managers:
        status = "🟢" if m['is_active'] else "🔴"
        tg_ids = ", ".join(str(tid) for tid in m['telegram_ids']) if m['telegram_ids'] else "—"
        lines.append(f"{status} *{m['name']}*\n   ID: `{tg_ids}`")

    await callback.message.edit_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=back_to_managers()
    )


# ============== ДОБАВЛЕНИЕ МЕНЕДЖЕРА ==============

@router.callback_query(F.data == "mgr:add")
async def add_manager_start(callback: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin:
        return
    await state.set_state(AddManagerStates.waiting_name)
    await callback.message.edit_text(
        "➕ *Добавление менеджера*\n\nВведите *имя сотрудника* (например: Иван Петров):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@router.message(AddManagerStates.waiting_name)
async def add_manager_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("⚠️ Имя слишком короткое. Попробуйте ещё раз:")
        return

    await state.update_data(name=name)
    await state.set_state(AddManagerStates.waiting_telegram_id)
    await message.answer(
        f"Имя: *{name}*\n\nТеперь введите *Telegram ID* сотрудника (число):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@router.message(AddManagerStates.waiting_telegram_id)
async def add_manager_telegram_id(message: Message, state: FSMContext):
    tid_text = message.text.strip()
    if not tid_text.isdigit():
        await message.answer("⚠️ Telegram ID должен быть числом. Попробуйте ещё раз:")
        return

    telegram_id = int(tid_text)
    data = await state.get_data()
    name = data['name']

    try:
        manager = await create_manager(name=name, telegram_id=telegram_id)
        await message.answer(
            f"✅ Менеджер добавлен!\n\n"
            f"👤 Имя: *{manager['name']}*\n"
            f"🆔 Telegram ID: `{telegram_id}`",
            parse_mode="Markdown",
            reply_markup=back_to_managers()
        )
    except Exception as e:
        err = str(e)
        if "managers_name_unique" in err:
            await message.answer(
                f"⚠️ Менеджер с именем *{name}* уже существует.",
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
            await message.answer(f"❌ Ошибка: {err}", reply_markup=back_to_managers())

    await state.clear()


# ============== ИЗМЕНЕНИЕ / ДОБАВЛЕНИЕ ID ==============

@router.callback_query(F.data == "mgr:edit_id")
async def edit_id_start(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    managers = await list_managers(only_active=True)
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
async def edit_id_select(callback: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin:
        return
    manager_id = int(callback.data.split(":")[2])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
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
async def delete_manager_start(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    managers = await list_managers(only_active=True)
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
async def delete_manager_confirm(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    manager_id = int(callback.data.split(":")[2])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return

    await callback.message.edit_text(
        f"⚠️ Удалить менеджера *{manager['name']}*?\n\n"
        f"_Запись будет деактивирована, его данные сохранятся в БД._",
        parse_mode="Markdown",
        reply_markup=confirm_delete_kb(manager_id)
    )


@router.callback_query(F.data.startswith("mgr_del_confirm:"))
async def delete_manager_do(callback: CallbackQuery, is_admin: bool):
    if not is_admin:
        return
    manager_id = int(callback.data.split(":")[1])
    manager = await get_manager_by_id(manager_id)
    if not manager:
        await callback.message.edit_text("⚠️ Менеджер не найден.", reply_markup=back_to_managers())
        return

    await deactivate_manager(manager_id)
    await callback.message.edit_text(
        f"✅ Менеджер *{manager['name']}* деактивирован.",
        parse_mode="Markdown",
        reply_markup=back_to_managers()
    )
