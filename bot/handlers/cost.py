"""
Хендлер раздела '💰 Расход на пробив' (только для админа).

Структура:
- "⚙️ Управление ботом" → "💰 Расход на пробив"
  - 📊 Общий      — всё, успехи + неудачи, разбивка по contexts
  - 👤 По менеджерам — пагинация по 10, сортировка по убыванию $
  - 🤖 Без привязки — manager_id IS NULL (tools/скрипты)

В каждом разделе — переключатель периода: Неделя / Месяц / Всё время.
Период запоминается в FSM между переходами между разделами.
"""
from datetime import datetime, timedelta, time
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.db.queries import (
    cost_stats_total,
    cost_stats_by_manager,
    cost_stats_no_attach,
)
from bot.keyboards.menus import cost_menu_kb, cost_period_kb

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10

PERIOD_LABELS = {
    "week": "За неделю",
    "month": "За месяц",
    "all": "За всё время",
}


def _period_since(period: str):
    """Datetime начала периода или None для 'all'."""
    now = datetime.now()
    if period == "week":
        return datetime.combine((now - timedelta(days=7)).date(), time.min)
    if period == "month":
        return datetime.combine((now - timedelta(days=30)).date(), time.min)
    return None  # all


def _fmt_money(amount) -> str:
    """0.4800 → '$0.48'."""
    try:
        return f"${float(amount):.2f}"
    except Exception:
        return f"${amount}"


def _fmt_count(n: int, word: str = "запрос") -> str:
    """Простое склонение: 1 запрос / 2 запроса / 5 запросов."""
    n = int(n)
    if 10 <= (n % 100) <= 20:
        return f"{n} {word}ов"
    last = n % 10
    if last == 1:
        return f"{n} {word}"
    if 2 <= last <= 4:
        return f"{n} {word}а"
    return f"{n} {word}ов"


