"""
Клиент для SMS Aero API v2 — HLR-проверка номеров телефонов.

Документация: https://smsaero.ru/api/

Используемые методы:
  POST /v2/hlr/check  — отправить номер(а) на проверку, получить request_id
  GET  /v2/hlr/status — узнать статус по request_id

Авторизация: HTTP Basic с email:api_key.

В HLR не отправляются номера операторов которые smsaero не поддерживает
(Мегафон, Йота — фильтруется на уровне БД до вызова этого модуля).
"""
import os
import logging
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ──────────── Настройки ────────────

SMSAERO_EMAIL = os.getenv("SMSAERO_EMAIL", "").strip()
SMSAERO_API_KEY = os.getenv("SMSAERO_API_KEY", "").strip()
SMSAERO_BASE_URL = "https://gate.smsaero.ru/v2"

# Таймаут запросов (smsaero обычно отвечает за 2-3 секунды)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Маппинг smsaero hlrStatus → наш hlr_status
# По доке: 1 — доступен, 2 — недоступен, 3 — не существует, 4 — в работе
SMSAERO_STATUS_MAP = {
    1: "available",
    2: "unavailable",
    3: "not_exists",
    4: "in_work",
}


class SmsaeroError(Exception):
    """Базовая ошибка smsaero."""
    pass


class SmsaeroAuthError(SmsaeroError):
    """Неверные credentials."""
    pass


class SmsaeroValidationError(SmsaeroError):
    """Ошибка валидации запроса."""
    pass


class SmsaeroTransientError(SmsaeroError):
    """Временная ошибка (5xx, таймаут) — повторим позже."""
    pass


# ──────────── Низкоуровневый HTTP-клиент ────────────

async def _request(method: str, path: str, **kwargs) -> dict:
    """
    Выполнить HTTP-запрос к smsaero с basic auth.
    
    Возвращает dict из ответа (data из success-обёртки).
    Бросает SmsaeroError-подклассы при ошибках.
    """
    if not SMSAERO_EMAIL or not SMSAERO_API_KEY:
        raise SmsaeroError("SMSAERO_EMAIL/SMSAERO_API_KEY не заданы в .env")

    url = f"{SMSAERO_BASE_URL}{path}"
    auth = aiohttp.BasicAuth(SMSAERO_EMAIL, SMSAERO_API_KEY)

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.request(
                method, url, auth=auth, headers={"Accept": "application/json"},
                **kwargs,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json()
                except Exception:
                    text = await resp.text()
                    raise SmsaeroError(f"HTTP {status}: не JSON — {text[:200]}")

                # smsaero отвечает success:true/false + data/message
                if status == 401:
                    raise SmsaeroAuthError(body.get("message", "auth failed"))
                if status == 400:
                    raise SmsaeroValidationError(body.get("message", "validation"))
                if status >= 500:
                    raise SmsaeroTransientError(f"HTTP {status}: {body.get('message')}")
                if status != 200:
                    raise SmsaeroError(f"HTTP {status}: {body}")
                if not body.get("success"):
                    msg = body.get("message", "unknown error")
                    # 402 — недостаточно средств — это критично, пробрасываем как auth-like
                    if "money" in str(msg).lower() or "fund" in str(msg).lower():
                        raise SmsaeroError(f"NO MONEY: {msg}")
                    raise SmsaeroError(f"smsaero responded with success=false: {msg}")

                return body.get("data") or {}
    except aiohttp.ClientError as e:
        raise SmsaeroTransientError(f"network error: {e}")
    except TimeoutError:
        raise SmsaeroTransientError("timeout")


# ──────────── Высокоуровневое API ────────────

async def send_hlr_batch(phones: list[str]) -> dict[str, int]:
    """
    Отправить пачку номеров на HLR-проверку.

    Args:
        phones: список номеров в формате '79161234567' (без +)

    Returns:
        dict {phone: request_id} — только успешно принятые

    Smsaero принимает numbers[]=... как параметры.
    """
    if not phones:
        return {}
    # Нормализуем — убираем + и нецифры
    cleaned = []
    for p in phones:
        only_digits = "".join(c for c in p if c.isdigit())
        if only_digits:
            cleaned.append(only_digits)
    if not cleaned:
        return {}

    # smsaero hlr/check принимает либо number=X (один), либо numbers[]=X (массив)
    # При нескольких номерах в одном запросе ответ — массив объектов.
    if len(cleaned) == 1:
        params = {"number": cleaned[0]}
    else:
        # aiohttp нормально сериализует list-параметр как numbers[]=
        params = [("numbers[]", n) for n in cleaned]

    try:
        data = await _request("GET", "/hlr/check", params=params)
    except (SmsaeroAuthError, SmsaeroValidationError) as e:
        logger.warning(f"smsaero hlr/check rejected: {e}")
        return {}
    except SmsaeroTransientError as e:
        logger.info(f"smsaero hlr/check transient: {e}")
        raise

    # Парсим ответ. Для одного номера это dict, для нескольких — list dict-ов.
    result: dict[str, int] = {}
    items = data if isinstance(data, list) else [data]
    for item in items:
        num = str(item.get("number") or "")
        req_id = item.get("id")
        if num and req_id:
            result[num] = int(req_id)
    return result


async def get_hlr_status(request_id: int) -> Optional[str]:
    """
    Узнать статус HLR по request_id.

    Returns:
        наш статус из SMSAERO_STATUS_MAP — 'available'/'unavailable'/'not_exists'/'in_work'
        None — если что-то пошло не так / неизвестный код
    """
    try:
        data = await _request("GET", "/hlr/status", params={"id": request_id})
    except SmsaeroValidationError as e:
        # 400 invalid id — запрос не существует у них, помечаем как ошибку
        logger.warning(f"smsaero hlr/status invalid id={request_id}: {e}")
        return None
    except SmsaeroTransientError:
        # 5xx / network — попробуем в следующий раз
        raise
    except SmsaeroError as e:
        logger.warning(f"smsaero hlr/status error id={request_id}: {e}")
        return None

    hlr_status = data.get("hlrStatus")
    if hlr_status is None:
        return None
    return SMSAERO_STATUS_MAP.get(int(hlr_status))


# ──────────── Самодиагностика ────────────

async def test_auth() -> bool:
    """Проверка авторизации (для smoke-теста)."""
    try:
        data = await _request("GET", "/auth")
        return True
    except SmsaeroAuthError:
        return False
    except SmsaeroError as e:
        logger.error(f"test_auth failed: {e}")
        return False
