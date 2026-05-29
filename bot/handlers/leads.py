"""
Хендлер "Список лидов".

Видимость:
- Менеджер → только свои военные
- Админ / Пульт → все военные

Флоу:
- Кнопка "Список лидов" → пагинированный список (по 20)
- Клик на лида → карточка с родственниками + действия
- Кнопки: дополнить, удалить лида, удалить/редактировать родственника
- Редактирование любых стандартных полей + кастомные через JSONB extra
"""
import logging
import re
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.db.queries import (
    list_military_paginated, get_military_by_id,
    get_relatives_of_military,
    delete_military_cascade, delete_relative_cascade,
    get_relative_by_id,
    update_relative_field, update_relative_extra,
    list_military_paginated_v2, get_military_by_id_office_check,    # B1: office-aware
)
from bot.parser.military_parser import status_label
from bot.parser.relative_parser import parse_date as parse_rel_date, normalize_phone
from bot.keyboards.menus import (
    leads_list_kb, lead_card_kb,
    confirm_delete_lead_kb, confirm_delete_relative_kb,
    edit_relative_fields_kb, EDIT_FIELDS,
)
from bot.handlers import relatives as relatives_handler

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 20

# ──────────── B1: office-aware хелперы ────────────

def _office_filter_for(role: str, office: str | None) -> str | None:
    """
    Определить какой office_filter применить в SELECT-запросах для текущей роли.

    Возвращает:
        None    — фильтр не нужен (super_admin видит всё)
        'xxx'   — фильтровать по этому офису

    Логика:
    - super_admin → None (видит все офисы)
    - office_admin/office_supervisor → свой офис
    - manager → свой офис (страховка; основной фильтр у него по manager_id)
    """
    if role == "super_admin":
        return None
    return office  # для admin/supervisor/manager — их office из контекста


def _can_access_military(military: dict, role: str, office: str | None,
                          manager_id: int | None) -> bool:
    """
    Может ли текущий пользователь видеть/менять конкретного военного.

    super_admin — может всё.
    office_admin/office_supervisor — только лиды своего офиса.
    manager — только свои лиды (added_by совпадает) и в своём офисе.

    Защита от подделанных callback_data: даже если пришёл lead:show:N
    с чужим N, мы не дадим открыть карточку.
    """
    if not military:
        return False
    if role == "super_admin":
        return True
    mil_office = military.get("office")
    if role in ("office_admin", "office_supervisor"):
        return mil_office == office
    if role == "manager":
        if military.get("added_by") != manager_id:
            return False
        # Перестраховка — если у лида другой office (теоретически не должно быть)
        if mil_office and office and mil_office != office:
            return False
        return True
    return False

# Лейблы стандартных полей по их id
FIELD_LABELS = {fid: label for fid, label, _ in EDIT_FIELDS}
FIELD_TYPES = {fid: ftype for fid, label, ftype in EDIT_FIELDS}


# ──────────── FSM ────────────

class EditRelativeStates(StatesGroup):
    waiting_value = State()


# ──────────── ВНУТРЕННЕЕ: рендеры ────────────

async def _render_list(target_message: Message, manager: dict | None,
                       is_admin: bool, is_supervisor: bool, page: int,
                       role: str = None, office: str = None):
    """
    Отрендерить страницу списка лидов.

    Логика фильтров (B1 — мульти-офисность):
    - super_admin → manager_id=None, office_filter=None → видит всё
    - office_admin/supervisor → manager_id=None, office_filter=свой_офис
    - manager → manager_id=свой, office_filter=None (office применится через added_by)
    """
    # Определяем manager_id для фильтра
    if role == "manager":
        manager_id = manager["id"] if manager else None
    else:
        # admin/supervisor видят всех менеджеров (своего офиса)
        manager_id = None

    # Определяем office_filter
    office_filter = _office_filter_for(role, office)

    records, total = await list_military_paginated_v2(
        manager_id=manager_id,
        office_filter=office_filter,
        page=page,
        page_size=PAGE_SIZE,
    )

    if not records:
        text = "📭 Список пуст." if page == 1 else "📭 На этой странице пусто."
        if target_message.from_user.is_bot:
            await target_message.edit_text(text)
        else:
            await target_message.answer(text)
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    header = f"*Список лидов*  ({total} всего)"
    kb = leads_list_kb(records, page=page, total_pages=total_pages)

    if target_message.from_user.is_bot:
        await target_message.edit_text(header, parse_mode="Markdown", reply_markup=kb)
    else:
        await target_message.answer(header, parse_mode="Markdown", reply_markup=kb)


