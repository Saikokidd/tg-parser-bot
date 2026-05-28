"""
Глобальный обработчик ошибок aiogram.

Цель — не засорять error.log "шумом" от нестабильной сети до Telegram
и устаревших callback-запросов. Эти ошибки происходят регулярно
(несколько раз в час) и НЕ требуют действий от разработчика:

- TelegramNetworkError: Request timeout — сетевой таймаут между ботом
  и серверами Telegram. aiogram сам делает retry. К коду отношения нет.

- TelegramBadRequest: query is too old — callback-кнопка ответили
  слишком долго (>15 сек). Часто следствие сетевых задержек выше.
  Менеджер уже не увидит ответа, но действие на нашей стороне выполнено.

Все остальные ошибки пропускаем дальше — пусть логируются как обычно.
"""
import logging

from aiogram import Router
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.types.error_event import ErrorEvent

router = Router(name="errors")
logger = logging.getLogger(__name__)


@router.errors()
async def global_error_handler(event: ErrorEvent) -> bool:
    """
    Возвращает True если ошибка обработана (aiogram не будет писать её в ERROR).
    False — если нужно пропустить дальше (тогда aiogram залогирует в ERROR).
    """
    exc = event.exception

    # Сетевые таймауты до Telegram — не наша проблема
    if isinstance(exc, TelegramNetworkError):
        logger.warning("Telegram network timeout (ignored): %s", exc)
        return True

    # Устаревшие callback queries — пользователь уже не увидит ответ,
    # но это не баг бота
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if "query is too old" in msg or "query id is invalid" in msg:
            logger.warning("Stale callback query (ignored): %s", exc)
            return True

    # Все остальные ошибки — пропускаем, пусть aiogram залогирует
    return False
