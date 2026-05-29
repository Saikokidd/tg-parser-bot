import os
import logging
from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from dotenv import load_dotenv

from bot.db.queries import get_manager_by_telegram_id

load_dotenv()
logger = logging.getLogger(__name__)


# Известные офисы. Расширяется добавлением кода в этот список.
KNOWN_OFFICES = ("pvl", "dp", "ha")


def _parse_ids(env_var: str) -> set[int]:
    raw = os.getenv(env_var, "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def get_super_admin_ids() -> set[int]:
    """Супер-админы — видят всё, не привязаны к офису."""
    return _parse_ids("SUPER_ADMIN_IDS")


def get_supervisor_ids_by_office() -> dict[str, set[int]]:
    """Пульты по офисам. Ключ — код офиса, значение — set telegram_id."""
    return {
        office: _parse_ids(f"SUPERVISOR_{office.upper()}_IDS")
        for office in KNOWN_OFFICES
    }


# ──────────── Обратная совместимость со старыми env ────────────
# Чтобы не сломать ничего что было — поддерживаем старые ADMIN_IDS как
# алиас для SUPER_ADMIN_IDS, и старый SUPERVISOR_IDS как глобальный пульт
# (видит все офисы; пока существует, помечаем людей как office_supervisor
# с office=None — они увидят сводную статистику как раньше).
def _legacy_admin_ids() -> set[int]:
    return _parse_ids("ADMIN_IDS")


def _legacy_supervisor_ids() -> set[int]:
    return _parse_ids("SUPERVISOR_IDS")


class AccessMiddleware(BaseMiddleware):
    """
    Определяет роль и офис пользователя.

    Кладёт в data следующие ключи:
        role: str | None — 'super_admin' / 'office_admin' / 'office_supervisor' / 'manager' / None
        office: str | None — код офиса (для всех кроме super_admin)
        manager: dict | None — запись из БД, если пользователь — менеджер/админ
        is_admin: bool — backward compat: True для super_admin и office_admin
        is_supervisor: bool — backward compat: True для office_supervisor
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        role, office, manager = await self._resolve(user.id)

        # Доступа нет — отказ
        if role is None:
            text = "У вас нет доступа к боту."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return

        # Backward compat: старые хендлеры используют is_admin/is_supervisor.
        # Пусть продолжают работать до полного рефакторинга.
        is_admin = role in ("super_admin", "office_admin")
        is_supervisor = role == "office_supervisor"

        data["role"] = role
        data["office"] = office
        data["manager"] = manager
        data["is_admin"] = is_admin
        data["is_supervisor"] = is_supervisor

        return await handler(event, data)

    async def _resolve(
        self, telegram_id: int
    ) -> tuple[str | None, str | None, dict | None]:
        """
        Определить (role, office, manager) для telegram_id.
        Возвращает (None, None, None) если доступа нет.
        """
        # 1. Супер-админ (включая legacy ADMIN_IDS как алиас)
        if telegram_id in get_super_admin_ids() or telegram_id in _legacy_admin_ids():
            # Если он одновременно менеджер в БД — подгружаем запись,
            # чтобы он мог использовать функционал менеджера (свой office
            # при пробивах, своя статистика и т.д. — но при этом не теряет
            # супер-админских прав).
            manager_record = await get_manager_by_telegram_id(telegram_id)
            return "super_admin", None, manager_record

        # 2. Пульт офиса (по новому SUPERVISOR_<OFFICE>_IDS)
        for office, ids in get_supervisor_ids_by_office().items():
            if telegram_id in ids:
                return "office_supervisor", office, None

        # 3. Legacy SUPERVISOR_IDS — глобальный пульт без офиса
        if telegram_id in _legacy_supervisor_ids():
            return "office_supervisor", None, None

        # 4. Менеджер или офис-админ из БД
        manager_record = await get_manager_by_telegram_id(telegram_id)
        if manager_record:
            role_in_db = manager_record.get("role") or "manager"
            office_in_db = manager_record.get("office")
            if role_in_db == "admin":
                return "office_admin", office_in_db, manager_record
            return "manager", office_in_db, manager_record

        # 5. Никто
        return None, None, None