"""
Реестр недействительных ИНН физлиц (ФНС), service.nalog.ru.

ИНН физлица аннулируют при снятии с учёта — чаще всего по смерти, но также
при гашении дублей. Поэтому важен не только факт, но и год: свежая дата
говорит о регистрации смерти, старая почти всегда означает дубль.

Сервис бесплатный, без капчи и ключей, но требует сессионную куку со
страницы формы.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

PAGE = "https://service.nalog.ru/invalid-inn-fl.html"
API = "https://service.nalog.ru/invalid-inn-proc.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
TIMEOUT = aiohttp.ClientTimeout(total=15)

_session: aiohttp.ClientSession | None = None
_lock = asyncio.Lock()


class FnsUnavailable(Exception):
    """Сервис не ответил. Это не «ИНН не найден» — решение принимать нельзя."""


# ──────────── ИНН: проверка и извлечение ────────────

def valid_inn(digits: str) -> bool:
    """Личный ИНН: ровно 12 цифр и верные контрольные разряды."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    n = [int(c) for c in digits]
    c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k11 = sum(a * b for a, b in zip(c11, n[:10])) % 11 % 10
    k12 = sum(a * b for a, b in zip(c12, n[:11])) % 11 % 10
    return k11 == n[10] and k12 == n[11]


def extract_inns(node, found: set | None = None) -> set:
    """Собрать все валидные ИНН из ответа Саурона, на любой глубине."""
    if found is None:
        found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.strip().upper() == "ИНН" and v:
                d = re.sub(r"\D", "", str(v))
                if valid_inn(d):
                    found.add(d)
            else:
                extract_inns(v, found)
    elif isinstance(node, list):
        for item in node:
            extract_inns(item, found)
    return found


# ──────────── Запрос в ФНС ────────────

async def _session_ready() -> aiohttp.ClientSession:
    global _session
    async with _lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                headers={"User-Agent": UA}, timeout=TIMEOUT)
            async with _session.get(PAGE) as r:
                await r.read()
        return _session


