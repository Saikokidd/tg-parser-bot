"""
HTTP API сервер.

Внешние агенты (например ha CRM) проверяют через нас занятость номера
телефона в БД и узнают офис.

Запросы защищены через X-API-Key (см. API_KEYS_HA в .env).

Эндпоинты:
    POST /api/check_phone
        Headers: X-API-Key: <secret>
        Body:    { "phone": "79161234567", "agent": "ha" }
        Resp:    { "status": "taken|free", "office": "ha|dp|pvl|null",
                   "is_yours": true|false }

Сервер поднимается из bot/main.py параллельно с polling бота.
"""
import os
import logging
import re
from aiohttp import web

from bot.db.queries import find_phone_owner_office, reserve_phone_for_ha

logger = logging.getLogger(__name__)

# Известные офисы, чтобы валидировать agent
KNOWN_OFFICES = {"pvl", "dp", "ha"}
# ID виртуального менеджера ha-api в БД (для резервов через API).
# Создан вручную: см. /tmp/create_ha_api_manager.sql
HA_API_MANAGER_ID = 98


def _load_api_keys() -> dict[str, str]:
    """
    Загружает API-ключи из env. Возвращает словарь {ключ: agent}.
    Поддерживает несколько ключей через запятую (на случай ротации).

    Пример .env:
        API_KEYS_HA=abc123,def456
        API_KEYS_DP=xyz789

    Каждый ключ привязан к agent — нельзя залогиниться под ключом ha
    и притвориться dp.
    """
    keys = {}
    for env_name, value in os.environ.items():
        if not env_name.startswith("API_KEYS_"):
            continue
        agent = env_name.replace("API_KEYS_", "").lower()
        if not value:
            continue
        for raw_key in value.split(","):
            key = raw_key.strip()
            if key:
                keys[key] = agent
    return keys


def _client_ip(request: web.Request) -> str:
    """Получить IP клиента (с учётом прокси, если будет nginx позже)."""
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote
        or "unknown"
    )


async def check_phone_handler(request: web.Request) -> web.Response:
    """POST /api/check_phone"""
    ip = _client_ip(request)

    # 1. Авторизация
    api_keys = request.app["api_keys"]
    provided_key = request.headers.get("X-API-Key", "")
    if not provided_key or provided_key not in api_keys:
        logger.warning(f"api: 401 unauthorized from ip={ip}")
        return web.json_response({"error": "unauthorized"}, status=401)

    key_agent = api_keys[provided_key]

    # 2. Парсим тело
    try:
        body = await request.json()
    except Exception:
        logger.warning(f"api: 400 invalid json from ip={ip} agent={key_agent}")
        return web.json_response({"error": "invalid json"}, status=400)

    phone = (body.get("phone") or "").strip() if isinstance(body, dict) else ""
    agent = (body.get("agent") or "").strip().lower() if isinstance(body, dict) else ""

    # 3. Проверяем agent (Q-B: 400 если неизвестный)
    if agent not in KNOWN_OFFICES:
        logger.warning(f"api: 400 unknown agent={agent!r} from ip={ip}")
        return web.json_response(
            {"error": "unknown agent", "known_agents": sorted(KNOWN_OFFICES)},
            status=400,
        )

    # 4. Проверяем что ключ соответствует agent
    # (нельзя слать с ключом ha, а в body указать agent=dp)
    if key_agent != agent:
        logger.warning(
            f"api: 403 key/agent mismatch: key_agent={key_agent} "
            f"body_agent={agent} ip={ip}"
        )
        return web.json_response({"error": "key/agent mismatch"}, status=403)

    # 5. Проверяем phone (Q-C: 400 если меньше 10 цифр)
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        logger.warning(
            f"api: 400 invalid phone={phone!r} from ip={ip} agent={agent}"
        )
        return web.json_response(
            {"error": "invalid phone format", "hint": "expected ≥10 digits"},
            status=400,
        )

    # 6. Ищем в БД
    try:
        owner_office = await find_phone_owner_office(phone)
    except Exception:
        logger.exception(f"api: 500 db error for phone={phone[-4:]}** agent={agent}")
        return web.json_response({"error": "internal error"}, status=500)

    if owner_office is None:
        # Свободен → резервируем за ha (если agent=ha).
        # Если в будущем другие агенты тоже захотят авто-резерв —
        # добавим аналогично.
        reserved_id = None
        if agent == "ha":
            reserved_id = await reserve_phone_for_ha(phone, HA_API_MANAGER_ID)
            if reserved_id:
                logger.info(
                    f"api: phone reserved for ha agent={agent} ip={ip} "
                    f"phone=***{digits[-4:]} new_id={reserved_id}"
                )
            else:
                logger.warning(
                    f"api: phone reserve FAILED for ha ip={ip} "
                    f"phone=***{digits[-4:]}"
                )
        result = {
            "status": "free",
            "office": None,
            "is_yours": False,
            "reserved": reserved_id is not None,
        }
    else:
        result = {
            "status": "taken",
            "office": owner_office,
            "is_yours": owner_office == agent,
            "reserved": False,
        }

    # Логируем результат. Phone маскируем — пишем только последние 4 цифры.
    logger.info(
        f"api: check_phone ok agent={agent} ip={ip} "
        f"phone=***{digits[-4:]} → {result}"
    )

    return web.json_response(result)


async def healthcheck_handler(request: web.Request) -> web.Response:
    """GET /api/health — для мониторинга."""
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
    app["api_keys"] = _load_api_keys()
    app.router.add_post("/api/check_phone", check_phone_handler)
    app.router.add_get("/api/health", healthcheck_handler)
    logger.info(
        f"api: configured with {len(app['api_keys'])} key(s), "
        f"agents={sorted(set(app['api_keys'].values()))}"
    )
    return app


async def start_api_server(port: int) -> web.AppRunner:
    """
    Поднять aiohttp web-сервер в фоне.
    Возвращает runner — его потом можно остановить через runner.cleanup().
    """
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    # 0.0.0.0 — слушаем все интерфейсы, нужно для внешних запросов
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"api: started on http://0.0.0.0:{port}")
    return runner
