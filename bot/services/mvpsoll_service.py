"""
Сервис определения оператора и часового пояса по номеру телефона.
API: mvpsoll.com — MNP-aware, батч до 1000 номеров, ключ в заголовке Bearer.
Документация: https://mvpsoll.com/api/v1  (POST /lookup)

Работает НАПРЯМУЮ (без VPN/прокси) — сервис доступен с немецкого IP.
Интерфейс совместим с прежним voxlink_service:
    lookup_phone(phone) -> dict | None  с ключами operator/region/tz_offset/old_operator
Плюс новая батч-функция lookup_phones_batch(phones) для enricher (до 1000 за раз).
"""
import os
import re
import logging
import asyncio
import aiohttp

logger = logging.getLogger(__name__)

API_BASE = "https://mvpsoll.com/api/v1"
API_KEY = os.getenv("MVPSOLL_API_KEY", "").strip()
TIMEOUT_SEC = 40
MAX_BATCH = 1000

if not API_KEY:
    logger.warning("mvpsoll: MVPSOLL_API_KEY не задан в окружении")


class MvpsollTransientError(Exception):
    """Временная ошибка (5xx / таймаут / сеть / нет баланса). Не считается за попытку."""
    pass


# ──────────── Маппинг операторов: юрназвание mvpsoll -> короткое имя ────────────
# mvpsoll отдаёт полные названия ("Мобильные ТелеСистемы ПАО").
# Приводим к коротким, совместимым с логикой бота и skip-листом (Мегафон/Йота).
_OPERATOR_MAP = [
    (re.compile(r"телесистем|\bмтс\b|\bmts\b", re.I), "МТС"),
    (re.compile(r"мегафон|megafon", re.I),           "МегаФон"),
    (re.compile(r"йота|\byota\b", re.I),             "Йота"),
    (re.compile(r"вымпел|билайн|beeline|вымпелком", re.I), "Билайн"),
    (re.compile(r"\bt2\b|tele2|теле2|т2\b|теле ?2", re.I), "Т2"),
    (re.compile(r"екатеринбург.*2000|мотив|motiv", re.I), "Мотив"),
    (re.compile(r"тинькоф|tinkoff|т-мобайл|t-mobile", re.I), "Тинькофф Мобайл"),
    (re.compile(r"сбербанк|сбермобайл|sber", re.I),  "СберМобайл"),
]


def _map_operator(raw: str | None) -> str | None:
    if not raw:
        return None
    for rx, short in _OPERATOR_MAP:
        if rx.search(raw):
            return short
    # неизвестный — вернём как есть (чтобы не терять данные)
    return raw.strip()


# ──────────── timezone IANA -> строка смещения от МСК ("МСК", "МСК+3") ────────────
# Москва = UTC+3. Считаем смещение зоны минус 3.
# Оффлайн-таблица основных российских зон (без внешних зависимостей).
_TZ_UTC_OFFSET = {
    "Europe/Kaliningrad": 2,
    "Europe/Moscow": 3, "Europe/Simferopol": 3, "Europe/Volgograd": 3,
    "Europe/Kirov": 3, "Europe/Astrakhan": 4, "Europe/Saratov": 4,
    "Europe/Ulyanovsk": 4, "Europe/Samara": 4,
    "Asia/Yekaterinburg": 5,
    "Asia/Omsk": 6,
    "Asia/Novosibirsk": 7, "Asia/Barnaul": 7, "Asia/Tomsk": 7,
    "Asia/Novokuznetsk": 7, "Asia/Krasnoyarsk": 7,
    "Asia/Irkutsk": 8,
    "Asia/Yakutsk": 9, "Asia/Chita": 9, "Asia/Khandyga": 9,
    "Asia/Vladivostok": 10, "Asia/Ust-Nera": 10,
    "Asia/Magadan": 11, "Asia/Sakhalin": 11, "Asia/Srednekolymsk": 11,
    "Asia/Kamchatka": 12, "Asia/Anadyr": 12,
}

def _tz_to_msk_offset(timezone: str | None) -> str | None:
    if not timezone:
        return None
    utc_off = _TZ_UTC_OFFSET.get(timezone)
    if utc_off is None:
        return None
    off = utc_off - 3  # относительно Москвы
    return "МСК" if off == 0 else f"МСК{off:+d}"


def _to_dict(entry: dict | None) -> dict | None:
    """Ответ mvpsoll по одному номеру -> формат, совместимый с voxlink."""
    if not entry:
        return None
    operator = _map_operator(entry.get("operator"))
    tz = entry.get("timezone")
    if not operator:
        return None
    return {
        "operator": operator,
        "region": None,          # mvpsoll региона не даёт, только timezone
        "tz_offset": _tz_to_msk_offset(tz),
        "old_operator": None,    # MNP old-operator mvpsoll не возвращает в /lookup
    }


async def lookup_phones_batch(phones: list[str]) -> dict[str, dict | None]:
    """
    Пробить пачку номеров (до 1000) одним запросом.
    Возвращает {исходный_номер: dict|None}.
    Бросает MvpsollTransientError при временных сбоях.
    """
    if not phones:
        return {}
    if not API_KEY:
        raise MvpsollTransientError("MVPSOLL_API_KEY не задан")

    # dedup + лимит
    uniq = list(dict.fromkeys(p for p in phones if p))[:MAX_BATCH]
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{API_BASE}/lookup", json={"phones": uniq}, headers=headers
            ) as resp:
                if resp.status == 402:
                    logger.error("mvpsoll: недостаточно баланса (402)")
                    raise MvpsollTransientError("insufficient_balance")
                if 500 <= resp.status < 600:
                    raise MvpsollTransientError(f"HTTP {resp.status}")
                if resp.status == 401:
                    logger.error("mvpsoll: неверный API-ключ (401)")
                    raise MvpsollTransientError("invalid_api_key")
                if resp.status != 200:
                    logger.warning(f"mvpsoll: HTTP {resp.status}")
                    return {p: None for p in phones}
                data = await resp.json(content_type=None)
    except MvpsollTransientError:
        raise
    except asyncio.TimeoutError:
        raise MvpsollTransientError("timeout")
    except aiohttp.ClientError as e:
        raise MvpsollTransientError(str(e))

    results = data.get("results", {}) or {}
    # ключи ответа — нормализованные mvpsoll'ом (+7...). Сопоставим обратно к исходным.
    out: dict[str, dict | None] = {}
    # построим индекс нормализованных -> dict
    norm_map = { _norm(k): _to_dict(v) for k, v in results.items() }
    for p in phones:
        out[p] = norm_map.get(_norm(p))
    return out


def _norm(phone: str) -> str:
    """Нормализация для сопоставления: только цифры, 8->7, ведущая 7."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return d


async def lookup_phone(phone: str) -> dict | None:
    """
    Совместимость с прежним voxlink-интерфейсом: один номер -> dict|None.
    Под капотом — батч на 1 номер.
    """
    res = await lookup_phones_batch([phone])
    return res.get(phone)


def format_phone_with_info(phone: str, info: dict | None) -> str:
    if not phone:
        return "—"
    if not info:
        return phone
    operator = info.get("operator") or "—"
    region = info.get("region") or info.get("tz_offset") or "—"
    return f"{phone} ({operator}, {region})"


# ──────────── Алиасы для drop-in замены voxlink_service ────────────
# Позволяют менять только имя модуля в импортах, не трогая остальной код:
#   from bot.services.voxlink_service import lookup_phone, VoxlinkTransientError
#   → from bot.services.mvpsoll_service import lookup_phone, VoxlinkTransientError
VoxlinkTransientError = MvpsollTransientError