async def invalid_since(inn: str) -> dt.date | None:
    """
    Дата аннулирования ИНН, либо None — если в реестре его нет.
    При недоступности сервиса бросает FnsUnavailable.
    """
    last = None
    for attempt in (1, 2):
        try:
            s = await _session_ready()
            async with s.post(API, data={"k": "fl", "inn": inn}, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": PAGE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }) as r:
                body = await r.text()
            raw = json.loads(body).get("date")
            if not raw:
                return None
            return dt.datetime.strptime(raw.split()[0], "%d.%m.%Y").date()
        except Exception as e:
            last = e
            logger.warning("ФНС попытка %d для ИНН …%s: %s", attempt, inn[-4:], e)
            global _session
            if _session and not _session.closed:
                await _session.close()      # протухла кука — пересоздадим
            _session = None
            await asyncio.sleep(1)
    raise FnsUnavailable(str(last))


async def latest_invalid(inns) -> dict:
    """{инн: дата} только по тем, что найдены в реестре."""
    out = {}
    for inn in sorted(inns):
        d = await invalid_since(inn)
        if d:
            out[inn] = d
    return out


# ──────────── Политика по годам ────────────

def allowed_years(manager_id: int | None = None) -> set:
    """
    Какие годы аннулирования ИНН считаем рабочими.

    PVL_INN_YEARS=2025,2026 — общее правило для офиса.
    PVL_INN_YEARS_57=2024,2025,2026 — исключение для менеджера 57.
    Пусто или не задано — проверка выключена.
    """
    raw = ""
    if manager_id is not None:
        raw = os.getenv(f"PVL_INN_YEARS_{manager_id}", "")
    if not raw:
        raw = os.getenv("PVL_INN_YEARS", "")
    return {int(x) for x in re.findall(r"\d{4}", raw)}


def verdict(dates: dict, manager_id: int | None = None) -> tuple:
    """
    (подходит, лучшая_дата) по самой поздней дате аннулирования.
    Пустой словарь — ИНН в реестре нет, значит не подходит.
    """
    if not dates:
        return False, None
    best = max(dates.values())
    return best.year in allowed_years(manager_id), best


def defer_manager_id() -> int:
    """Кому уходят отложенные лиды. По умолчанию Драйвер (139)."""
    return int(os.getenv("PVL_INN_DEFER_MANAGER_ID", "139"))


def noinn_manager_id() -> int:
    """
    Кому уходят лиды, у которых Саурон вообще не нашёл ИНН.

    Их отличие от отложенных: перепроверять нечего — нет номера, который можно
    бесплатно спросить у ФНС. Нужен новый платный пробив, поэтому куча отдельная.
    По умолчанию Findre (1).
    """
    return int(os.getenv("PVL_INN_NOINN_MANAGER_ID", "1"))


# ──────────── ЗАГС: свидетельство о смерти ────────────

def _pdate(v):
    """'2025-12-03' или '2025-12-04 05:06:45.937' → date, иначе None."""
    try:
        return dt.date.fromisoformat(str(v or "").strip()[:10])
    except ValueError:
        return None


def _norm_fio(v) -> str:
    t = str(v or "").lower().replace("\u0451", "\u0435")
    t = re.sub(r"[^а-яa-z\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Роли, означающие, что запись о ДРУГОМ человеке: наш лишь участник
OTHER_ROLES = ("отец", "мать", "супруг", "сын", "дочь", "заявител",
               "ребен", "ребён", "родител")


def _is_subject(rec: dict, full_name, birth_date) -> bool:
    """
    Запись должна быть о том, кого пробиваем.

    В ответе Саурона лежат и записи его связей. Без этой проверки смерть
    отца или ребёнка засчиталась бы самому военному — он жив, а бот сказал
    бы «проходит».
    """
    role = str(rec.get("Роль участника", "")).lower()
    if role and any(r in role for r in OTHER_ROLES):
        return False
    bd = _pdate(rec.get("День рождения"))
    if birth_date is not None and bd is not None and bd != birth_date:
        return False
    a, b = _norm_fio(rec.get("ФИО")), _norm_fio(full_name)
    if a and b and a.split()[:2] != b.split()[:2]:
        return False
    return True


def extract_zags_death(node, full_name=None, birth_date=None, found=None) -> list:
    """
    Записи ЗАГС о смерти: (дата выдачи свидетельства, дата смерти, источник).

    В выгрузках ЗАГС лежат все акты — браки, рождения, смерти, — и у одного
    человека их бывает несколько за разные годы. Поэтому отбираем строго по
    типу события: иначе однажды примем за погибшего живого, у которого в
    рабочем году родился ребёнок.
    """
    if found is None:
        found = []
    if isinstance(node, dict):
        src = str(node.get("Источник", "")).strip()
        ev = str(node.get("Тип события", "")).lower()
        doc = str(node.get("Документ", "")).lower()
        if (src.upper().startswith("ЗАГС") and ("смерт" in ev or "смерт" in doc)
                and _is_subject(node, full_name, birth_date)):
            found.append((
                _pdate(node.get("Дата выдачи свидетельства")),
                _pdate(node.get("Дата смерти") or node.get("Дата мероприятия")),
                src,
            ))
        for v in node.values():
            extract_zags_death(v, full_name, birth_date, found)
    elif isinstance(node, list):
        for item in node:
            extract_zags_death(item, full_name, birth_date, found)
    return found


def zags_years(manager_id: int | None = None) -> set:
    """
    Рабочие годы выдачи свидетельства.

    PVL_ZAGS_YEARS, при отсутствии — те же годы, что и для ИНН.
    Дата выдачи свидетельства важнее даты аннулирования ИНН: последняя
    равна дате смерти, а нам нужно знать, когда семья получила документ.
    """
    raw = ""
    if manager_id is not None:
        raw = os.getenv(f"PVL_ZAGS_YEARS_{manager_id}", "")
    if not raw:
        raw = os.getenv("PVL_ZAGS_YEARS", "")
    years = {int(x) for x in re.findall(r"\d{4}", raw)}
    return years or allowed_years(manager_id)
