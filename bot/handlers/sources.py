"""
Раздел "📌 Мои источники" — менеджер управляет своими источниками лидов.
Доступ: manager и office_admin (у них есть свои лиды, есть смысл иметь
источники). Office_supervisor и super_admin (без manager-роли) — без доступа.

Меню:
    Главное меню (reply) → "📌 Мои источники"
      ↓
    Список своих источников с пагинацией (5/стр)
      ↓ (клик на источник)
    Карточка: имя, дата, лидов
      ↓
    [✏️ Переименовать] [❌ Удалить] [« Назад]
"""
import logging
import html as _html

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery

from bot.db.queries import (
    list_sources_by_manager,
    count_sources_by_manager,
    get_source_by_id,
    count_military_by_source,
    rename_source,
    soft_delete_source,
    find_source_by_normalized_name,
)
from bot.keyboards.menus import (
    my_sources_list_kb,
    source_card_kb,
    confirm_delete_source_kb,
)

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 5


class MySourcesStates(StatesGroup):
    waiting_rename = State()  # ждём новый текст имени


# ──────────── Вход (reply-кнопка) ────────────

@router.message(F.text == "📌 Мои источники")
async def open_my_sources(message: Message, state: FSMContext, manager: dict | None,
                          role: str = None):
    if not _has_access(role, manager):
        await message.answer("Раздел доступен менеджерам и админам офиса.")
        return
    await state.clear()  # выходим из любого FSM
    await _show_list(message, manager_id=manager["id"], page=1)


def _has_access(role: str | None, manager: dict | None) -> bool:
    """Только manager и office_admin (у которых есть свои лиды/источники)."""
    if role == "manager":
        return manager is not None
    if role == "office_admin":
        return manager is not None
    if role == "super_admin":
        return manager is not None  # супер-админ если он одновременно менеджер
    return False


async def _show_list(target, manager_id: int, page: int = 1,
                     edit: bool = False, kb_from_callback=None):
    """
    Показать список источников. target — Message или CallbackQuery.message.
    """
    total = await count_sources_by_manager(manager_id)
    if total == 0:
        text = (
            "📌 <b>Мои источники</b>\n\n"
            "У вас пока нет сохранённых источников.\n"
            "Они появятся автоматически когда вы введёте новый источник "
            "на шаге «Откуда взяли данные?» при создании лида."
        )
        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML")
            except Exception:
                await target.answer(text, parse_mode="HTML")
        else:
            await target.answer(text, parse_mode="HTML")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    items = await list_sources_by_manager(manager_id, page=page, page_size=PAGE_SIZE)

    text = (
        f"📌 <b>Мои источники</b>\n\n"
        f"Всего: {total}. Жмите на источник чтобы открыть карточку."
    )
    kb = my_sources_list_kb(items, page=page, total_pages=total_pages)
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await target.answer(text, parse_mode="HTML", reply_markup=kb)


# ──────────── Пагинация ────────────

@router.callback_query(F.data.startswith("mysrc:page:"))
async def my_sources_page(callback: CallbackQuery, manager: dict | None,
                           role: str = None):
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
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
    await _show_list(callback.message, manager_id=manager["id"], page=page, edit=True)


# ──────────── Закрыть ────────────

