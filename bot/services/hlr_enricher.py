"""
Фоновая задача: отправка номеров на HLR-проверку через smsaero.

Раз в N минут:
  1. Берёт номера из relative_phones где:
       - operator определён
       - operator не в скип-листе (Мегафон, Йота)
       - relative.office='pvl'
       - hlr_status IS NULL
     (фильтр уже в phones_pending_hlr)
  2. Отправляет пачкой (до 50 за раз) в smsaero
  3. Сохраняет request_id и hlr_status='pending'

Опрос результатов делает отдельный воркер hlr_poller.py.
"""
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from bot.db.queries import (
    phones_pending_hlr, update_phone_hlr_request, mark_phone_hlr_error,
)
from bot.services.smsaero_service import (
    send_hlr_batch, SmsaeroTransientError, SmsaeroError,
)


# ──────────── Настройки ────────────

INTERVAL_SECONDS = 60          # раз в минуту
BATCH_SIZE = 50                # smsaero принимает несколько номеров за раз
START_DELAY_SECONDS = 90       # пауза после старта бота


# ──────────── Логгер с ротацией в свой файл ────────────

logger = logging.getLogger("hlr_enricher")
logger.setLevel(logging.INFO)
logger.propagate = False

_log_path = Path(__file__).resolve().parent.parent.parent / "logs" / "hlr_enricher.log"
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


# ──────────── Один прогон ────────────

async def run_once() -> dict:
    """Один прогон отправки пачки в smsaero."""
    candidates = await phones_pending_hlr(limit=BATCH_SIZE)
    if not candidates:
        return {"candidates": 0}

    # Готовим номера в формате без + (smsaero ожидает только цифры)
    phone_by_clean: dict[str, dict] = {}
    for c in candidates:
        only_digits = "".join(ch for ch in c["phone"] if ch.isdigit())
        if only_digits:
            phone_by_clean[only_digits] = c

    if not phone_by_clean:
        return {"candidates": 0}

    stats = {
        "candidates": len(phone_by_clean),
        "sent": 0,
        "errors": 0,
    }

    try:
        result = await send_hlr_batch(list(phone_by_clean.keys()))
    except SmsaeroTransientError as e:
        # 5xx / network — попробуем в следующий прогон, ничего не пишем в БД
        logger.info(f"transient: {e} (попробуем позже)")
        stats["transient"] = stats["candidates"]
        return stats
    except SmsaeroError as e:
        # Любая другая ошибка smsaero — помечаем все номера error
        logger.error(f"smsaero error: {e}")
        for c in phone_by_clean.values():
            await mark_phone_hlr_error(c["id"])
        stats["errors"] = stats["candidates"]
        return stats

    # Записываем request_id для каждого принятого
    for clean_phone, req_id in result.items():
        c = phone_by_clean.get(clean_phone)
        if not c:
            continue
        await update_phone_hlr_request(c["id"], req_id)
        stats["sent"] += 1

    # Те номера которые smsaero не принял — помечаем error
    for clean_phone, c in phone_by_clean.items():
        if clean_phone not in result:
            await mark_phone_hlr_error(c["id"])
            stats["errors"] += 1

    return stats


# ──────────── Бесконечный цикл ────────────

async def enricher_loop():
    """Запускается из main.py как фоновая задача."""
    logger.info(f"hlr_enricher запущен, интервал={INTERVAL_SECONDS}s")
    await asyncio.sleep(START_DELAY_SECONDS)

    while True:
        try:
            stats = await run_once()
            if stats.get("candidates", 0) > 0:
                logger.info(
                    f"Прогон: candidates={stats['candidates']}, "
                    f"sent={stats.get('sent', 0)}, "
                    f"errors={stats.get('errors', 0)}, "
                    f"transient={stats.get('transient', 0)}"
                )
        except asyncio.CancelledError:
            logger.info("hlr_enricher остановлен")
            raise
        except Exception as e:
            logger.exception(f"Ошибка в цикле: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)
