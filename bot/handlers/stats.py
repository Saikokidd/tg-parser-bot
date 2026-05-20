"""
Хендлер статистики.
Менеджер: видит только свою статистику.
Админ/Пульт: видит список всех менеджеров с разбивкой.
  - Скрываем менеджеров с 0 лидов в выбранном периоде
  - Пагинация по 10 на страницу
  - Сортировка: по убыванию loaded (топ-менеджеры сверху)
Периоды: сегодня / неделя / всё время.
"""
from datetime import datetime, timedelta, time
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from bot.db.queries import stats_for_manager, stats_for_all_managers
from bot.keyboards.menus import stats_period_kb

router = Router()

PAGE_SIZE = 10

PERIOD_LABELS = {
    "today": "За сегодня",
    "week": "За неделю",
    "all": "За всё время",
}


def _period_since(period: str):
    """Вернуть datetime начала периода или None для 'all'"""
    now = datetime.now()
    if period == "today":
        return datetime.combine(now.date(), time.min)
    if period == "week":
        return datetime.combine((now - timedelta(days=7)).date(), time.min)
    return None  # all


def _format_admin_stats(rows: list, period: str, page: int) -> tuple[str, int]:
    """
    Сформировать текст сводки для админа/пульта.
    Возвращает (текст, общее число страниц).
    """
    label = PERIOD_LABELS.get(period, period)

    # Считаем общие итоги ДО фильтрации — чтобы видеть полную картину
    total_loaded = sum(r['loaded'] for r in rows)
    total_filled = sum(r['filled'] for r in rows)

    # Фильтруем — оставляем только тех у кого есть активность
    active_rows = [r for r in rows if r['loaded'] > 0]

    if not active_rows:
        return f"*{label}*\n\n_Нет активности за выбранный период._", 1

    total_pages = max(1, (len(active_rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_rows = active_rows[start:end]

    lines = [f"*{label}*"]
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}, активных менеджеров: {len(active_rows)}_")
    lines.append("")

    for r in page_rows:
        lines.append(
            f"*{r['name']}*\n"
            f"  Загружено: {r['loaded']} | Заполнено: {r['filled']}\n"
        )

    lines.append("─────────────")
    lines.append(f"*Всего: {total_loaded} | Заполнено: {total_filled}*")

    return "\n".join(lines), total_pages


# ──────────── Вход по кнопке ────────────

@router.message(F.text == "📊 Статистика")
async def btn_stats(message: Message, manager: dict | None,
                    is_admin: bool, is_supervisor: bool):
    if not manager and not is_admin and not is_supervisor:
        await message.answer("Доступа нет.")
        return
    await message.answer(
        "Выберите период:",
        reply_markup=stats_period_kb()
    )


# ──────────── Обработка выбора периода ────────────

@router.callback_query(F.data.startswith("stats:page:"))
async def show_stats_page(callback: CallbackQuery, manager: dict | None,
                           is_admin: bool, is_supervisor: bool):
    """Переключение страницы пагинации"""
    parts = callback.data.split(":")
    # формат: stats:page:<period>:<page>
    period = parts[2]
    page = int(parts[3])
    await callback.answer()
    await _render_admin_stats(callback, period, page, is_admin, is_supervisor)


@router.callback_query(F.data == "stats:noop")
async def stats_noop(callback: CallbackQuery):
    """Заглушка для кнопки-номера страницы"""
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def show_stats(callback: CallbackQuery, manager: dict | None,
                     is_admin: bool, is_supervisor: bool):
    """Выбор периода"""
    period = callback.data.split(":")[1]
    await callback.answer()

    if is_admin or is_supervisor:
        await _render_admin_stats(callback, period, 0, is_admin, is_supervisor)
    else:
        # Своя статистика
        since = _period_since(period)
        label = PERIOD_LABELS.get(period, period)
        s = await stats_for_manager(manager['id'], since=since)
        await callback.message.edit_text(
            f"*{label}*\n\n"
            f"*{manager['name']}*\n"
            f"Загружено: {s['loaded']} | Заполнено: {s['filled']}",
            parse_mode="Markdown",
            reply_markup=stats_period_kb(period=period)
        )


async def _render_admin_stats(callback: CallbackQuery, period: str, page: int,
                               is_admin: bool, is_supervisor: bool):
    """Отрисовка статистики для админа/пульта с пагинацией"""
    since = _period_since(period)
    rows = await stats_for_all_managers(since=since)

    text, total_pages = _format_admin_stats(rows, period, page)
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=stats_period_kb(period=period, page=page, total_pages=total_pages)
    )