#!/usr/bin/env python3
"""
Этап 1 автоподачи Соне — ручная проверка ядра, БЕЗ фонового цикла и лимитов.

    venv/bin/python -m tools.autofeed_test                 # PREVIEW (read-only): доноры и кандидаты в окне
    venv/bin/python -m tools.autofeed_test --move-one      # перенести РОВНО ОДИН лид (проверка переезда)

Параметры окна/порога: --min-days 3 --max-days 10 --donor-min 20 --target 111
"""
import argparse
import asyncio

from dotenv import load_dotenv
load_dotenv()

from bot.db.queries import autofeed_preview, autofeed_pick_and_move  # noqa: E402


async def do_preview(min_days, max_days, donor_min):
    rows = await autofeed_preview(min_days, max_days, donor_min)
    if not rows:
        print(f"В окне {min_days}-{max_days} дней подходящих лидов нет (dp/ha, заполненные, невыгруженные).")
        return
    print(f"Доноры dp/ha — заполненные невыгруженные лиды в окне {min_days}-{max_days} дней:")
    print(f"{'office':<6} {'mgr':>4} {'manager':<14} {'в окне':>7}  донор_ok(>{donor_min})")
    movable = 0
    donors_ok = 0
    for r in rows:
        cnt = r["eligible_in_window"]
        ok = cnt > donor_min
        if ok:
            donors_ok += 1
            movable += cnt          # сколько всего у пригодных доноров
        flag = "✅" if ok else "—"
        print(f"{r['office']:<6} {r['added_by']:>4} {str(r['manager']):<14} {cnt:>7}  {flag}")
    print(f"\nИтого доноров в окне: {len(rows)}; пригодных (>{donor_min}): {donors_ok}; "
          f"суммарно у пригодных: {movable} лидов.")
    print("PREVIEW — ничего не менялось.")


async def do_move_one(target, min_days, max_days, donor_min):
    moved = await autofeed_pick_and_move(target, min_days, max_days, donor_min)
    if not moved:
        print("Подходящих лидов нет — ничего не перенесено.")
        return
    print("✅ Перенесён ОДИН лид:")
    print(f"   id={moved['id']}  {moved['full_name']}")
    print(f"   от: офис={moved['from_office']} менеджер_id={moved['from_manager']} "
          f"(исходная дата {moved['orig_created']})")
    print(f"   → к Соне (target={target}), office=pvl, created_at=NOW(), маркер extra.autofed проставлен.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--move-one", action="store_true", help="перенести ровно один лид (иначе только preview)")
    ap.add_argument("--status", action="store_true", help="показать дневной счётчик и гейты (read-only)")
    ap.add_argument("--target", type=int, default=111)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--max-days", type=int, default=10)
    ap.add_argument("--donor-min", type=int, default=20)
    args = ap.parse_args()
    if args.status:
        from bot.db.queries import autofeed_should_run
        st = asyncio.run(autofeed_should_run(args.target, 30))
        print("Статус автоподачи (read-only):")
        print(f"  сейчас можно: {st['allowed']} — {st['reason']}")
        print(f"  подано сегодня: {st['today']}/{st['limit']}, час Киева: {st['kyiv_hour']}")
    elif args.move_one:
        asyncio.run(do_move_one(args.target, args.min_days, args.max_days, args.donor_min))
    else:
        asyncio.run(do_preview(args.min_days, args.max_days, args.donor_min))