def _md_escape(text) -> str:
    """
    Экранировать спецсимволы Markdown (legacy mode aiogram).
    Telegram Markdown понимает: _ * ` [
    Если их не экранировать — упадёт 400 'can't parse entities'.
    """
    if text is None:
        return "—"
    s = str(text)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def _format_lead_card(military: dict, relatives: list) -> str:
    """Текст карточки лида (Markdown-safe — экранируем спецсимволы в данных)"""
    e = _md_escape  # короткий алиас

    birth = military.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    extra = military.get('extra') or {}

    lines = [
        f"*{e(military.get('full_name'))}*",
        f"ДР: {birth_str}",
        f"Статус: {e(status_label(military.get('status')))}",
    ]
    if extra.get('unit'):
        lines.append(f"Б/Ч: {e(extra['unit'])}")
    if extra.get('callsign'):
        lines.append(f"Позывной: {e(extra['callsign'])}")
    if extra.get('note'):
        lines.append(f"Доп.инфа: {e(extra['note'])}")
    if extra.get('source'):
        lines.append(f"Источник: {e(extra['source'])}")
    lines.append("")

    if relatives:
        lines.append(f"*Лица ({len(relatives)}):*")
        for i, r in enumerate(relatives, 1):
            r_birth = r.get('birth_date')
            r_birth_str = r_birth.strftime('%d.%m.%Y') if r_birth else '—'
            block = [f"{i}. *{e(r.get('full_name'))}*"]
            block.append(f"   ДР: {r_birth_str}")
            if r.get('phone'):
                block.append(f"   📞 {e(r['phone'])}")
            if r.get('address'):
                addr = r['address']
                if len(addr) > 80:
                    addr = addr[:77] + "..."
                block.append(f"   🏠 {e(addr)}")
            r_extra = r.get('extra') or {}
            for key, label in [('snils', 'СНИЛС'), ('inn', 'ИНН'),
                               ('passport', 'Паспорт'), ('email', 'Почта'),
                               ('operator', 'Оператор'), ('region', 'Регион')]:
                if r_extra.get(key):
                    block.append(f"   {label}: {e(r_extra[key])}")
            # Стандартные ключи которые мы уже показали выше + служебные
            std_keys = {'snils', 'inn', 'passport', 'email', 'operator', 'region',
                        'voxlink_attempts', 'voxlink_skipped'}
            for key, val in r_extra.items():
                if key not in std_keys and val:
                    block.append(f"   {e(key)}: {e(val)}")
            lines.append("\n".join(block))
    else:
        lines.append("_Лица не привязаны._")
    return "\n".join(lines)


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "Список лидов")
async def btn_leads_list(message: Message, manager: dict | None,
                         is_admin: bool, is_supervisor: bool,
                         role: str = None, office: str = None):
    if not manager and not is_admin and not is_supervisor:
        await message.answer("Доступ запрещён.")
        return
    await _render_list(message, manager, is_admin, is_supervisor, page=1,
                       role=role, office=office)


# ──────────── ПАГИНАЦИЯ ────────────

@router.callback_query(F.data.startswith("lead:page:"))
async def leads_page(callback: CallbackQuery, manager: dict | None,
                     is_admin: bool, is_supervisor: bool,
                     role: str = None, office: str = None):
    page = int(callback.data.split(":")[2])
    await callback.answer()
    await _render_list(callback.message, manager, is_admin, is_supervisor, page=page,
                       role=role, office=office)


@router.callback_query(F.data == "lead:noop")
async def leads_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "lead:back")
async def leads_back(callback: CallbackQuery, manager: dict | None,
                     is_admin: bool, is_supervisor: bool,
                     role: str = None, office: str = None):
    await callback.answer()
    await _render_list(callback.message, manager, is_admin, is_supervisor, page=1,
                       role=role, office=office)


# ──────────── КАРТОЧКА ЛИДА ────────────

@router.callback_query(F.data.startswith("lead:show:"))
async def lead_show(callback: CallbackQuery, manager: dict | None = None,
                    role: str = None, office: str = None):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.answer("Лид не найден.", show_alert=True)
        return

    # B1: проверка офисного доступа
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Лид недоступен.", show_alert=True)
        return

    relatives = await get_relatives_of_military(military_id)
    text = _format_lead_card(military, relatives)
    kb = lead_card_kb(military_id, relatives)

    await callback.answer()
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning(f"edit_text failed: {e}")
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ──────────── УДАЛЕНИЕ ЛИДА ────────────

@router.callback_query(F.data.startswith("lead:del:"))
async def lead_delete_confirm(callback: CallbackQuery, manager: dict | None = None,
                              role: str = None, office: str = None):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.answer("Лид не найден.", show_alert=True)
        return

    # B1: проверка офисного доступа
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Лид недоступен.", show_alert=True)
        return

    relatives = await get_relatives_of_military(military_id)
    rel_count = len(relatives)
    rel_text = f" и {rel_count} привязанных лиц" if rel_count else ""

    await callback.message.edit_text(
        f"⚠️ *Удалить лида?*\n\n"
        f"{military['full_name']}{rel_text}\n\n"
        f"_Действие необратимо._",
        parse_mode="Markdown",
        reply_markup=confirm_delete_lead_kb(military_id)
    )


