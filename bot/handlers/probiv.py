"""
Хендлер пробива через внешние API (Sauron + future providers).

Флоу:
1. Сразу после сохранения военного — автоматически запускается пробив.
   Показываем "Возможные связи по адресу" + кнопки "Пробить" по каждому найденному.

2. Менеджер жмёт "🔍 [имя]" → новый запрос к API по этому человеку →
   возвращаем заполненный шаблон родственника (с самыми частыми значениями).
   Менеджер копирует шаблон и через "✍️ Заполнить родственников" вносит в БД.
"""
import logging
from datetime import date
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.services.probiv_service import (
    probit_person, format_probiv_result, ProbivResult
)
from bot.parser.sauron_parser import format_relative_template
from bot.keyboards.menus import probiv_persons_kb

logger = logging.getLogger(__name__)
router = Router()


# ════════════════════════════════════════════════════════════
#  Шаг 1: автоматический пробив сразу после сохранения военного
# ════════════════════════════════════════════════════════════

async def run_probiv_after_save(
    message_or_callback,
    state: FSMContext,
    full_name: str,
    birth_date: date | None
):
    """
    Вызывается из military.py после успешного сохранения военного.
    Делает пробив и кладёт результат в FSM (для последующих "Пробить далее").
    """
    target = (
        message_or_callback.message
        if isinstance(message_or_callback, CallbackQuery)
        else message_or_callback
    )

    # Сообщаем что начали пробив
    status_msg = await target.answer("⏳ Делаю пробив через внешние API...")

    try:
        result: ProbivResult = await probit_person(full_name, birth_date)
    except ValueError as e:
        await status_msg.edit_text(f"⚠️ Пробив не запущен: {e}")
        return
    except Exception as e:
        logger.exception("Ошибка пробива")
        await status_msg.edit_text(f"❌ Ошибка пробива: {e}")
        return

    # Если все провайдеры упали — показываем ошибки и выходим
    if not result.raw_results:
        await status_msg.edit_text(format_probiv_result(result, header="🔍 *Пробив*"))
        return

    # Сохраняем найденных людей в FSM для кнопок "Пробить далее"
    persons_index = []
    seen = set()
    for block in result.address_relations:
        for p in block["persons"]:
            key = f"{p['full_name']}|{p['birth_date_str']}"
            if key in seen:
                continue
            seen.add(key)
            persons_index.append(p)

    await state.update_data(probiv_persons=persons_index)

    # Показываем результат
    text = format_probiv_result(result, header="🔍 *Пробив выполнен*")
    if result.address_relations:
        await status_msg.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=probiv_persons_kb(result.address_relations)
        )
        await target.answer(
            "ℹ️ Нажмите на любого найденного человека чтобы пробить его и "
            "получить шаблон родственника."
        )
    else:
        await status_msg.edit_text(text, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
#  Шаг 2: пробив выбранного человека → шаблон родственника
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("probiv:next:"))
async def probiv_next(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[2])
    data = await state.get_data()
    persons = data.get("probiv_persons", [])

    if idx >= len(persons):
        await callback.answer("⚠️ Запись не найдена.", show_alert=True)
        return

    person = persons[idx]
    full_name = person["full_name"]
    birth_str = person["birth_date_str"]  # 'ДД.ММ.ГГГГ' или ''

    # Парсим дату обратно в date
    birth_date = None
    if birth_str:
        try:
            d, m, y = birth_str.split(".")
            birth_date = date(int(y), int(m), int(d))
        except Exception:
            birth_date = None

    await callback.answer()
    status_msg = await callback.message.answer(
        f"⏳ Пробиваю *{full_name}*...",
        parse_mode="Markdown"
    )

    try:
        result: ProbivResult = await probit_person(full_name, birth_date)
    except Exception as e:
        logger.exception("Ошибка пробива (next)")
        await status_msg.edit_text(f"❌ Ошибка пробива: {e}")
        return

    if not result.raw_results:
        await status_msg.edit_text(format_probiv_result(result, header="🔍 *Пробив*"))
        return

    # Шаблон родственника
    if result.relative_template:
        cost_line = f"💰 Стоимость: *{result.total_cost:.2f} ₽*\n\n"
        await status_msg.edit_text(
            cost_line + format_relative_template(result.relative_template),
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"💰 Стоимость: {result.total_cost:.2f} ₽\n\n"
            f"❌ Не удалось собрать шаблон родственника из ответа API."
        )


@router.callback_query(F.data == "probiv:done")
async def probiv_done(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Готово")
