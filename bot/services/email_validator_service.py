"""
Сервис валидации email через API smtp.bz.
Документация: https://smtp.bz/
Авторизация: header 'Authorization: <api_key>'
"""
import os
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.smtp.bz/v1/check/email"
TIMEOUT_SEC = 15


async def validate_email(email: str) -> dict:
    """
    Проверить email через smtp.bz.
    Возвращает {'valid': bool|None, 'error': str|None}
    valid=True   — почта рабочая
    valid=False  — почта не рабочая
    valid=None   — не смогли проверить (ключ, таймаут, ошибка API)
    """
    api_key = os.getenv("SMTPBZ_API_KEY")
    if not api_key:
        return {"valid": None, "error": "SMTPBZ_API_KEY не задан в .env"}

    if not email or "@" not in email:
        return {"valid": False, "error": "невалидный формат"}

    url = f"{BASE_URL}/{email}"
    headers = {"Authorization": api_key}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    valid = bool(data.get("result", False))
                    return {"valid": valid, "error": None}
                if resp.status == 400:
                    return {"valid": False, "error": "ошибка квоты или валидации"}
                if resp.status == 401:
                    return {"valid": None, "error": "неверный API ключ"}
                return {"valid": None, "error": f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        return {"valid": None, "error": "таймаут"}
    except aiohttp.ClientError as e:
        logger.warning(f"smtp.bz: сетевая ошибка для {email}: {e}")
        return {"valid": None, "error": "сетевая ошибка"}
    except Exception as e:
        logger.warning(f"smtp.bz: неизвестная ошибка для {email}: {e}")
        return {"valid": None, "error": "внутренняя ошибка"}


async def validate_emails_parallel(emails: list[str], max_concurrent: int = 3) -> dict[str, bool]:
    """
    Проверить несколько email параллельно.
    Возвращает {email: True/False}.
    Email с valid=None (ошибка проверки) пропускаются.
    """
    if not emails:
        return {}

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _check(email: str):
        async with semaphore:
            result = await validate_email(email)
            return email, result

    results = await asyncio.gather(
        *(_check(e) for e in emails),
        return_exceptions=True
    )

    output = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        email, res = r
        if res["valid"] is True:
            output[email] = True
        elif res["valid"] is False:
            output[email] = False
        # None пропускаем — значит не смогли проверить
    return output
