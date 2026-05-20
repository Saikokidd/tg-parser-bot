"""
Парсинг блокнотов через Claude API (Haiku 4.5).

Шаги:
1. Читаем .txt блокнот
2. Делим на чанки (~50 записей)
3. Каждый чанк → API → JSON
4. Сохраняем итоговый JSON в файл

Использование:
    venv/bin/python -m tools.import_parsers.llm_parser blocknotes/Миша.txt parsed/Миша.json

Перед импортом в БД ничего не делаем — только парсим.
"""
import os
import sys
import json
import asyncio
import logging
import re
from pathlib import Path
from typing import Any
import aiohttp
from dotenv import load_dotenv

# Грузим .env проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm_parser")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8192

# Размер чанка по строкам исходника. Один блокнот режется на куски,
# чтобы не упереться в лимит выходных токенов модели.
CHUNK_LINES = 150

# Чанки идут последовательно (PARALLEL=1) из-за rate limit Anthropic
# 10K output токенов/минуту на Haiku → больше одного запроса разом не выдержит.
PARALLEL = 1

# Между запросами ждём минимум столько секунд (бережёт rate limit)
DELAY_BETWEEN_REQUESTS = 30

# Сколько раз повторяем при 429
MAX_RETRIES_ON_429 = 6
RETRY_DELAY = 30


SYSTEM_PROMPT = """Ты парсер записей о военных и их родственниках из неструктурированного текста.

ТВОЯ ЗАДАЧА: извлечь данные ТОЛЬКО из текста, который тебе дали. НИЧЕГО НЕ ПРИДУМЫВАЙ.
Если поле не указано в тексте — оставляй null. Лучше пропустить, чем сочинить.

Возвращай строго JSON-массив объектов военных. Каждый объект:
{
  "full_name": "string",            // ФИО военного
  "birth_date": "DD.MM.YYYY" | null, // ДР военного
  "status": "killed" | "missing" | "string",
  // status:
  //   "killed" если есть "Дата смерти", "погиб", "200", "ПОГИБ"
  //   "missing" если есть "без вести пропавший", "пропал", "500", "БП"
  //   иначе свободный текст из источника или "missing" если непонятно
  "extra": {
    "unit": "string|null",          // Б/Ч, в/ч, полк, дивизия — одной строкой
    "callsign": "string|null",      // позывной
    "note": "string|null"           // Все остальные поля одной строкой через " | ":
                                    // даты "Без вести пропавший", "Дата смерти", доп. инфа,
                                    // место службы и т.д.
  },
  "relatives": [
    {
      "full_name": "string",
      "birth_date": "DD.MM.YYYY" | null,
      "phone": "string|null",       // только цифры с кодом страны, например "79991234567"
      "address": "string|null",     // если несколько адресов — через " ; "
      "extra": {
        "role": "жена" | "мать" | "отец" | "сестра" | "брат" | "сын" | "дочь" | "бабушка" | "дедушка" | "тётя" | "дядя" | null,
        "snils": "string|null",     // только цифры
        "inn": "string|null",       // только цифры
        "passport": "string|null",  // только цифры
        "email": "string|null",
        "phones_other": "string|null",  // если несколько телефонов — все остальные через ", "
        "operator": "string|null",  // если в тексте указан оператор телефона (Мегафон, МТС, Т2 и т.д.)
        "note": "string|null"       // прочая инфа: "Сын", "Получила выплаты", "На попробовать" и т.д.
      }
    }
  ]
}

ВАЖНО:
- Прочерки "-", "—", "= = =" → null, не пиши их
- "ФИО Родственника" БЕЗ метки "Жена/Мать" — определи роль по контексту, или поставь null в role
- Если у одного военного несколько родственников — все в массив relatives
- Если несколько военных подряд относятся к одной семье и явно связаны (например брат "Элерт Александр" и "Элерт Сергей") — каждый отдельной записью
- Если в одной записи указано несколько родственников через ";" или ", " — каждого отдельно
- Если военного нет (только родственники без главного) — пропусти весь блок, не выдумывай военного
- ОЧЕНЬ ВАЖНО: если в тексте идёт " = " как разделитель блоков — это границы записей

ОТВЕТ — ТОЛЬКО валидный JSON-массив. Никаких комментариев, markdown-обёрток, преамбул."""


