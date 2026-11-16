"""
Разовый прогон определения операторов партиями подряд (без 10-мин паузы бота).
Использует тот же voxlink_enricher.run_once() (BATCH_LIMIT=100, mvpsoll).
Останавливается, когда партия перестаёт что-либо делать или очередь пуста.

Запуск:
  cd /root/projects/tg-parser-bot
  venv/bin/python run_operator_batches.py
"""
import asyncio
from bot.services.voxlink_enricher import run_once

MAX_BATCHES = 40  # запас (40*100 = 4000)

async def main():
    total_upd = 0
    for i in range(1, MAX_BATCHES + 1):
        stats = await run_once()
        upd = stats.get("updated", 0)
        nf  = stats.get("not_found_yet", 0)
        sk  = stats.get("marked_skipped", 0)
        tr  = stats.get("transient", 0)
        total_upd += upd
        print(f"партия {i:2}: updated={upd} not_found={nf} skipped={sk} transient={tr} | всего обновлено={total_upd}")
        # нечего обрабатывать — выходим
        if (upd + nf + sk) == 0:
            print("Очередь пуста или ничего не обрабатывается — стоп.")
            break
        await asyncio.sleep(2)  # маленькая пауза, бережём mvpsoll
    print(f"\nГОТОВО. Операторов проставлено: {total_upd}")

if __name__ == "__main__":
    asyncio.run(main())
