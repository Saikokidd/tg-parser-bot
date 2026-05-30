"""
Хендлер для работы с родственниками:
- Кнопка "✍️ Заполнить" → выбор военного → ввод данных
- Парсинг данных + дубль-чек (2 из 4 полей) с инфой о привязках
- Сохранение + связка military_relatives
- Цикл "Добавить ещё?"
"""
from datetime import datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.utils.menu_guard import is_menu_button_pressed

from bot.parser.relative_parser import (
    parse_relative, parse_relatives_batch,
    validate_relative, format_relative, format_relative_record
)

from bot.parser.military_parser import status_label
from bot.db.queries import (
    list_military_without_relatives_v2, list_military_by_manager, get_military_by_id,
    find_relative_duplicates_with_links, link_military_relative,
    find_relative_global_dup, insert_relative_v2,    # B1: глобальный дубль + office
)
from bot.keyboards.menus import (
    military_list_kb, confirm_relative_kb, add_more_relatives_kb,
    fill_action_kb,
)

router = Router()


# ──────────── FSM ────────────

class RelativeStates(StatesGroup):
    waiting_template = State()
    waiting_confirmation = State()


# ──────────── /cancel ────────────

@router.message(Command("cancel"), RelativeStates.waiting_template)
@router.message(Command("cancel"), RelativeStates.waiting_confirmation)
async def cancel_relative_flow(message: Message, state: FSMContext):
    """Команда /cancel — полностью прерывает любой режим"""
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "✍️ Заполнить")
async def btn_fill_relatives(message: Message, state: FSMContext, manager: dict | None):
    if not manager:
        await message.answer("Доступно только менеджерам.")
        return

    pending = await list_military_without_relatives_v2(
        manager_id=manager['id'],
        office_filter=manager.get('office'),
    )

    if pending:
        await message.answer(
            f"*Не заполнены ({len(pending)}):*\nВыберите запись:",
            parse_mode="Markdown",
            reply_markup=military_list_kb(pending[:30], action="rel:pick")
        )
    else:
        all_records = await list_military_by_manager(manager['id'])
        if not all_records:
            await message.answer("📭 Ваша база пустая.")
            return
        await message.answer(
            "Все записи заполнены.\n"
            "Если нужно добавить ещё — выберите запись:",
            reply_markup=military_list_kb(all_records[:30], action="rel:pick")
        )


# ──────────── ВЫБОР ВОЕННОГО ────────────

@router.callback_query(F.data.startswith("rel:pick:"))
async def relative_pick_military(callback: CallbackQuery, state: FSMContext, manager: dict):
    """После выбора лида — спрашиваем как заполнять: автопробив или вручную."""
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Запись не найдена.")
        return

    await callback.answer()

    # Запомним выбор лида (понадобится в обоих ветках)
    await state.update_data(military_id=military_id, manager_id=manager['id'])

    birth = military.get('birth_date')
    birth_str = birth.strftime('%d.%m.%Y') if birth else '—'
    has_birth = birth is not None

    if not has_birth:
        # Sauron без ДР работает плохо — сразу даём ручной ввод
        await state.set_state(RelativeStates.waiting_template)
        await callback.message.edit_text(
            f"*{military['full_name']}* (ДР: —)\n\n"
            f"⚠️ У этого лида не указана дата рождения, "
            f"автопробив через Sauron невозможен.\n\n"
            f"Заполните данные вручную.\n\n"
            f"_Для отмены — /cancel_",
            parse_mode="Markdown"
        )
        return

    # Есть ДР — предлагаем выбор
    await callback.message.edit_text(
        f"*{military['full_name']}* ({birth_str})\n\n"
        f"Как заполнить родственников?",
        parse_mode="Markdown",
        reply_markup=fill_action_kb(military_id),
    )


@router.callback_query(F.data.startswith("rel:manual:"))
async def relative_manual_entry(callback: CallbackQuery, state: FSMContext):
    """Ручной ввод шаблона родственника — старый путь"""
    await callback.answer()
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Запись не найдена.")
        return

    await state.set_state(RelativeStates.waiting_template)
    await callback.message.edit_text(
        f"*{military['full_name']}*\n\n"
        f"Заполните данные родственника.\n"
        f"Можно несколько разом — через пустую строку.\n\n"
        f"_Для отмены — /cancel_",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("rel:probiv:"))
