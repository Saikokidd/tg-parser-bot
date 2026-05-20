"""
Сервис определения оператора и региона по номеру телефона.
API: voxlink.ru — бесплатный, лимит 10 запросов/сек, без ключа.

Документация: GET http://num.voxlink.ru/get/?num=НОМЕР
Возвращает JSON: {code, num, full_num, operator, old_operator?, region}
"""
import os
import re
import logging
import aiohttp
import asyncio

logger = logging.getLogger(__name__)

API_URL = "http://num.voxlink.ru/get/"
TIMEOUT_SEC = 10

# Прокси с российским IP — voxlink с 2026 принимает запросы только из РФ.
# Указывается в .env как VOXLINK_PROXY_URL=http://user:pass@host:port
# Если не задан — запросы идут напрямую (для локальной разработки/тестов).
VOXLINK_PROXY_URL = os.getenv("VOXLINK_PROXY_URL", "").strip() or None
if VOXLINK_PROXY_URL:
    logger.info("voxlink: запросы будут идти через прокси")


def _normalize_phone_for_api(phone: str) -> str | None:
    """
    Привести телефон к виду для voxlink: только цифры, желательно с кодом страны.
    Возвращает None если не получилось извлечь номер.
    """
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return None
    # Если 10 цифр — добавляем '7' впереди (российский без кода)
    if len(digits) == 10:
        digits = '7' + digits
    # 8XXXXXXXXXX — заменяем 8 на 7
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits


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
