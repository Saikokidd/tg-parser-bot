"""
Универсальный поиск военных и родственников.
Доступ: super_admin, office_admin, office_supervisor.
Меню: ⚙️ Управление ботом → 🔎 Поиск лида.

Запрос — свободный текст. Бот определяет:
- ФИО (текст) → ILIKE по full_name
- Дата (ДД.ММ.ГГГГ) → точное совпадение по birth_date
- Телефон (≥10 цифр) → по последним 10 цифрам phone родственников

Доступ изолирован по офису (B1).
"""
import logging
import html as _html
from typing import Optional

from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from bot.db.queries import (
    search_leads,
    get_military_by_id,
    get_relative_by_id,
    get_relatives_of_military,
)
from bot.keyboards.menus import search_results_kb

logger = logging.getLogger(__name__)
router = Router()


class SearchStates(StatesGroup):
    waiting_query = State()


# ──────────── ВХОД ──────────────────────────────────────────

@router.callback_query(F.data == "admin:search")
async def search_open(callback: CallbackQuery, state: FSMContext,
                      role: str = None, office: str = None):
    """Вход в поиск из меню админа"""
    if role not in ("super_admin", "office_admin", "office_supervisor"):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    await state.set_state(SearchStates.waiting_query)
    await callback.answer()
    await callback.message.edit_text(
        "*Поиск лида*\n\n"
        "Введите для поиска:\n"
        "• ФИО (или часть): `Иванов`\n"
        "• Дату рождения: `15.03.1985`\n"
        "• ФИО + дату: `Иванов 15.03.1985`\n"
        "• Номер телефона: `+79991234567`\n\n"
        "Поиск идёт по военным и родственникам.\n"
        "Отмена — /cancel",
        parse_mode="Markdown",
    )


# ──────────── /cancel ───────────────────────────────────────

@router.message(Command("cancel"), SearchStates.waiting_query)
async def search_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Поиск отменён.")


# ──────────── ПРИЁМ ЗАПРОСА ─────────────────────────────────

@router.message(SearchStates.waiting_query)
async def search_query(message: Message, state: FSMContext, manager: dict | None,
                       role: str = None, office: str = None):
    query = (message.text or "").strip()
    if not query:
        await message.answer("⚠️ Пустой запрос. Введите текст или /cancel")
        return

    # Сбросим FSM — результаты — отдельное "состояние без состояния"
    await state.clear()

    manager_id = manager["id"] if manager else None
    LIMIT = 20

    try:
        result = await search_leads(
            query=query,
            role=role,
            office=office,
            manager_id=manager_id,
            limit=LIMIT,
        )
    except Exception:
        logger.exception("search_leads failed for query=%r", query)
        await message.answer("⚠️ Ошибка при поиске. Попробуйте ещё раз.")
        return

    mil = result["military"]
    rel = result["relatives"]
    mil_total = result["military_total"]
    rel_total = result["relatives_total"]

    if mil_total == 0 and rel_total == 0:
        await message.answer("Ничего не найдено.")
        return

    # Формируем сводку
    lines = [f"<b>Результаты поиска:</b> <code>{_html.escape(query)}</code>", ""]

    if mil_total > 0:
        lines.append(f"<b>[ВОЕННЫЕ]</b> найдено {mil_total}"
                     + (f", показаны первые {len(mil)}" if mil_total > len(mil) else ""))
        for m in mil:
            birth = m.get("birth_date")
            birth_str = birth.strftime("%d.%m.%Y") if birth else "—"
            mgr = m.get("manager_name") or "—"
            office_str = m.get("office") or "—"
            rel_cnt = m.get("relatives_count") or 0
            lines.append(
                f"  • <b>{_html.escape(m['full_name'])}</b> • {birth_str}\n"
                f"    Офис: {office_str} | Менеджер: {_html.escape(mgr)} | Родственников: {rel_cnt}"
            )
        lines.append("")

    if rel_total > 0:
        lines.append(f"<b>[РОДСТВЕННИКИ]</b> найдено {rel_total}"
                     + (f", показаны первые {len(rel)}" if rel_total > len(rel) else ""))
        for r in rel:
            birth = r.get("birth_date")
            birth_str = birth.strftime("%d.%m.%Y") if birth else "—"
            mgr = r.get("manager_name") or "—"
            office_str = r.get("office") or "—"
            phone = r.get("phone") or ""
            phone_str = f" | Тел: {_html.escape(phone)}" if phone else ""
            attached = r.get("attached_to") or []
            if attached:
                names = ", ".join(
                    f"{_html.escape(a['full_name'])}"
                    for a in attached[:3]
                )
                if len(attached) > 3:
                    names += f" и ещё {len(attached) - 3}"
                attached_str = f"\n    Привязан к: {names}"
            else:
                attached_str = "\n    Не привязан ни к кому"
            lines.append(
                f"  • <b>{_html.escape(r['full_name'])}</b> • {birth_str}{phone_str}\n"
                f"    Офис: {office_str} | Менеджер: {_html.escape(mgr)}{attached_str}"
            )
        lines.append("")

    if mil_total > LIMIT or rel_total > LIMIT:
        lines.append("<i>Найдено больше чем показано. Уточните запрос (например, добавьте дату).</i>")

    text = "\n".join(lines)

    # Клавиатура с кнопками "Открыть"
    kb = search_results_kb(mil, rel)

    # Текст может выйти за 4096 — порежем безопасно
    if len(text) > 4096:
        text = text[:4000] + "\n...\n<i>(результаты обрезаны, уточните запрос)</i>"

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ──────────── ОТКРЫТИЕ КАРТОЧКИ ─────────────────────────────