async def relative_probiv_entry(callback: CallbackQuery, state: FSMContext, manager: dict):
    """Запускаем автопробив через Sauron как при создании военного"""
    await callback.answer()
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Запись не найдена.")
        return

    office = manager.get('office')
    if not office:
        await callback.message.edit_text(
            "⚠️ У вас не указан офис. Обратитесь к админу."
        )
        return

    # Очищаем сообщение с кнопками
    await callback.message.edit_text(
        f"🔍 Запускаю пробив *{military['full_name']}* через Sauron...",
        parse_mode="Markdown"
    )

    # Используем тот же механизм что и в military.py после сохранения
    from bot.handlers.probiv import run_probiv_after_save
    await run_probiv_after_save(
        callback.message,
        state,
        full_name=military['full_name'],
        birth_date=military.get('birth_date'),
        military_id=military_id,
        office=office,
    )


# ──────────── ПРИЁМ ДАННЫХ ────────────

@router.message(RelativeStates.waiting_template)
async def receive_relative_template(message: Message, state: FSMContext):
    if await is_menu_button_pressed(message, state):
        return
    """
    Принимает один или несколько блоков родственников разделённых пустыми строками.
    Обрабатывает последовательно с дубль-чеком, в конце выводит сводку.
    """
    parsed_list = parse_relatives_batch(message.text)

    # Разделяем валидные и невалидные блоки
    valid_queue = []
    invalid_count = 0
    for p in parsed_list:
        if validate_relative(p) is None:
            valid_queue.append(p)
        else:
            invalid_count += 1

    if not valid_queue:
        await message.answer(
            "⚠️ Не удалось распарсить ни одного родственника.\n"
            "Проверьте формат (нужно как минимум 'ФИО: Иванова Мария') и отправьте ещё раз, "
            "или /cancel."
        )
        return

    # Сохраняем очередь и счётчики в FSM
    await state.update_data(
        relative_queue=valid_queue,
        relative_queue_pos=0,
        batch_saved=0,
        batch_dup_saved=0,
        batch_skipped=invalid_count,
        batch_size=len(valid_queue),
    )

    # Запускаем обработку первого элемента
    await _process_next_in_queue(message, state)