@router.callback_query(F.data.startswith("lead:del_yes:"))
async def lead_delete_do(callback: CallbackQuery, manager: dict | None,
                         is_admin: bool, is_supervisor: bool,
                         role: str = None, office: str = None):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.answer("Лид не найден.", show_alert=True)
        return

    # B1: проверка офисного доступа
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Лид недоступен.", show_alert=True)
        return

    deleted_relatives = await delete_military_cascade(military_id)

    rel_text = f" + удалено {deleted_relatives} лиц" if deleted_relatives else ""
    await callback.message.edit_text(
        f"✅ Удалено: *{military['full_name']}*{rel_text}",
        parse_mode="Markdown"
    )

    await _render_list(callback.message, manager, is_admin, is_supervisor, page=1,
                       role=role, office=office)


# ──────────── УДАЛЕНИЕ РОДСТВЕННИКА ────────────

@router.callback_query(F.data.startswith("rel:del:"))
async def relative_delete_confirm(callback: CallbackQuery, manager: dict | None = None,
                                  role: str = None, office: str = None):
    parts = callback.data.split(":")
    relative_id = int(parts[2])
    military_id = int(parts[3])

    # Проверка офисного доступа через военного-владельца
    military = await get_military_by_id(military_id)
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Запись недоступна.", show_alert=True)
        return

    relative = await get_relative_by_id(relative_id)
    if not relative:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚠️ *Удалить запись?*\n\n"
        f"{relative['full_name']}\n\n"
        f"_Удаление полное (включая связи с другими лидами)._",
        parse_mode="Markdown",
        reply_markup=confirm_delete_relative_kb(relative_id, military_id)
    )


@router.callback_query(F.data.startswith("rel:del_yes:"))
async def relative_delete_do(callback: CallbackQuery, manager: dict | None = None,
                             role: str = None, office: str = None):
    parts = callback.data.split(":")
    relative_id = int(parts[2])
    military_id = int(parts[3])

    # Проверка офисного доступа
    military = await get_military_by_id(military_id)
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Запись недоступна.", show_alert=True)
        return

    relative = await get_relative_by_id(relative_id)
    if relative:
        await delete_relative_cascade(relative_id)
        await callback.answer(f"Удалено: {relative['full_name']}")
    else:
        await callback.answer("Уже удалено.")

    if not military:
        await callback.message.edit_text("Лид удалён.")
        return

    relatives = await get_relatives_of_military(military_id)
    text = _format_lead_card(military, relatives)
    kb = lead_card_kb(military_id, relatives)
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


# ──────────── ДОПОЛНИТЬ ЛИЦО (из карточки) ────────────

@router.callback_query(F.data.startswith("lead:addrel:"))
async def lead_add_relative(callback: CallbackQuery, state: FSMContext,
                             manager: dict | None,
                             role: str = None, office: str = None):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.answer("Лид не найден.", show_alert=True)
        return

    # B1: проверка офисного доступа
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Лид недоступен.", show_alert=True)
        return

    if manager:
        manager_id = manager['id']
    else:
        manager_id = military['added_by']

    await state.set_state(relatives_handler.RelativeStates.waiting_template)
    await state.update_data(military_id=military_id, manager_id=manager_id)

    await callback.answer()
    await callback.message.edit_text(
        f"*{military['full_name']}*\n\n"
        f"Заполните данные.\n\n"
        f"_Для отмены — /cancel_",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════
#                  РЕДАКТИРОВАНИЕ РОДСТВЕННИКА
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("rel:edit:"))
async def relative_edit_menu(callback: CallbackQuery, manager: dict | None = None,
                             role: str = None, office: str = None):
    """Показать меню выбора поля для редактирования"""
    parts = callback.data.split(":")
    relative_id = int(parts[2])
    military_id = int(parts[3])

    # B1: проверка офисного доступа через военного-владельца
    military = await get_military_by_id(military_id)
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Запись недоступна.", show_alert=True)
        return

    relative = await get_relative_by_id(relative_id)
    if not relative:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    extra = relative.get('extra') or {}
    birth = relative.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'

    lines = [
        f"*Редактирование:* {relative['full_name']}\n",
        f"ФИО: {relative.get('full_name') or '—'}",
        f"ДР: {birth_str}",
        f"Телефон: {relative.get('phone') or '—'}",
        f"Адрес: {relative.get('address') or '—'}",
        f"СНИЛС: {extra.get('snils') or '—'}",
        f"ИНН: {extra.get('inn') or '—'}",
        f"Паспорт: {extra.get('passport') or '—'}",
        f"Почта: {extra.get('email') or '—'}",
    ]
    std_keys = {'snils', 'inn', 'passport', 'email'}
    custom = [(k, v) for k, v in extra.items() if k not in std_keys]
    if custom:
        lines.append("\n_Свои поля:_")
        for k, v in custom:
            lines.append(f"{k}: {v}")

    lines.append("\nВыберите поле для редактирования:")

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=edit_relative_fields_kb(relative_id, military_id)
    )


