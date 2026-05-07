import os
from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from dotenv import load_dotenv

from bot.db.queries import get_manager_by_telegram_id

load_dotenv()


def get_admin_ids() -> set[int]:
    """Получить ID админов из .env"""
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


class AccessMiddleware(BaseMiddleware):
    """
    Проверяет доступ к боту:
    - Админы (из ADMIN_IDS) — полный доступ
    - Менеджеры (из БД) — только функции внесения данных
    - Все остальные — отказ
    
    Прокидывает в data:
        is_admin: bool
        manager: dict | None
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        # Достаём пользователя
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        admin_ids = get_admin_ids()
        is_admin = user.id in admin_ids
        manager = await get_manager_by_telegram_id(user.id)

        # Доступа нет — отказ
        if not is_admin and not manager:
            text = (
                "🚫 У вас нет доступа к боту.\n"
                "Обратитесь к администратору."
            )
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return

        # Прокидываем в хендлеры
        data["is_admin"] = is_admin
        data["manager"] = manager
        data["admin_ids"] = admin_ids

        return await handler(event, data)
