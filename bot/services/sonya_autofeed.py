"""
Фоновая задача: пассивная автоподача заполненных лидов менеджеру Соня (id=111).

Раз в INTERVAL проверяет гейты (autofeed_should_run: окно 09–18 Киев + дневной лимит),
и если можно — переносит ОДИН старейший подходящий лид (dp/ha, заполненный, невыгруженный,
возраст 3–10 дней, донор с >20 в окне) Соне: office='pvl', added_by=111, created_at=NOW(),
с маркером extra.autofed (для отката и дневного счётчика).

Рубильник: env SONYA_AUTOFEED_ENABLED (по умолчанию OFF). Пока не '1' — цикл спит навсегда.
Дневной лимит и «день» считаются по Киеву из БД (переживают рестарт).
"""
import asyncio
import logging
import os

from bot.db.queries import autofeed_should_run, autofeed_pick_and_move

logger = logging.getLogger("sonya_autofeed")

# ──────────── Настройки ────────────
TARGET_MANAGER = 111          # Соня
DAILY_LIMIT = 30              # потолок в день (недобор не переносится на завтра)
INTERVAL_SEC = 15 * 60        # тик каждые 15 минут — плавная капля, не пачкой
INITIAL_DELAY_SEC = 60        # не дёргать сразу на старте/рестарте
MIN_DAYS, MAX_DAYS, DONOR_MIN = 3, 10, 20


def _enabled() -> bool:
    return os.getenv("SONYA_AUTOFEED_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


async def autofeed_loop():
    if not _enabled():
        logger.info("sonya_autofeed: ВЫКЛЮЧЕН (SONYA_AUTOFEED_ENABLED не задан) — задача завершается")
        return

    logger.info(
        "sonya_autofeed: ВКЛЮЧЁН. Цель=%s, лимит=%s/день, тик=%sмин, окно=%s-%s дней, донор>%s",
        TARGET_MANAGER, DAILY_LIMIT, INTERVAL_SEC // 60, MIN_DAYS, MAX_DAYS, DONOR_MIN,
    )
    await asyncio.sleep(INITIAL_DELAY_SEC)

    last_skip_reason = None  # чтобы не спамить одинаковыми причинами простоя
    while True:
        try:
            st = await autofeed_should_run(TARGET_MANAGER, DAILY_LIMIT)
            if not st["allowed"]:
                if st["reason"] != last_skip_reason:
                    logger.info("sonya_autofeed: пауза — %s", st["reason"])
                    last_skip_reason = st["reason"]
            else:
                last_skip_reason = None
                moved = await autofeed_pick_and_move(
                    TARGET_MANAGER, MIN_DAYS, MAX_DAYS, DONOR_MIN
                )
                if moved:
                    logger.info(
                        "sonya_autofeed: перенесён лид id=%s '%s' от %s/mgr=%s → Соня (%s/%s сегодня)",
                        moved["id"], moved.get("full_name"),
                        moved.get("from_office"), moved.get("from_manager"),
                        st["today"] + 1, DAILY_LIMIT,
                    )
                else:
                    reason = "нет подходящих доноров (все ≤%s в окне)" % DONOR_MIN
                    if reason != last_skip_reason:
                        logger.info("sonya_autofeed: пауза — %s", reason)
                        last_skip_reason = reason
        except asyncio.CancelledError:
            logger.info("sonya_autofeed: остановлен")
            raise
        except Exception:
            logger.exception("sonya_autofeed: ошибка в цикле (продолжаю)")

        await asyncio.sleep(INTERVAL_SEC)