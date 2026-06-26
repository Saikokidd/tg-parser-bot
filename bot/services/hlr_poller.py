"""
Фоновая задача: опрос статусов HLR-проверок в smsaero.

Раз в N минут:
  1. Берёт записи relative_phones где hlr_request_id IS NOT NULL
     AND hlr_status IN ('pending', 'in_work')
  2. Для каждой запрашивает /hlr/status в smsaero
  3. Обновляет hlr_status на финальное значение
     (available / unavailable / not_exists / in_work — остаётся опрашивать)

Финальные статусы (не опрашиваются больше):
  available, unavailable, not_exists, error, skipped_operator
"""
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from bot.db.queries import (
    phones_pending_hlr_poll,
    update_phone_hlr_status,
    mark_phone_hlr_error,
)
from bot.services.smsaero_service import (
    get_hlr_status, SmsaeroTransientError, SmsaeroError,
)


# ──────────── Настройки ────────────

INTERVAL_SECONDS = 60          # раз в минуту
BATCH_SIZE = 200               # за один прогон опрашиваем до N pending
START_DELAY_SECONDS = 120      # после старта бота — пауза побольше чем у enricher

# Параллельность опросов: smsaero выдерживает 10 req/sec без проблем
PARALLEL = 5


# ──────────── Логгер ────────────

logger = logging.getLogger("hlr_poller")
logger.setLevel(logging.INFO)
logger.propagate = False

_log_path = Path(__file__).resolve().parent.parent.parent / "logs" / "hlr_poller.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)

if not logger.handlers:
    _handler = TimedRotatingFileHandler(
        _log_path, when="D", interval=1, backupCount=14, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(_handler)


# ──────────── Один опрос ────────────

# Сколько максимум держим запрос в in_work — после этого помечаем как error
STUCK_THRESHOLD_HOURS = 48


async def _poll_one(rp: dict, stats: dict, sem: asyncio.Semaphore):
    """Опросить статус одного запроса."""
    async with sem:
        phone_id = rp["id"]
        req_id = rp["hlr_request_id"]
        created_at = rp.get("created_at")
        try:
            status = await get_hlr_status(req_id)
        except SmsaeroTransientError as e:
            stats["transient"] = stats.get("transient", 0) + 1
            return
        except SmsaeroError as e:
            logger.warning(f"[#{phone_id}] req_id={req_id}: error {e}")
            await mark_phone_hlr_error(phone_id)
            stats["errors"] = stats.get("errors", 0) + 1
            return
        except Exception as e:
            logger.exception(f"[#{phone_id}] req_id={req_id}: unexpected: {e}")
            stats["errors"] = stats.get("errors", 0) + 1
            return

        if status is None:
            # smsaero вернул что-то неожиданное — пометим error
            await mark_phone_hlr_error(phone_id)
            stats["errors"] = stats.get("errors", 0) + 1
            return

        # Проверка зависания: если запрос висит > 48 часов и smsaero
        # всё ещё отвечает 'in_work' — помечаем как error, не будем мучить
        if status == "in_work" and created_at:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(created_at.tzinfo) if created_at.tzinfo else datetime.now()
            age = now - created_at
            if age > timedelta(hours=STUCK_THRESHOLD_HOURS):
                logger.warning(
                    f"[#{phone_id}] req_id={req_id}: stuck in_work "
                    f"for {age}, marking as error"
                )
                await mark_phone_hlr_error(phone_id)
                stats["stuck_marked_error"] = stats.get("stuck_marked_error", 0) + 1
                return

        # Обновляем
        await update_phone_hlr_status(phone_id, status)
        if status == "in_work":
            stats["still_in_work"] = stats.get("still_in_work", 0) + 1
        else:
            stats["finalized"] = stats.get("finalized", 0) + 1
            stats[f"final_{status}"] = stats.get(f"final_{status}", 0) + 1


async def run_once() -> dict:
    """Один прогон опроса."""
    candidates = await phones_pending_hlr_poll(limit=BATCH_SIZE)
    if not candidates:
        return {"candidates": 0}

    stats = {"candidates": len(candidates)}
    sem = asyncio.Semaphore(PARALLEL)
    await asyncio.gather(*[_poll_one(rp, stats, sem) for rp in candidates])
    return stats


# ──────────── Бесконечный цикл ────────────

async def poller_loop():
    """Запускается из main.py как фоновая задача."""
    logger.info(f"hlr_poller запущен, интервал={INTERVAL_SECONDS}s")
    await asyncio.sleep(START_DELAY_SECONDS)

    while True:
        try:
            stats = await run_once()
            if stats.get("candidates", 0) > 0:
                # Собираем чёткое сообщение со всеми финальными статусами
                parts = [
                    f"candidates={stats['candidates']}",
                    f"finalized={stats.get('finalized', 0)}",
                    f"still_in_work={stats.get('still_in_work', 0)}",
                ]
                for key in ("final_available", "final_unavailable",
                            "final_not_exists"):
                    val = stats.get(key, 0)
                    if val:
                        parts.append(f"{key}={val}")
                if stats.get("transient"):
                    parts.append(f"transient={stats['transient']}")
                if stats.get("errors"):
                    parts.append(f"errors={stats['errors']}")
                logger.info(f"Прогон: {', '.join(parts)}")
        except asyncio.CancelledError:
            logger.info("hlr_poller остановлен")
            raise
        except Exception as e:
            logger.exception(f"Ошибка в цикле: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)