def chunk_text(text: str, max_lines: int = CHUNK_LINES) -> list[str]:
    """
    Разбиваем текст на чанки по ~max_lines строк.
    Стараемся резать на пустых строках чтобы не разрывать блоки.
    """
    lines = text.splitlines()
    chunks = []
    i = 0
    n = len(lines)

    while i < n:
        end = min(i + max_lines, n)
        # Если конец чанка не на пустой строке — пытаемся доехать до пустой (но не больше +50 строк)
        if end < n:
            search_end = min(end + 50, n)
            for j in range(end, search_end):
                if not lines[j].strip():
                    end = j
                    break
        chunks.append("\n".join(lines[i:end]))
        i = end
    return chunks


async def call_claude(session: aiohttp.ClientSession, chunk_text: str, api_key: str, debug_idx: int = 0) -> list[dict]:
    """Один запрос к Claude API → возвращает список военных из чанка"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": chunk_text}
        ],
    }

    # Повторяем при 429
    for attempt in range(MAX_RETRIES_ON_429):
        async with session.post(API_URL, headers=headers, json=payload) as resp:
            if resp.status == 429:
                # Rate limit — ждём и повторяем
                # Можно прочитать retry-after, но проще фиксированная задержка
                retry_after = resp.headers.get("retry-after")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else RETRY_DELAY
                logger.warning(f"  429 rate limit, жду {wait}s (попытка {attempt + 1}/{MAX_RETRIES_ON_429})")
                await asyncio.sleep(wait)
                continue
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Claude API error {resp.status}: {text[:500]}")
            data = await resp.json()
            break
    else:
        raise Exception(f"Превышено число попыток retry при 429")

    raw = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            raw += block["text"]

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    debug_path = Path(f"parsed/_debug_chunk_{debug_idx}.txt")
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(raw, encoding="utf-8")

    stop_reason = data.get("stop_reason")
    usage = data.get("usage", {})
    logger.info(
        f"  → stop_reason={stop_reason} "
        f"input={usage.get('input_tokens')} output={usage.get('output_tokens')}"
    )

    if stop_reason == "max_tokens":
        logger.warning(f"  ВНИМАНИЕ: ответ оборван на max_tokens — JSON будет битый")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Невалидный JSON, см. {debug_path}")
        raise


async def process_chunk(idx: int, total: int, chunk: str, api_key: str,
                         sem: asyncio.Semaphore, results: list, stats: dict):
    """Обработка одного чанка с rate-limit"""
    async with sem:
        timeout = aiohttp.ClientTimeout(total=180)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                parsed = await call_claude(session, chunk, api_key, debug_idx=idx)
            results.append((idx, parsed))
            stats["ok"] += 1
            logger.info(f"  [{idx + 1}/{total}] получено {len(parsed)} военных")
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"  [{idx + 1}/{total}] ошибка: {e}")
            results.append((idx, []))

        # Задержка перед следующим запросом — бережём rate limit
        if idx + 1 < total:
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)


async def main(input_path: str, output_path: str, max_chunks: int = None):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY не задан в .env")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = chunk_text(text)
    logger.info(f"Файл {input_path}")
    logger.info(f"Размер: {len(text)} символов, строк: {len(text.splitlines())}")
    logger.info(f"Разбит на чанков: {len(chunks)}")

    if max_chunks:
        chunks = chunks[:max_chunks]
        logger.info(f"РЕЖИМ ТЕСТА: обработаем только первые {len(chunks)} чанков")

    stats = {"ok": 0, "errors": 0}
    results = []
    sem = asyncio.Semaphore(PARALLEL)

    tasks = [
        process_chunk(i, len(chunks), chunk, api_key, sem, results, stats)
        for i, chunk in enumerate(chunks)
    ]
    await asyncio.gather(*tasks)

    # Объединяем результаты в исходном порядке
    results.sort(key=lambda x: x[0])
    all_military = []
    for _, parsed in results:
        all_military.extend(parsed)

    # Сохраняем
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_military, f, ensure_ascii=False, indent=2)

    logger.info("=" * 50)
    logger.info(f"Чанков успешно: {stats['ok']} / Ошибок: {stats['errors']}")
    logger.info(f"Распарсено военных: {len(all_military)}")
    total_relatives = sum(len(m.get("relatives", [])) for m in all_military)
    logger.info(f"Распарсено родственников: {total_relatives}")
    logger.info(f"Сохранено в: {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Путь к .txt блокноту")
    parser.add_argument("output", help="Путь к .json файлу для сохранения")
    parser.add_argument("--max-chunks", type=int, default=None,
                        help="Обработать только N первых чанков (для теста)")
    args = parser.parse_args()

    asyncio.run(main(args.input, args.output, args.max_chunks))