@router.callback_query(F.data == "mysrc:close")
async def my_sources_close(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ──────────── Назад к списку из карточки ────────────

@router.callback_query(F.data == "mysrc:back")
async def my_sources_back(callback: CallbackQuery, manager: dict | None,
                           role: str = None):
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
    await callback.answer()
    await _show_list(callback.message, manager_id=manager["id"], page=1, edit=True)


# ──────────── Открыть карточку ────────────

@router.callback_query(F.data.startswith("mysrc:open:"))
async def my_sources_open(callback: CallbackQuery, manager: dict | None,
                           role: str = None):
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
    await callback.answer()
    try:
        source_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    src = await get_source_by_id(source_id)
    if not src or src["owner_manager_id"] != manager["id"] or not src["is_active"]:
        await callback.message.answer("⚠️ Источник недоступен.")
        return

    lead_count = await count_military_by_source(source_id)
    created = src["created_at"].strftime("%d.%m.%Y")
    name = _html.escape(src["name"])

    text = (
        f"📌 <b>{name}</b>\n\n"
        f"Создан: {created}\n"
        f"Лидов на источнике: <b>{lead_count}</b>"
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=source_card_kb(source_id),
        )
    except Exception:
        await callback.message.answer(
            text, parse_mode="HTML",
            reply_markup=source_card_kb(source_id),
        )


# ──────────── Переименование ────────────

@router.callback_query(F.data.startswith("mysrc:rename:"))
async def my_sources_rename_start(callback: CallbackQuery, state: FSMContext,
                                   manager: dict | None, role: str = None):
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
    await callback.answer()
    try:
        source_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    src = await get_source_by_id(source_id)
    if not src or src["owner_manager_id"] != manager["id"] or not src["is_active"]:
        await callback.message.answer("⚠️ Источник недоступен.")
        return

    await state.set_state(MySourcesStates.waiting_rename)
    await state.update_data(rename_source_id=source_id)

    name = _html.escape(src["name"])
    await callback.message.answer(
        f"✏️ Текущее имя: <b>{name}</b>\n\n"
        f"Введите новое имя источника или /cancel.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), MySourcesStates.waiting_rename)
async def my_sources_rename_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Переименование отменено.")


@router.message(MySourcesStates.waiting_rename)
async def my_sources_rename_apply(message: Message, state: FSMContext,
                                    manager: dict | None, role: str = None):
    if not _has_access(role, manager):
        await state.clear()
        return

    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("⚠️ Пустое имя. Введите новое имя или /cancel.")
        return

    data = await state.get_data()
    source_id = data.get("rename_source_id")
    if not source_id:
        await state.clear()
        await message.answer("⚠️ Состояние утеряно. Откройте «📌 Мои источники» заново.")
        return

    ok, err = await rename_source(source_id, new_name)
    if not ok:
        if err and err.startswith("taken_by_office:"):
            office = err.split(":")[1] or "—"
            await message.answer(
                f"❌ Имя <b>{_html.escape(new_name)}</b> уже занято менеджером "
                f"из офиса <b>{office}</b>.\n\n"
                f"Введите другое или /cancel.",
                parse_mode="HTML",
            )
            return
        elif err == "empty":
            await message.answer("⚠️ Пустое имя. Введите новое имя или /cancel.")
            return
        else:
            await message.answer("⚠️ Не удалось переименовать. Попробуйте ещё раз или /cancel.")
            return

    await state.clear()
    await message.answer(
        f"✅ Источник переименован в <b>{_html.escape(new_name)}</b>.",
        parse_mode="HTML",
    )
    # Сразу показываем обновлённый список
    await _show_list(message, manager_id=manager["id"], page=1)


# ──────────── Удаление ────────────

@router.callback_query(F.data.startswith("mysrc:delete:"))
async def my_sources_delete_ask(callback: CallbackQuery, manager: dict | None,
                                  role: str = None):
    """Подтверждение перед удалением."""
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
    await callback.answer()
    try:
        source_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    src = await get_source_by_id(source_id)
    if not src or src["owner_manager_id"] != manager["id"] or not src["is_active"]:
        await callback.message.answer("⚠️ Источник недоступен.")
        return

    lead_count = await count_military_by_source(source_id)
    name = _html.escape(src["name"])

    text = (
        f"❓ Удалить источник <b>{name}</b>?\n\n"
        f"К нему привязано лидов: <b>{lead_count}</b>.\n"
        f"Сами лиды останутся, но источник пропадёт из вашего списка "
        f"и из выбора при создании новых лидов.\n\n"
        f"Восстановить можно будет только через админа."
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=confirm_delete_source_kb(source_id),
        )
    except Exception:
        await callback.message.answer(
            text, parse_mode="HTML",
            reply_markup=confirm_delete_source_kb(source_id),
        )


@router.callback_query(F.data.startswith("mysrc:delete_confirm:"))
async def my_sources_delete_confirm(callback: CallbackQuery, manager: dict | None,
                                      role: str = None):
    """Подтверждено — soft delete."""
    if not _has_access(role, manager):
        await callback.answer("Доступа нет.", show_alert=True)
        return
    await callback.answer()
    try:
        source_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    src = await get_source_by_id(source_id)
    if not src or src["owner_manager_id"] != manager["id"]:
        await callback.message.answer("⚠️ Источник недоступен.")
        return

    ok = await soft_delete_source(source_id)
    if ok:
        await callback.message.edit_text(
            f"✅ Источник <b>{_html.escape(src['name'])}</b> удалён.",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("⚠️ Не удалось удалить.")
    # Не возвращаемся в список — пользователь сам откроет если нужно
