"""
Сервис определения оператора и региона по номеру телефона.
API: voxlink.ru — бесплатный, лимит 10 запросов/сек, без ключа.

Документация: GET http://num.voxlink.ru/get/?num=НОМЕР
Возвращает JSON: {code, num, full_num, operator, old_operator?, region}
"""
import os
import re
import logging
import time
import aiohttp
import asyncio
from bot.services.tz_regions import region_to_msk_offset

logger = logging.getLogger(__name__)

API_URL = "http://num.voxlink.ru/get/"
TIMEOUT_SEC = 10

# Прокси с российским IP — устаревший путь.
# С 29 мая 2026 ходим через WireGuard-туннель wg1 (см. /etc/wireguard/wg1.conf).
# Маршрут до 79.137.209.117 уведён через RU-VPN, на уровне ядра.
# Переменная VOXLINK_PROXY_URL оставлена опционально, если когда-то понадобится
# вернуть HTTP-прокси.
VOXLINK_PROXY_URL = os.getenv("VOXLINK_PROXY_URL", "").strip() or None
if VOXLINK_PROXY_URL:
    logger.info("voxlink: запросы будут идти через прокси")
else:
    logger.info("voxlink: запросы напрямую (предполагается WG-маршрут)")


# ──────────── Rate limiter ────────────
# voxlink заявляет 10 RPS, на практике 8-9 уже даёт 429. Держим 8 с запасом.
# Лимитер общий на весь процесс — действует для всех потребителей
# (probiv_service, voxlink_enricher, tools/* при импорте этого модуля).
#
# Реализация: простой временной слот. Перед каждым запросом считаем
# когда было прошлое обращение, и если прошло меньше MIN_INTERVAL —
# спим оставшееся.
VOXLINK_MAX_RPS = 8
_MIN_INTERVAL = 1.0 / VOXLINK_MAX_RPS  # секунды между запросами
_rate_lock = asyncio.Lock()
_last_request_at = 0.0


async def _rate_limit():
    """Подождать до следующего разрешённого слота. Глобальный лок на процесс."""
    global _last_request_at
    async with _rate_lock:
        now = time.monotonic()
        wait = _last_request_at + _MIN_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def _normalize_phone_for_api(phone: str) -> str | None:
    """
    Привести телефон к виду для voxlink: только цифры, валидный код страны,
    мобильный (НЕ городской) номер.
    Возвращает None если номер невалидный или не мобильный —
    НЕ делаем запрос к API, чтобы не засорять логи 404-ками.

    Принимаемые форматы → результат:
        +79991234567 / 79991234567  → 79991234567   (российский мобильный)
        89991234567                 → 79991234567   (старый формат)
        9991234567                  → 79991234567   (без кода)
        +380501234567 / 380501234567 → 380501234567 (украинский)

    Отбрасываются (возвращается None):
        +74951234567   — российские городские (Москва)
        +78121234567   — российские городские (СПб)
        76305775882    — другие нет-мобильные префиксы РФ
        +71604631      — слишком короткий, очевидный мусор
        +79088923456789012345  — слишком длинный, склейка
        abcdef         — не цифры
    """
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return None

    # Нормализуем к каноническому виду (без +, с кодом страны)
    # 10 цифр → считаем российским без кода
    if len(digits) == 10:
        digits = '7' + digits
    # 11 цифр с '8' → меняем на '7'
    elif len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]

    # Российский номер: 11 цифр, начинается с 7
    if len(digits) == 11 and digits.startswith('7'):
        # Мобильные операторы РФ используют префикс 79 (то есть код +7 9xx).
        # 74xx — Москва городская, 78xx — СПб городская и т.д.
        # voxlink работает только с мобильными — городские отбрасываем.
        if digits[1] != '9':
            return None
        return digits

    # Украинский номер: 12 цифр, начинается с 380
    if len(digits) == 12 and digits.startswith('380'):
        return digits

    # Всё остальное — невалидно
    return None


class VoxlinkTransientError(Exception):
    """Временная ошибка (voxlink/прокси упал). Не считается за попытку."""
    pass


async def lookup_phone(phone: str) -> dict | None:
    """
    Получить оператора и регион номера.

    Возвращает:
        dict {'operator', 'region', 'old_operator'} — если найдено
        None — если номер невалиден или просто не найден (404)

    Бросает VoxlinkTransientError при временных сбоях (5xx, таймаут, сеть).
    """
    num = _normalize_phone_for_api(phone)
    if not num:
        return None

    # Глобальный rate-limit 8 RPS — общий на весь процесс.
    # Сюда приходят и фоновый enricher, и автопробив, и tools.
    await _rate_limit()

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                API_URL,
                params={"num": num},
                proxy=VOXLINK_PROXY_URL,
            ) as resp:
                # 5xx — voxlink/прокси сломан, надо повторить позже
                if 500 <= resp.status < 600:
                    logger.warning(f"voxlink: HTTP {resp.status} (transient) для {phone}")
                    raise VoxlinkTransientError(f"HTTP {resp.status}")
                # 4xx и прочие — номер не найден / невалиден
                if resp.status != 200:
                    logger.warning(f"voxlink: HTTP {resp.status} для номера {phone}")
                    return None
                data = await resp.json(content_type=None)
    except VoxlinkTransientError:
        raise
    except asyncio.TimeoutError:
        logger.warning(f"voxlink: timeout для {phone}")
        raise VoxlinkTransientError("timeout")
    except aiohttp.ClientError as e:
        # Сетевые проблемы, прокси не отвечает — это транзиентно
        logger.warning(f"voxlink: сетевая ошибка для {phone}: {e}")
        raise VoxlinkTransientError(str(e))
    except Exception as e:
        logger.warning(f"voxlink: неизвестная ошибка для {phone}: {e}")
        return None

    operator = data.get("operator")
    region = data.get("region")
    if not operator and not region:
        return None

    return {
        "operator": operator,
        "region": region,
        "tz_offset": region_to_msk_offset(region),
        "old_operator": data.get("old_operator"),
    }


def format_phone_with_info(phone: str, info: dict | None) -> str:
    """
    Сформировать строку 'Телефон: +79991234567 (МегаФон, Москва)'.
    Если инфы нет — возвращаем номер как есть.
    """
    if not phone:
        return "—"
    if not info:
        return phone

    operator = info.get("operator") or "—"
    region = info.get("region") or "—"
    return f"{phone} ({operator}, {region})"
