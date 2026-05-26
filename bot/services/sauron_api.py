"""
Клиент API сервиса пробива sauron.info

Документация поведения:
- Запрос ушёл успешно         → ok=True, есть result
- Ошибка авторизации/баланса  → ok=False, error_code, description
- Сетевая/таймаут             → исключение SauronError

Стоимость одного запроса по статистике ~0.02 ₽.
"""
import os
import logging
from typing import Optional
import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

API_BASE = "https://api.sauron.info/v1/query/get"
TIMEOUT_SEC = 60


class SauronError(Exception):
    """Любая ошибка при работе с API"""
    pass


class SauronAuthError(SauronError):
    """Ошибка авторизации (неверный токен / нет доступа)"""
    pass


class SauronBalanceError(SauronError):
    """Закончился баланс на счёте API"""
    pass


# ──────────── Внутренний хелпер ────────────

async def _post(endpoint: str, payload: dict) -> dict:
    """
    Универсальная отправка POST-запроса к API.
    Возвращает result из ответа или бросает SauronError.
    """
    token = os.getenv("SAURON_TOKEN")
    if not token:
        raise SauronError("SAURON_TOKEN не задан в .env")

    payload["token"] = token
    url = f"{API_BASE}/{endpoint}"

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=payload) as resp:
                data = await resp.json()
    except aiohttp.ClientError as e:
        raise SauronError(f"Сетевая ошибка: {e}") from e
    except Exception as e:
        raise SauronError(f"Ошибка запроса: {e}") from e

    # Разбор ответа
    if not data.get("ok"):
        code = data.get("error_code")
        desc = data.get("description", "Неизвестная ошибка")

        if code == 401:
            raise SauronAuthError(f"Авторизация не пройдена: {desc}")
        if "balance" in desc.lower() or code == 402:
            raise SauronBalanceError(f"Недостаточно средств: {desc}")

        raise SauronError(f"API вернул ошибку (code={code}): {desc}")

    return data.get("result", {})


# ──────────── Публичные методы ────────────

async def query_person(
    lastname: str,
    firstname: str,
    middlename: Optional[str] = None,
    day: Optional[int] = None,
    month: Optional[int] = None,
    year: Optional[int] = None,
    country: str = "RU"
) -> dict:
    """
    Пробить человека по ФИО (+опц. дате рождения).
    Возвращает result из ответа sauron — со всеми записями из разных источников.
    """
    payload = {
        "country": country,
        "lastname": lastname,
        "firstname": firstname,
    }
    if middlename:
        payload["middlename"] = middlename
    if day:
        payload["day"] = day
    if month:
        payload["month"] = month
    if year:
        payload["year"] = year

    logger.info(f"Sauron: пробив {lastname} {firstname} {middlename or ''} "
                f"{f'{day:02d}.{month:02d}.{year}' if day else ''}")

    result = await _post("person", payload)

    cost = result.get("cost", "?")
    balance = result.get("balance", "?")
    total = result.get("response", {}).get("total", 0)
    logger.info(f"Sauron: cost=${cost} balance=${balance} records={total}")

    return result


async def query_phone(phone: str, parser: bool = False) -> dict:
    """Пробить по номеру телефона. Не используется в основном флоу, на будущее."""
    payload = {
        "phone": phone,
        "parser": "1" if parser else "0",
    }
    return await _post("phone", payload)


# ──────────── Утилита: разбить ФИО ────────────

# Тюркские/арабские постфиксы отчества — Sauron их не принимает
# Убираем их перед отправкой запроса (Оглы/Кызы — "сын/дочь" по-тюркски)
TURKIC_FILIAL_SUFFIXES = {
    "оглы", "оглу", "огли",
    "кызы", "кизи", "гызы",
    "ызы",  # обрезанные
}


def _clean_name_part(part: str) -> str:
    """Убрать неподдерживаемые Sauron символы из части имени"""
    # Только буквы, тире и пробелы. Точки, скобки, цифры — выбрасываем
    import re as _re
    cleaned = _re.sub(r'[^а-яёА-ЯЁa-zA-Z\-\s]', '', part).strip()
    return cleaned


def split_full_name(full_name: str) -> tuple[str, str, Optional[str]]:
    """
    Из 'Иванов Иван Иванович' → ('Иванов', 'Иван', 'Иванович')
    Из 'Иванов Иван' → ('Иванов', 'Иван', None)

    Дополнительно: убирает тюркские постфиксы 'Оглы/Кызы/Оглу' и
    проблемные символы (скобки, цифры, точки) — иначе Sauron API возвращает 400.
    """
    parts = full_name.strip().split()
    if len(parts) < 2:
        raise ValueError(f"ФИО должно содержать минимум фамилию и имя: '{full_name}'")

    # Отфильтровываем тюркские постфиксы из любых позиций
    filtered = [p for p in parts if p.lower() not in TURKIC_FILIAL_SUFFIXES]
    if len(filtered) < 2:
        # После очистки осталось меньше двух слов — берём что есть
        filtered = parts

    lastname = _clean_name_part(filtered[0])
    firstname = _clean_name_part(filtered[1])
    middlename = _clean_name_part(filtered[2]) if len(filtered) >= 3 else None

    if not lastname or not firstname:
        raise ValueError(f"После очистки не осталось валидных частей ФИО: '{full_name}'")

    return lastname, firstname, middlename
