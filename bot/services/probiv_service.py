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

async def probit_person(full_name: str, birth_date=None) -> ProbivResult:
    """
    Сделать пробив человека через все доступные провайдеры (параллельно).
    Объединить результаты в ProbivResult.

    full_name — полное ФИО (минимум "Фамилия Имя")
    birth_date — datetime.date или None
    """
    result = ProbivResult()

    # Список провайдеров. Сейчас один, но архитектура готова к расширению.
    providers = [
        _probit_sauron(full_name, birth_date),
        # _probit_other_service(full_name, birth_date),
    ]

    # Параллельный запрос с обработкой ошибок отдельно
    raw = await asyncio.gather(*providers, return_exceptions=True)

    for item in raw:
        if isinstance(item, Exception):
            err_msg = f"{type(item).__name__}: {item}"
            logger.warning(f"Probiv provider error: {err_msg}")
            result.errors.append(err_msg)
            continue

        result.raw_results.append(item)
        result.total_cost += item.get("cost", 0)

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

    return result


# ──────────── Форматирование результата для бота ────────────

def format_probiv_result(result: ProbivResult, header: str = "") -> str:
    """
    Сформировать текстовый ответ менеджеру:
    - Заголовок (например "🔍 Пробив выполнен")
    - Стоимость + статистика по провайдерам
    - Возможные связи по адресу
    - (шаблон родственника показываем отдельным сообщением — он жирный)
    """
    parts = []
    if header:
        parts.append(header)

    # Статистика
    n_ok = len(result.raw_results)
    n_fail = len(result.errors)
    if n_ok > 0:
        parts.append(
            f"💰 Стоимость пробива: *{result.total_cost:.2f} ₽* "
            f"({n_ok}/{n_ok + n_fail} провайдеров)"
        )
    else:
        parts.append("⚠️ Все провайдеры пробива вернули ошибку:")
        for err in result.errors:
            parts.append(f"  • {err}")
        return "\n".join(parts)

    # Связи
    parts.append("")
    parts.append(sauron_parser.format_address_relations(result.address_relations))

    return "\n".join(parts)