async def _process_next_in_queue(target, state: FSMContext):
    """
    Берёт следующего из очереди и обрабатывает.
    Когда очередь пуста — выводит итоговую сводку.
    target — объект с методом .answer (Message или callback.message).
    """
    data = await state.get_data()
    queue = data.get("relative_queue") or []
    pos = data.get("relative_queue_pos", 0)
    manager_id = data["manager_id"]
    military_id = data["military_id"]

    # Очередь закончилась
    if pos >= len(queue):
        await _finalize_batch(target, state)
        return

    parsed = queue[pos]
    await state.update_data(relative_queue_pos=pos + 1)

    # Префикс с прогрессом если в пачке больше одного
    progress = f"[{pos + 1}/{len(queue)}] " if len(queue) > 1 else ""

    status_msg = await target.answer(f"{progress}🔍 Проверка на дубликаты...")

    # Глобальный дубль-чек (этап B1)
    # Сначала проверяем — может дубль из ЧУЖОГО офиса. Тогда сразу отказ.
    # Получаем office текущего менеджера из БД (надёжнее чем из FSM,
    # т.к. в FSM он мог не положиться при некоторых сценариях).
    from bot.db.queries import get_manager_by_id
    mgr = await get_manager_by_id(manager_id)
    my_office = (mgr or {}).get("office") if mgr else None

    global_dup = await find_relative_global_dup(
        full_name=parsed.get("full_name"),
        birth_date=parsed.get("birth_date"),
        phone=parsed.get("phone"),
        address=parsed.get("address"),
    )

    if global_dup:
        dup_office = global_dup.get("office")

        # Дубль из чужого офиса — жёсткий отказ без вариантов
        if dup_office and my_office and dup_office != my_office:
            mgr_name = global_dup.get("manager_name") or "—"
            await status_msg.edit_text(
                f"{progress}⛔ Этот родственник уже в работе у офиса "
                f"<b>{dup_office}</b> (менеджер: <b>{mgr_name}</b>).\n\n"
                f"Работа с ним запрещена.",
                parse_mode="HTML",
            )
            # Пропускаем этого, идём дальше по очереди
            data2 = await state.get_data()
            await state.update_data(batch_skipped=data2.get("batch_skipped", 0) + 1)
            await _process_next_in_queue(target, state)
            return

    # Дубля из чужого офиса нет — смотрим дубли в БД (свой офис или общая база)
    # Используем старую функцию с привязками — она нужна для UI выбора.
    duplicates = await find_relative_duplicates_with_links(
        full_name=parsed.get("full_name"),
        birth_date=parsed.get("birth_date"),
        phone=parsed.get("phone"),
        address=parsed.get("address"),
    )

    if duplicates:
        # Сохраняем текущего в state и ждём решения менеджера
        await state.update_data(parsed=parsed)
        await state.set_state(RelativeStates.waiting_confirmation)

        lines = [f"{progress}⚠️ *Найдены дубли в базе ({len(duplicates)} шт.):*\n"]
        for i, d in enumerate(duplicates, 1):
            d_birth = d.get("birth_date")
            d_birth_str = d_birth.strftime("%d.%m.%Y") if d_birth else "—"

            lines.append(f"*{i}. {d.get('full_name', '—')}* ({d_birth_str})")
            if d.get("phone"):
                lines.append(f"   📞 {d['phone']}")
            if d.get("address"):
                addr = d["address"]
                if len(addr) > 70:
                    addr = addr[:67] + "..."
                lines.append(f"   🏠 {addr}")

            if d.get("manager_name"):
                lines.append(f"   _Добавил:_ {d['manager_name']}")

            linked_to = d.get("linked_to") or []
            if linked_to:
                lines.append("   _Уже закреплён за:_")
                for m in linked_to:
                    m_birth = m.get("birth_date", "—")
                    if m_birth and m_birth != "—":
                        try:
                            m_birth = datetime.strptime(m_birth, "%Y-%m-%d").strftime("%d.%m.%Y")
                        except Exception:
                            pass
                    lines.append(
                        f"     • {m.get('full_name', '—')} "
                        f"({m_birth}, {status_label(m.get('status'))})"
                    )
            else:
                lines.append("   _Не закреплён ни за одним лидом._")
            lines.append("")

        if len(queue) > 1:
            lines.append("Сохранить этого как нового или пропустить?")
        else:
            lines.append("Всё равно сохранить как нового?")

        await status_msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=confirm_relative_kb()
        )
        return

    # Дублей нет — сохраняем тихо и идём дальше
    await status_msg.edit_text(f"{progress}✅ Дубликатов не выявлено")
    await _save_and_link(target, state, parsed, manager_id, military_id, batch_mode=True)
    await _process_next_in_queue(target, state)


async def _finalize_batch(target, state: FSMContext):
    """Финальная сводка по обработанной пачке"""
    data = await state.get_data()
    saved = data.get("batch_saved", 0)
    dup_saved = data.get("batch_dup_saved", 0)
    skipped = data.get("batch_skipped", 0)
    batch_size = data.get("batch_size", 0)
    military_id = data.get("military_id")

    # Одиночный режим без ошибок — финальное сообщение уже отправили в save_relative/_save_and_link
    if batch_size == 1 and skipped == 0:
        await state.clear()
        await target.answer(
            "Добавить ещё одного?",
            reply_markup=add_more_relatives_kb(military_id)
        )
        return

    lines = ["📊 *Итог обработки пачки:*\n"]
    lines.append(f"✅ Сохранено: {saved}")
    if dup_saved:
        lines.append(f"🔁 Сохранены при наличии дубля: {dup_saved}")
    if skipped:
        lines.append(f"⚠️ Пропущено (нет ФИО / отмена): {skipped}")

    await target.answer("\n".join(lines), parse_mode="Markdown")
    await state.clear()
    await target.answer(
        "Добавить ещё одного?",
        reply_markup=add_more_relatives_kb(military_id)
    )


