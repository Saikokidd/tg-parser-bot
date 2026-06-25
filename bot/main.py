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
from bot.handlers import commands, admin, military, relatives, probiv, stats, leads, export, cost, search, sources, errors
from bot.api_server import start_api_server
from bot.middlewares.access import AccessMiddleware
from bot.utils.logging_config import setup_logging
from bot.services.voxlink_enricher import enricher_loop as voxlink_enricher_loop
from bot.services.hlr_enricher import enricher_loop as hlr_enricher_loop
from bot.services.hlr_poller import poller_loop as hlr_poller_loop
from bot.api_server import start_api_server

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
    dp.include_router(search.router)
    dp.include_router(sources.router)
    dp.include_router(commands.router)
    # Глобальный обработчик ошибок — должен быть последним,
    # чтобы поймать ошибки из всех остальных роутеров.
    dp.include_router(errors.router)
    

    await get_pool()
    logger.info("Database pool initialized")

    # Запускаем фоновую задачу обогащения через voxlink (раз в 10 минут)
    voxlink_task = asyncio.create_task(voxlink_enricher_loop())
    logger.info("voxlink_enricher started in background")

    hlr_enricher_task = asyncio.create_task(hlr_enricher_loop())
    logger.info("hlr_enricher started in background")

    hlr_poller_task = asyncio.create_task(hlr_poller_loop())
    logger.info("hlr_poller started in background")

    # HTTP API сервер для внешних агентов (например ha CRM)
    api_runner = None
    api_port_env = os.getenv("API_PORT")
    if api_port_env:
        try:
            api_runner = await start_api_server(int(api_port_env))
        except Exception:
            logger.exception("api: failed to start, продолжаем без HTTP API")

    logger.info("Bot started")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if api_runner is not None:
            await api_runner.cleanup()
            logger.info("api: stopped")
        for task in (voxlink_task, hlr_enricher_task, hlr_poller_task):
            task.cancel()
        for task in (voxlink_task, hlr_enricher_task, hlr_poller_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_pool()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())