import asyncio
import logging
import os

from dotenv import load_dotenv

# КРИТИЧНО: .env должен быть загружен ДО всех bot.* импортов,
# потому что модули проекта читают env-переменные на уровне модуля
# (например VOXLINK_PROXY_URL в voxlink_service.py)
load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.db.connection import get_pool, close_pool
from bot.handlers import commands, admin, military, relatives, probiv, stats, leads, export, cost
from bot.middlewares.access import AccessMiddleware
from bot.utils.logging_config import setup_logging
from bot.services.voxlink_enricher import enricher_loop
setup_logging()

logger = logging.getLogger(__name__)


async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware на все апдейты
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    # Роутеры (порядок: admin → military → relatives → commands)
    dp.include_router(admin.router)
    dp.include_router(military.router)
    dp.include_router(relatives.router)
    dp.include_router(probiv.router)
    dp.include_router(stats.router)
    dp.include_router(leads.router)
    dp.include_router(export.router)
    dp.include_router(cost.router)
    dp.include_router(commands.router)
    

    await get_pool()
    logger.info("Database pool initialized")

    # Запускаем фоновую задачу обогащения через voxlink (раз в 10 минут)
    enricher_task = asyncio.create_task(enricher_loop())
    logger.info("voxlink_enricher started in background")

    logger.info("Bot started")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        enricher_task.cancel()
        try:
            await enricher_task
        except asyncio.CancelledError:
            pass
        await close_pool()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())