# ──────────── ПОДТВЕРЖДЕНИЕ ПРИ ДУБЛЯХ ────────────

@router.callback_query(F.data == "rel:save", RelativeStates.waiting_confirmation)
async def save_relative(callback: CallbackQuery, state: FSMContext):
    """Менеджер согласился сохранить несмотря на дубль"""
    await callback.answer()
    data = await state.get_data()
    parsed = data["parsed"]
    manager_id = data["manager_id"]
    military_id = data["military_id"]

    await _save_and_link(
        callback.message, state, parsed, manager_id, military_id,
        batch_mode=True, dup_save=True,
    )
    await callback.message.edit_text(
        f"🔁 Сохранён как новый: {parsed.get('full_name', '—')}"
    )
    await _process_next_in_queue(callback.message, state)


@router.callback_query(F.data == "rel:cancel", RelativeStates.waiting_confirmation)
async def cancel_relative(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись отменена.")


# ──────────── ОБЩАЯ ЛОГИКА: сохранение + связка ────────────

async def _save_and_link(target: Message, state: FSMContext,
                         parsed: dict, manager_id: int, military_id: int,
                         batch_mode: bool = False, dup_save: bool = False):
    """
    Сохранить родственника и привязать к военному.
    batch_mode=True — обрабатывается несколько подряд, UI-сообщения подавляются,
                      финальную сводку покажет _finalize_batch.
    dup_save=True   — это сохранение по согласию менеджера несмотря на дубль.

    Перед сохранением — обогащаем телефон через voxlink (оператор/регион).
    Если они уже заполнены в parsed.extra — не перезаписываем.
    """
    parsed = await _enrich_with_voxlink(parsed)

    # v2: автоматически проставляет office из manager.office создателя
    relative = await insert_relative_v2(parsed, manager_id)
    await link_military_relative(military_id, relative['id'], manager_id)

    if batch_mode:
        # Обновляем счётчики, никакого UI здесь
        data = await state.get_data()
        if dup_save:
            await state.update_data(batch_dup_saved=data.get("batch_dup_saved", 0) + 1)
        else:
            await state.update_data(batch_saved=data.get("batch_saved", 0) + 1)
        return

    # Одиночный режим — старое поведение
    await state.clear()
    await target.answer(
        f"✅ *Сохранено и привязано*\n\n{parsed.get('full_name', '—')}",
        parse_mode="Markdown"
    )
    await target.answer(
        "Добавить ещё одного?",
        reply_markup=add_more_relatives_kb(military_id)
    )


async def _enrich_with_voxlink(parsed: dict) -> dict:
    """
    Дёрнуть voxlink по телефону, докинуть operator/region в extra.
    Не перезаписывает уже заполненные значения.
    При ошибке — тихо возвращает parsed без изменений (voxlink не критичен).
    """
    phone = parsed.get("phone")
    if not phone:
        return parsed

    extra = parsed.get("extra") or {}
    # Если оба поля уже есть — не дёргаем API
    if extra.get("operator") and extra.get("region"):
        return parsed

    try:
        from bot.services.voxlink_service import lookup_phone
        info = await lookup_phone(phone)
    except Exception:
        return parsed

    if not info:
        return parsed

    operator = info.get("operator")
    region = info.get("region")

    if operator and not extra.get("operator"):
        extra["operator"] = operator
    if region and not extra.get("region"):
        extra["region"] = region

    parsed["extra"] = extra
    return parsed


# ──────────── ЦИКЛ "Добавить ещё" ────────────

@router.callback_query(F.data.startswith("rel:more:"))
async def relative_more(callback: CallbackQuery, state: FSMContext, manager: dict):
    military_id = int(callback.data.split(":")[2])
    military = await get_military_by_id(military_id)
    if not military:
        await callback.message.edit_text("⚠️ Запись не найдена.")
        return

    await state.set_state(RelativeStates.waiting_template)
    await state.update_data(military_id=military_id, manager_id=manager['id'])

    await callback.message.edit_text(
        f"*{military['full_name']}*\n\nОтправьте следующую запись.",
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "rel:done")
async def relative_done(callback: CallbackQuery):
    await callback.message.edit_text("✅ Готово.")