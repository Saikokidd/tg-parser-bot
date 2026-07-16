"""
Фоновая задача: каждые N минут обогащает номера без оператора через voxlink.

Запускается из main.py через asyncio.create_task().
Логи пишутся в отдельный файл logs/voxlink_enrich.log.
"""
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from bot.db.connection import get_pool
from bot.db.queries import phones_pending_voxlink, update_phone_operator
from bot.services.voxlink_service import lookup_phone, VoxlinkTransientError

# ──────────── Настройки ────────────

# Интервал между прогонами
INTERVAL_SECONDS = 10 * 60  # 10 минут

# Сколько раз пытаемся для одного номера. После N неудач — помечаем как skipped.
MAX_ATTEMPTS = 2

# Сколько номеров обрабатываем за один прогон (чтобы не висеть в БД часами)
BATCH_LIMIT = 200

# Параллельность запросов
PARALLEL = 3


# ──────────── Логгер с ротацией в свой файл ────────────

logger = logging.getLogger("voxlink_enricher")
logger.setLevel(logging.INFO)
logger.propagate = False  # не дублируем в общий лог

_log_path = Path(__file__).resolve().parent.parent.parent / "logs" / "voxlink_enrich.log"
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


# ──────────── Доступ к БД ────────────

async def _fetch_candidates(limit: int) -> list:
    """
    Берём родственников у которых:
    - есть телефон
    - нет operator/region в extra
    - не помечены как voxlink_skipped
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, phone, extra
            FROM relatives
            WHERE phone IS NOT NULL AND phone != ''
              AND (extra->>'operator' IS NULL OR extra->>'operator' = '')
              AND COALESCE((extra->>'voxlink_skipped')::boolean, FALSE) = FALSE
            ORDER BY id
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def _update_with_info(rel_id: int, current_extra: dict, info: dict) -> None:
    """Записать operator/region в extra, не перетирая чужие поля."""
    new_extra = dict(current_extra or {})
    if info.get("operator"):
        new_extra["operator"] = info["operator"]
    if info.get("region"):
        new_extra["region"] = info["region"]
    if info.get("tz_offset"):
        new_extra["tz_offset"] = info["tz_offset"]
    if info.get("old_operator"):
        new_extra["old_operator"] = info["old_operator"]
    # Сбрасываем счётчик попыток если был
    new_extra.pop("voxlink_attempts", None)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE relatives SET extra = $2, updated_at = NOW() WHERE id = $1",
            rel_id, new_extra,
        )


async def _bump_attempts(rel_id: int, current_extra: dict) -> int:
    """
    Увеличиваем счётчик попыток. Если достигли MAX_ATTEMPTS — помечаем как skipped.
    Возвращаем новое значение счётчика.
    """
    new_extra = dict(current_extra or {})
    attempts = int(new_extra.get("voxlink_attempts", 0)) + 1
    new_extra["voxlink_attempts"] = attempts
    if attempts >= MAX_ATTEMPTS:
        new_extra["voxlink_skipped"] = True

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE relatives SET extra = $2 WHERE id = $1",
            rel_id, new_extra,
        )
    return attempts


# ──────────── Один прогон ────────────

async def _process_one(rel: dict, stats: dict, sem: asyncio.Semaphore):
    """Обработка одной записи под семафором (ограничение параллельности)"""
    async with sem:
        rel_id = rel["id"]
        phone = rel["phone"]
        extra = rel["extra"] or {}

        try:
            info = await lookup_phone(phone)
        except VoxlinkTransientError as e:
            # Сервер voxlink упал / прокси не отвечает.
            # НЕ увеличиваем счётчик — попробуем снова в следующем прогоне.
            logger.info(f"[#{rel_id}] {phone}: transient ({e}), пробуем позже")
            stats["transient"] = stats.get("transient", 0) + 1
            return
        except Exception as e:
            logger.warning(f"[#{rel_id}] {phone}: ошибка lookup_phone — {e}")
            stats["errors"] += 1
            return

        if info and (info.get("operator") or info.get("region")):
            await _update_with_info(rel_id, extra, info)
            stats["updated"] += 1
            return

        # 404 / номер не найден в базе voxlink — это реальная попытка
        attempts = await _bump_attempts(rel_id, extra)
        if attempts >= MAX_ATTEMPTS:
            stats["marked_skipped"] += 1
            logger.info(f"[#{rel_id}] {phone}: помечен voxlink_skipped после {attempts} попыток")
        else:
            stats["not_found_yet"] += 1


async def run_once() -> dict:
    """Один прогон обогащения. Возвращает статистику."""
    candidates = await _fetch_candidates(BATCH_LIMIT)
    if not candidates:
        return {"candidates": 0}

    stats = {
        "candidates": len(candidates),
        "updated": 0,
        "not_found_yet": 0,
        "marked_skipped": 0,
        "errors": 0,
    }

    sem = asyncio.Semaphore(PARALLEL)
    await asyncio.gather(*[_process_one(rel, stats, sem) for rel in candidates])

    return stats


# ──────────── Бесконечный цикл ────────────

async def enricher_loop():
    """Запускается из main.py как фоновая задача"""
    logger.info(f"voxlink_enricher запущен, интервал={INTERVAL_SECONDS}s")
    # Первый прогон даём небольшую паузу чтобы бот успел подняться
    await asyncio.sleep(60)

    while True:
        # Legacy: relatives.phone
        try:
            stats = await run_once()
            if stats.get("candidates", 0) > 0:
                logger.info(
                    f"Прогон legacy: candidates={stats['candidates']}, "
                    f"updated={stats.get('updated', 0)}, "
                    f"not_found={stats.get('not_found_yet', 0)}, "
                    f"marked_skipped={stats.get('marked_skipped', 0)}, "
                    f"transient={stats.get('transient', 0)}, "
                    f"errors={stats.get('errors', 0)}"
                )
        except asyncio.CancelledError:
            logger.info("voxlink_enricher остановлен")
            raise
        except Exception as e:
            logger.exception(f"Ошибка в цикле legacy: {e}")

        # Multi-phones: relative_phones
        try:
            rp_stats = await run_once_relative_phones()
            if rp_stats.get("rp_candidates", 0) > 0:
                logger.info(
                    f"Прогон relative_phones: candidates={rp_stats['rp_candidates']}, "
                    f"updated={rp_stats.get('rp_updated', 0)}, "
                    f"not_found={rp_stats.get('rp_not_found', 0)}, "
                    f"transient={rp_stats.get('rp_transient', 0)}, "
                    f"errors={rp_stats.get('rp_errors', 0)}"
                )
        except asyncio.CancelledError:
            logger.info("voxlink_enricher остановлен")
            raise
        except Exception as e:
            logger.exception(f"Ошибка в цикле relative_phones: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)


# ════════════════════════════════════════════════════════════════
#  Обогащение relative_phones (multi-phones фича)
# ════════════════════════════════════════════════════════════════

async def _process_one_phone(rp: dict, stats: dict, sem: asyncio.Semaphore):
    """
    Обработка одной записи relative_phones под семафором.
    Логика проще чем для legacy: один номер, один запрос, обновление operator.
    Voxlink-skipping для multi-phones не делаем — повторим в следующий
    прогон если не определилось.
    """
    async with sem:
        phone_id = rp["id"]
        phone = rp["phone"]
        try:
            info = await lookup_phone(phone)
        except VoxlinkTransientError as e:
            logger.info(f"[rp#{phone_id}] {phone}: transient ({e}), позже")
            stats["rp_transient"] = stats.get("rp_transient", 0) + 1
            return
        except Exception as e:
            logger.warning(f"[rp#{phone_id}] {phone}: ошибка lookup_phone — {e}")
            stats["rp_errors"] = stats.get("rp_errors", 0) + 1
            return

        if info and info.get("operator"):
            await update_phone_operator(phone_id, info["operator"], info.get("old_operator"))
            stats["rp_updated"] = stats.get("rp_updated", 0) + 1
        else:
            # 404 или nothing — пометим operator_checked_at (через None в update)
            # чтобы не выбирать постоянно тот же. В следующем прогоне попадёт
            # снова (через ORDER BY id ASC у которого нет фильтра по checked_at —
            # это ОК для нашего объёма).
            await update_phone_operator(phone_id, None)
            stats["rp_not_found"] = stats.get("rp_not_found", 0) + 1


async def run_once_relative_phones() -> dict:
    """Один прогон обогащения relative_phones."""
    candidates = await phones_pending_voxlink(limit=BATCH_LIMIT)
    if not candidates:
        return {"rp_candidates": 0}
    
    stats = {"rp_candidates": len(candidates)}
    sem = asyncio.Semaphore(PARALLEL)
    await asyncio.gather(*[_process_one_phone(rp, stats, sem) for rp in candidates])
    return stats
