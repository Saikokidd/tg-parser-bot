import os
from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from dotenv import load_dotenv

from bot.db.queries import get_manager_by_telegram_id

load_dotenv()


def _parse_ids(env_var: str) -> set[int]:
    raw = os.getenv(env_var, "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def get_admin_ids() -> set[int]:
    return _parse_ids("ADMIN_IDS")


def get_supervisor_ids() -> set[int]:
    return _parse_ids("SUPERVISOR_IDS")


class AccessMiddleware(BaseMiddleware):
    """
    Проверяет доступ к боту и определяет роль:
    - Админы (из ADMIN_IDS) — полный доступ
    - Пульт (из SUPERVISOR_IDS) — только статистика
    - Менеджеры (из БД) — внесение данных + статистика
    - Все остальные — отказ

    Прокидывает в data:
        is_admin: bool
        is_supervisor: bool
        manager: dict | None
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        admin_ids = get_admin_ids()
        supervisor_ids = get_supervisor_ids()

        is_admin = user.id in admin_ids
        is_supervisor = user.id in supervisor_ids and not is_admin

        # Менеджера ищем всегда — админ может быть одновременно менеджером.
        # Пульт не может быть менеджером (это разные роли).
        if is_supervisor:
            manager = None
        else:
            manager = await get_manager_by_telegram_id(user.id)

        # Доступа нет — отказ
        if not is_admin and not is_supervisor and not manager:
            text = "У вас нет доступа к боту."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return

        data["is_admin"] = is_admin
        data["is_supervisor"] = is_supervisor
        data["manager"] = manager

        return await handler(event, data)