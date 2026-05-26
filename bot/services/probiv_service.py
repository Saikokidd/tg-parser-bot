"""
Сервис пробива — абстрактный слой над конкретными API провайдеров.

Сейчас поддерживается один провайдер (Sauron).
В будущем здесь можно будет добавлять новых: запросы будут идти параллельно,
а результаты — объединяться для более точных данных.
"""
import asyncio
import logging
from typing import Optional

from bot.services import sauron_api
from bot.parser import sauron_parser
from bot.services import voxlink_service, email_validator_service
from bot.db.queries import insert_probiv_log

logger = logging.getLogger(__name__)


class ProbivResult:
    """
    Объединённый результат пробива от всех провайдеров.

    raw_results — список сырых ответов API от каждого провайдера
                   (формат: [{'provider': 'sauron', 'data': {...}}, ...])
    address_relations — список блоков "возможные связи" со всех провайдеров
    relative_template — собранный шаблон родственника
    errors — ошибки от провайдеров (не критичные — другие могли отработать)
    """
    def __init__(self):
        self.raw_results: list[dict] = []
        self.address_relations: list[dict] = []
        self.relative_template: dict = {}
        self.errors: list[str] = []
        self.total_cost: float = 0.0


# ──────────── Запуск пробива у одного провайдера ────────────

async def _probit_sauron(full_name: str, birth_date) -> dict:
    """
    Сделать запрос к Sauron API.
    Возвращает {'provider', 'data', 'cost'} или бросает исключение.
    """
    try:
        lastname, firstname, middlename = sauron_api.split_full_name(full_name)
    except ValueError as e:
        raise ValueError(f"Не могу разобрать ФИО для пробива: {e}")

    # Логируем намерение запроса — поможет понять "почему мало данных" в будущем
    bd_str = birth_date.strftime("%d.%m.%Y") if birth_date else "БЕЗ ДР"
    logger.info(f"_probit_sauron: {full_name} ({bd_str})")

    kwargs = {
        "lastname": lastname,
        "firstname": firstname,
        "middlename": middlename,
    }
    if birth_date:
        kwargs["day"] = birth_date.day
        kwargs["month"] = birth_date.month
        kwargs["year"] = birth_date.year

    result = await sauron_api.query_person(**kwargs)
    return {
        "provider": "sauron",
        "data": result,
        "cost": float(result.get("cost", 0) or 0),
    }


# ──────────── Главная функция ────────────

async def probit_person(
    full_name: str,
    birth_date=None,
    manager_id: int | None = None,
    context: str = "other",
    military_id: int | None = None,
) -> ProbivResult:
    """
    Сделать пробив человека через все доступные провайдеры (параллельно).
    Объединить результаты в ProbivResult.

    Args:
        full_name: полное ФИО (минимум "Фамилия Имя")
        birth_date: datetime.date или None
        manager_id: ID менеджера запустившего пробив. None для админских/tool-прогонов.
        context: 'auto' / 'next' / 'tool' / 'other' — откуда вызван пробив.
                 Влияет только на учёт в probiv_log, не на саму логику.
        military_id: ID военного, если пробив привязан к нему (для 'auto').
    """
    result = ProbivResult()

    # Список провайдеров. Сейчас один, но архитектура готова к расширению.
    providers = [
        _probit_sauron(full_name, birth_date),
        # _probit_other_service(full_name, birth_date),
    ]

    # Параллельный запрос с обработкой ошибок отдельно
    raw = await asyncio.gather(*providers, return_exceptions=True)

    # Имена провайдеров параллельно для логирования (тот же порядок что в providers)
    provider_names = ["sauron"]

    for prov_name, item in zip(provider_names, raw):
        if isinstance(item, Exception):
            err_msg = f"{type(item).__name__}: {item}"
            logger.warning(f"Probiv provider error: {err_msg}")
            result.errors.append(err_msg)

            # Логируем неудачный запрос (Sauron часто берёт деньги даже за ошибки)
            await insert_probiv_log(
                provider=prov_name,
                context=context,
                manager_id=manager_id,
                full_name=full_name,
                birth_date=birth_date,
                cost=0,
                military_id=military_id,
                success=False,
                error=err_msg,
            )
            continue

        result.raw_results.append(item)
        result.total_cost += item.get("cost", 0)

        # Логируем успешный запрос
        await insert_probiv_log(
            provider=prov_name,
            context=context,
            manager_id=manager_id,
            full_name=full_name,
            birth_date=birth_date,
            cost=item.get("cost", 0),
            military_id=military_id,
            success=True,
        )

    if not result.raw_results:
        # Все провайдеры упали
        return result

    # Объединяем "возможные связи" со всех провайдеров
    for raw_item in result.raw_results:
        if raw_item["provider"] == "sauron":
            blocks = sauron_parser.extract_address_relations(raw_item["data"])
            result.address_relations.extend(blocks)

    # Сортируем связи: сначала свежие
    result.address_relations.sort(key=lambda b: b.get("year") or 0, reverse=True)

    # Собираем шаблон родственника. Пока на основе одного провайдера —
    # позже здесь будет мерж по самому частому значению через все провайдеры.
    for raw_item in result.raw_results:
        if raw_item["provider"] == "sauron":
            template = sauron_parser.build_relative_template(raw_item["data"])
            if template and not result.relative_template:
                result.relative_template = template
                break

    # Обогащение шаблона через дополнительные API
    if result.relative_template:
        await _enrich_template(result.relative_template)

    return result


async def _enrich_template(template: dict) -> None:
    """
    Обогащает шаблон родственника:
    - Телефон → voxlink → operator + region
    - emails_top → smtp.bz → только валидные
    Изменяет template inplace.
    """
    # Запускаем параллельно: lookup_phone + validate_emails
    phone = template.get("phone")
    emails_top = template.get("emails_top") or []

    tasks = []
    tasks.append(voxlink_service.lookup_phone(phone) if phone else asyncio.sleep(0, result=None))
    tasks.append(
        email_validator_service.validate_emails_parallel(emails_top)
        if emails_top else asyncio.sleep(0, result={})
    )

    phone_info, email_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Обработка исключений
    if isinstance(phone_info, Exception):
        logger.warning(f"voxlink failed: {phone_info}")
        phone_info = None
    if isinstance(email_results, Exception):
        logger.warning(f"email validation failed: {email_results}")
        email_results = {}

    # Записываем в шаблон
    template["phone_info"] = phone_info  # dict | None
    template["valid_emails"] = [e for e, ok in (email_results or {}).items() if ok]


# ──────────── Форматирование результата для бота ────────────

def format_probiv_result(result: ProbivResult, header: str = "") -> str:
    """
    Сформировать текстовый ответ менеджеру:
    - Заголовок (например "🔍 Пробив выполнен")
    - Возможные связи по адресу

    Стоимость и количество провайдеров не показываем.
    """
    parts = []
    if header:
        parts.append(header)

    n_ok = len(result.raw_results)
    if n_ok == 0:
        parts.append("⚠️ Все провайдеры пробива вернули ошибку:")
        for err in result.errors:
            parts.append(f"  • {err}")
        return "\n".join(parts)

    parts.append("")
    parts.append(sauron_parser.format_address_relations(result.address_relations))

    return "\n".join(parts)