@router.callback_query(F.data.startswith("search:m:"))
async def search_open_military(callback: CallbackQuery, manager: dict | None = None,
                                role: str = None, office: str = None):
    """Открыть карточку военного из результатов поиска"""
    military_id = int(callback.data.split(":")[2])

    military = await get_military_by_id(military_id)
    if not military:
        await callback.answer("Лид не найден (возможно удалён).", show_alert=True)
        return

    # Доступ: super_admin видит всё, office_admin/supervisor — свой офис
    if role in ("office_admin", "office_supervisor"):
        if military.get("office") != office:
            await callback.answer("Лид из чужого офиса.", show_alert=True)
            return
    elif role == "manager":
        # На всякий случай — кнопка для admin'ов и пультов, но защитимся
        mgr_id = manager["id"] if manager else None
        if military.get("added_by") != mgr_id:
            await callback.answer("Это не ваш лид.", show_alert=True)
            return

    # Формируем компактную карточку прямо в результатах
    relatives = await get_relatives_of_military(military_id)
    text = _format_military_compact(military, relatives)

    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("search:r:"))
async def search_open_relative(callback: CallbackQuery, manager: dict | None = None,
                                role: str = None, office: str = None):
    """Открыть карточку родственника (показываем привязки + контакты)"""
    relative_id = int(callback.data.split(":")[2])

    relative = await get_relative_by_id(relative_id)
    if not relative:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Доступ
    if role in ("office_admin", "office_supervisor"):
        if relative.get("office") != office:
            await callback.answer("Запись из чужого офиса.", show_alert=True)
            return
    elif role == "manager":
        mgr_id = manager["id"] if manager else None
        if relative.get("added_by") != mgr_id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

    text = _format_relative_compact(relative)

    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "search:close")
async def search_close(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ──────────── ФОРМАТЫ КАРТОЧЕК ──────────────────────────────

def _format_military_compact(m: dict, relatives: list) -> str:
    birth = m.get("birth_date")
    birth_str = birth.strftime("%d.%m.%Y") if birth else "—"

    lines = [
        f"<b>[ВОЕННЫЙ]</b> <b>{_html.escape(m['full_name'])}</b>",
        f"ДР: {birth_str}",
        f"Офис: {m.get('office') or '—'}",
        f"ID лида: {m['id']}",
    ]
    if m.get("status"):
        lines.append(f"Статус: {_html.escape(str(m['status']))}")

    extra = m.get("extra") or {}
    if extra.get("note"):
        lines.append(f"Доп.инфа: {_html.escape(str(extra['note']))}")
    if extra.get("source"):
        lines.append(f"Источник: {_html.escape(str(extra['source']))}")
    if extra.get("unit"):
        lines.append(f"Б/Ч: {_html.escape(str(extra['unit']))}")
    if extra.get("callsign"):
        lines.append(f"Позывной: {_html.escape(str(extra['callsign']))}")

    if relatives:
        lines.append(f"\n<b>Родственников: {len(relatives)}</b>")
        for r in relatives[:10]:
            r_birth = r.get("birth_date")
            r_birth_str = r_birth.strftime("%d.%m.%Y") if r_birth else "—"
            phone = r.get("phone") or ""
            phone_str = f" | Тел: {_html.escape(phone)}" if phone else ""
            lines.append(
                f"  • {_html.escape(r['full_name'])} • {r_birth_str}{phone_str}"
            )
        if len(relatives) > 10:
            lines.append(f"  <i>...и ещё {len(relatives) - 10}</i>")
    else:
        lines.append("\n<i>Родственники не привязаны</i>")

    return "\n".join(lines)


def _format_relative_compact(r: dict) -> str:
    birth = r.get("birth_date")
    birth_str = birth.strftime("%d.%m.%Y") if birth else "—"

    lines = [
        f"<b>[РОДСТВЕННИК]</b> <b>{_html.escape(r['full_name'])}</b>",
        f"ДР: {birth_str}",
        f"Офис: {r.get('office') or '—'}",
        f"ID: {r['id']}",
    ]
    if r.get("phone"):
        lines.append(f"Тел: {_html.escape(r['phone'])}")
    if r.get("address"):
        addr = r["address"]
        if len(addr) > 100:
            addr = addr[:97] + "..."
        lines.append(f"Адрес: {_html.escape(addr)}")

    extra = r.get("extra") or {}
    for key, label in [("snils", "СНИЛС"), ("inn", "ИНН"), ("passport", "Паспорт"),
                       ("email", "Почта")]:
        if extra.get(key):
            lines.append(f"{label}: {_html.escape(str(extra[key]))}")

    return "\n".join(lines)
