"""
Хендлер выгрузки лидов в .xlsx.

Флоу:
- Менеджер жмёт "📤 Выгрузить лидов"
- Бот показывает сколько доступно (с заполненными родственниками, не выгруженных)
- Менеджер выбирает: "Выгрузить всё" или указать число
- Бот формирует .xlsx, отправляет, помечает лиды как выгруженные

Видимость:
- Менеджер → выгружает только своих лидов
- Админ → выгружает ВСЕХ менеджеров (для теста)
- Пульт → нет доступа
"""
import asyncio
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.db.queries import (
    count_available_for_export,
    fetch_military_for_export,
    fetch_relatives_for_military_ids,
    mark_military_exported,
)
from bot.services.export_service import build_xlsx, make_filename
from bot.keyboards.menus import export_count_kb

logger = logging.getLogger(__name__)
router = Router()


# ──────────── FSM ────────────

class ExportStates(StatesGroup):
    waiting_count = State()


# ──────────── /cancel ────────────

@router.message(Command("cancel"), ExportStates.waiting_count)
async def cancel_export(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Выгрузка отменена.")


# ──────────── ВХОД ПО КНОПКЕ ────────────

@router.message(F.text == "📤 Выгрузить лидов")
async def btn_export(message: Message, manager: dict | None,
                     is_admin: bool, is_supervisor: bool):
    if is_supervisor:
        await message.answer("Выгрузка доступна только менеджерам.")
        return
    if not manager and not is_admin:
        await message.answer("Доступа нет.")
        return

    # Админ видит ВСЕ записи. Менеджер — только свои.
    manager_id_filter = None if is_admin else manager['id']
    available = await count_available_for_export(manager_id=manager_id_filter)

    if available == 0:
        await message.answer(
            "📭 Нет лидов доступных для выгрузки.\n\n"
            "_Доступны только лиды с заполненными родственниками "
            "и которых ещё не выгружали._",
            parse_mode="Markdown"
        )
        return

    label = "по всем менеджерам" if is_admin else "из вашей базы"
    await message.answer(
        f"📤 *Выгрузка лидов*\n\n"
        f"Доступно {label}: *{available}*\n\n"
        f"Выберите действие:",
        parse_mode="Markdown",
        reply_markup=export_count_kb(available)
    )


# ──────────── Выгрузка всего ────────────

@router.callback_query(F.data == "export:all")
async def export_all(callback: CallbackQuery, manager: dict | None, is_admin: bool):
    await callback.answer()
    await _do_export(callback.message, manager, is_admin, limit=None)


# ──────────── Запрос количества ────────────

@router.callback_query(F.data == "export:custom")
async def export_custom_ask(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ExportStates.waiting_count)
    await callback.message.edit_text(
        "Введите *количество* лидов которые хотите выгрузить (число).\n\n"
        "_Для отмены — /cancel_",
        parse_mode="Markdown"
    )


@router.message(ExportStates.waiting_count)
async def export_custom_receive(message: Message, state: FSMContext,
                                 manager: dict | None, is_admin: bool):
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("⚠️ Нужно ввести число. Попробуйте ещё раз или /cancel.")
        return

    limit = int(raw)
    if limit <= 0:
        await message.answer("⚠️ Число должно быть больше нуля. Попробуйте ещё раз или /cancel.")
        return

    await state.clear()
    await _do_export(message, manager, is_admin, limit=limit)


# ──────────── Отмена ────────────

@router.callback_query(F.data == "export:cancel")
async def export_cancel_cb(callback: CallbackQuery):
    await callback.message.edit_text("❌ Выгрузка отменена.")
    await callback.answer()


# ──────────── ОБЩАЯ ЛОГИКА ВЫГРУЗКИ ────────────

_export_locks: dict = {}


async def _do_export(target: Message, manager: dict | None,
                     is_admin: bool, limit: int | None):
    """
    Одна выгрузка на менеджера за раз.

    Раньше защитой от повторной выдачи тех же лидов служила пометка
    «выгружено» до отправки файла. Это и привело к потере двух партий
    Леси 18.11.2026: пометка встала, отправка не прошла, байты пропали.
    Теперь порядок правильный, а от двойного нажатия защищает замок.
    """
    key = manager["id"] if manager else 0
    lock = _export_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        await target.answer("\u23f3 Предыдущая выгрузка ещё готовится, подождите её.")
        return
    async with lock:
        await _do_export_inner(target, manager, is_admin, limit)


async def _do_export_inner(target: Message, manager: dict | None, is_admin: bool, limit: int | None):
    """
    Выгрузить лиды в .xlsx и пометить их.
    """
    manager_id_filter = None if is_admin else manager['id']
    label = None if is_admin else manager['name']

    status_msg = await target.answer("⏳ Готовлю файл...")

    # Получаем военных и родственников
    military_records = await fetch_military_for_export(
        manager_id=manager_id_filter, limit=limit
    )
    if not military_records:
        await status_msg.edit_text("📭 Нет данных для выгрузки.")
        return

    military_ids = [m['id'] for m in military_records]
    relatives = await fetch_relatives_for_military_ids(military_ids)

    # Генерируем файл
    try:
        # Генерация xlsx синхронная (openpyxl) и на больших выгрузках занимает
        # десятки секунд — уносим в отдельный поток, иначе блокируется весь
        # event loop и у остальных менеджеров бот «висит».
        xlsx_bytes = await asyncio.to_thread(
            build_xlsx, military_records, relatives, manager_label=label
        )
    except Exception as e:
        logger.exception("Ошибка генерации xlsx")
        await status_msg.edit_text(f"❌ Ошибка при генерации файла: {e}")
        return

    filename = make_filename(label)

    # Копия на диск до отправки: если доставка не пройдёт, файл можно
    # отдать руками, не пересобирая из базы.
    try:
        with open("/root/exports/" + filename, "wb") as fh:
            fh.write(xlsx_bytes)
    except Exception:
        logger.exception("не удалось сохранить копию выгрузки")

    try:
        await status_msg.delete()
    except Exception:
        pass

    # Сначала отправляем. Пометка — только после успешной отправки,
    # иначе при сбое лиды списаны, а файла у менеджера нет.
    # Telegram периодически отвечает 500 на sendDocument. Сбой короткий,
    # поэтому три попытки с нарастающей паузой снимают почти все случаи.
    err = None
    for attempt in (1, 2, 3):
        try:
            await target.answer_document(
                BufferedInputFile(xlsx_bytes, filename=filename),
                caption=(
                    f"\u2705 Выгружено: *{len(military_records)}* лидов\n"
                    f"Родственников в файле: *{len(relatives)}*"
                ),
                parse_mode="Markdown"
            )
            err = None
            break
        except Exception as e:
            err = e
            logger.warning("выгрузка, попытка %d из 3 не прошла: %s", attempt, e)
            if attempt < 3:
                await asyncio.sleep(3 * attempt)

    if err is not None:
        e = err
        logger.exception("Не удалось отправить файл выгрузки")
        await target.answer(
            "\u274c Файл не отправился, лиды остались доступными для выгрузки.\n"
            f"Копия лежит на сервере: {filename}\n\n{e}"
        )
        return

    if not is_admin:
        await mark_military_exported(military_ids)

    logger.info("export: менеджер=%s лидов=%d родни=%d файл=%s",
                label or "админ", len(military_records), len(relatives), filename)