# ════════════════════════════════════════════════════════════
#       ВХОД: "Управление ботом" → "Расход на пробив"
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:cost")
async def open_cost_menu(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Меню раздела 'Расход на пробив'"""
    if not is_admin:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "💰 *Расход на пробив*\n\nВыберите раздел:",
        parse_mode="Markdown",
        reply_markup=cost_menu_kb(),
    )


@router.callback_query(F.data == "cost:noop")
async def cost_noop(callback: CallbackQuery):
    """Заглушка для кнопки-номера страницы"""
    await callback.answer()


# ════════════════════════════════════════════════════════════
#                       📊 ОБЩИЙ
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data == "cost:total")
async def cost_total_default(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Вход в раздел 'Общий' — берём запомненный период или week по умолчанию."""
    if not is_admin:
        return
    data = await state.get_data()
    period = data.get("cost_period", "week")
    await _render_total(callback, period, state)


@router.callback_query(F.data.startswith("cost:total:"))
async def cost_total_period(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Смена периода в разделе 'Общий'."""
    if not is_admin:
        return
    period = callback.data.split(":")[2]
    await _render_total(callback, period, state)


async def _render_total(callback: CallbackQuery, period: str, state: FSMContext):
    """Отрисовка раздела 'Общий'."""
    await callback.answer()
    await state.update_data(cost_period=period)  # запоминаем

    since = _period_since(period)
    stats = await cost_stats_total(since=since)

    period_label = PERIOD_LABELS.get(period, period)

    lines = [f"📊 *{period_label} — Общий расход*", ""]
    lines.append(f"Всего: {_fmt_count(stats['total_count'])} | {_fmt_money(stats['total_cost'])}")
    lines.append("")
    lines.append("*По контексту:*")
    lines.append(f"  • auto (автопробив): {stats['auto_count']} | {_fmt_money(stats['auto_cost'])}")
    lines.append(f"  • next (Пробить далее): {stats['next_count']} | {_fmt_money(stats['next_cost'])}")
    lines.append(f"  • tool (скрипты): {stats['tool_count']} | {_fmt_money(stats['tool_cost'])}")

    if stats["failed_count"]:
        lines.append("")
        lines.append(
            f"⚠️ Неудачных: {stats['failed_count']} | {_fmt_money(stats['failed_cost'])}"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=cost_period_kb(section="total", period=period),
    )


# ════════════════════════════════════════════════════════════
#                  👤 ПО МЕНЕДЖЕРАМ
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data == "cost:by_mgr")
async def cost_by_mgr_default(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Вход в раздел 'По менеджерам' — запомненный период, страница 0."""
    if not is_admin:
        return
    data = await state.get_data()
    period = data.get("cost_period", "week")
    await _render_by_mgr(callback, period, page=0, state=state)


@router.callback_query(F.data.startswith("cost:by_mgr:"))
async def cost_by_mgr_route(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """
    Универсальный роутер для 'По менеджерам':
    - cost:by_mgr:{period}:{page}
    """
    if not is_admin:
        return
    parts = callback.data.split(":")
    # parts = ['cost', 'by_mgr', period, page]
    period = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    await _render_by_mgr(callback, period, page=page, state=state)


async def _render_by_mgr(callback: CallbackQuery, period: str, page: int, state: FSMContext):
    """Отрисовка раздела 'По менеджерам' с пагинацией."""
    await callback.answer()
    await state.update_data(cost_period=period)

    since = _period_since(period)
    rows = await cost_stats_by_manager(since=since)

    period_label = PERIOD_LABELS.get(period, period)

    if not rows:
        await callback.message.edit_text(
            f"👤 *{period_label} — По менеджерам*\n\n_Нет активности._",
            parse_mode="Markdown",
            reply_markup=cost_period_kb(section="by_mgr", period=period),
        )
        return

    # Считаем общий итог по менеджерам (для шапки)
    total_count = sum(r["total_count"] for r in rows)
    total_cost = sum(float(r["total_cost"]) for r in rows)

    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_rows = rows[start:end]

    lines = [f"👤 *{period_label} — По менеджерам*"]
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}, всего менеджеров: {len(rows)}_")
    lines.append("")

    # Сначала ВСЕГДА общий итог (шапка)
    lines.append(f"*Итого: {_fmt_count(total_count)} | {_fmt_money(total_cost)}*")
    lines.append("─────────────")
    lines.append("")

    for r in page_rows:
        name = r["name"]
        if not r["is_active"]:
            name += " *(не активен)*"
        lines.append(
            f"*{name}*\n"
            f"  {_fmt_count(r['total_count'])} "
            f"(auto: {r['auto_count']} / next: {r['next_count']}) | "
            f"{_fmt_money(r['total_cost'])}"
        )
        lines.append("")

    await callback.message.edit_text(
        "\n".join(lines).rstrip(),
        parse_mode="Markdown",
        reply_markup=cost_period_kb(
            section="by_mgr", period=period, page=page, total_pages=total_pages
        ),
    )


# ════════════════════════════════════════════════════════════
#                  🤖 БЕЗ ПРИВЯЗКИ
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data == "cost:noattach")
async def cost_noattach_default(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Вход в раздел 'Без привязки'."""
    if not is_admin:
        return
    data = await state.get_data()
    period = data.get("cost_period", "week")
    await _render_noattach(callback, period, state)


@router.callback_query(F.data.startswith("cost:noattach:"))
async def cost_noattach_period(callback: CallbackQuery, is_admin: bool, state: FSMContext):
    """Смена периода в разделе 'Без привязки'."""
    if not is_admin:
        return
    period = callback.data.split(":")[2]
    await _render_noattach(callback, period, state)


async def _render_noattach(callback: CallbackQuery, period: str, state: FSMContext):
    """Отрисовка раздела 'Без привязки к менеджеру'."""
    await callback.answer()
    await state.update_data(cost_period=period)

    since = _period_since(period)
    stats = await cost_stats_no_attach(since=since)

    period_label = PERIOD_LABELS.get(period, period)

    lines = [f"🤖 *{period_label} — Без привязки к менеджеру*", ""]

    if stats["total_count"] == 0:
        lines.append("_Запросов без привязки нет._")
        lines.append("")
        lines.append("Сюда попадают запуски скриптов из tools/")
        lines.append("(те что без привязки к менеджеру).")
    else:
        lines.append(f"Всего: {_fmt_count(stats['total_count'])} | {_fmt_money(stats['total_cost'])}")

        if stats["failed_count"]:
            lines.append("")
            lines.append(
                f"⚠️ Неудачных: {stats['failed_count']} | {_fmt_money(stats['failed_cost'])}"
            )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=cost_period_kb(section="noattach", period=period),
    )