@router.callback_query(F.data.startswith("rel:editfield:"))
async def relative_edit_field(callback: CallbackQuery, state: FSMContext,
                              manager: dict | None = None,
                              role: str = None, office: str = None):
    """Выбрано конкретное поле — спрашиваем новое значение"""
    parts = callback.data.split(":")
    relative_id = int(parts[2])
    military_id = int(parts[3])
    field_id = parts[4]

    # B1: проверка офисного доступа через военного-владельца
    military = await get_military_by_id(military_id)
    manager_id = manager["id"] if manager else None
    if not _can_access_military(military, role, office, manager_id):
        await callback.answer("Запись недоступна.", show_alert=True)
        return

    await state.set_state(EditRelativeStates.waiting_value)
    await state.update_data(
        relative_id=relative_id,
        military_id=military_id,
        field_id=field_id,
    )

    if field_id == "_custom":
        prompt = (
            "Введите *своё поле* в формате:\n\n"
            "`название: значение`\n\n"
            "Например: `Telegram: @ivanov` или `Whatsapp: +79991234567`\n\n"
            "Чтобы удалить поле — оставьте значение пустым: `название:`\n\n"
            "_Для отмены — /cancel_"
        )
    else:
        label = FIELD_LABELS.get(field_id, field_id)
        prompt = (
            f"Введите новое значение для *{label}*.\n\n"
            f"_Чтобы удалить поле — отправьте слово_ `пусто`\n"
            f"_Для отмены — /cancel_"
        )

    await callback.answer()
    await callback.message.edit_text(prompt, parse_mode="Markdown")


@router.message(EditRelativeStates.waiting_value)
async def relative_edit_save(message: Message, state: FSMContext):
    """Получено новое значение — сохраняем"""
    data = await state.get_data()
    relative_id = data['relative_id']
    military_id = data['military_id']
    field_id = data['field_id']

    raw = message.text.strip()

    # ──────── Кастомное поле ────────
    if field_id == "_custom":
        if ':' not in raw:
            await message.answer(
                "⚠️ Формат: `название: значение`\nПопробуйте ещё раз или /cancel.",
                parse_mode="Markdown"
            )
            return
        key, value = raw.split(':', 1)
        key = key.strip()
        value = value.strip()
        if not key:
            await message.answer("⚠️ Название поля пустое. Попробуйте ещё раз или /cancel.")
            return
        if key.lower() in {"фио", "др", "телефон", "адрес"}:
            await message.answer(
                f"⚠️ Поле `{key}` — структурное. Используйте кнопки выше для его редактирования."
            )
            return
        await update_relative_extra(relative_id, key, value if value else None)

    else:
        # ──────── Стандартное поле ────────
        if raw.lower() == "пусто":
            value = None
        else:
            value = raw

        ftype = FIELD_TYPES.get(field_id)

        if ftype == "structural":
            if field_id == "birth_date" and value:
                parsed = parse_rel_date(value)
                if not parsed:
                    await message.answer(
                        "⚠️ Не удалось распознать дату. Форматы: 15.03.1985, 15/03/85, 1985-03-15.\n"
                        "Попробуйте ещё раз или /cancel."
                    )
                    return
                value = parsed
            elif field_id == "phone" and value:
                value = normalize_phone(value)

            try:
                await update_relative_field(relative_id, field_id, value)
            except ValueError as e:
                await message.answer(f"⚠️ {e}")
                return

        elif ftype == "extra":
            if value and field_id in ("snils", "inn", "passport"):
                value = re.sub(r'\D', '', value)
            if value and field_id == "email":
                value = value.lower()
            await update_relative_extra(relative_id, field_id, value if value else None)

    await state.clear()

    # Возвращаемся в карточку лида
    military = await get_military_by_id(military_id)
    if not military:
        await message.answer("✅ Сохранено. Лид был удалён.")
        return

    relatives = await get_relatives_of_military(military_id)
    text = "✅ Сохранено.\n\n" + _format_lead_card(military, relatives)
    kb = lead_card_kb(military_id, relatives)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ──────────── /cancel в редактировании ────────────

@router.message(Command("cancel"), EditRelativeStates.waiting_value)
async def cancel_edit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Редактирование отменено.")