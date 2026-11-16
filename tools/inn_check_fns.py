#!/usr/bin/env python3
"""
Проверка ИНН по реестру недействительных ИНН физлиц (ФНС).

Ответ без лишних полей  → ИНН в реестре нет, то есть действующий.
Ответ с полями          → попадание, пишем его целиком.

    venv/bin/python tools/inn_check_fns.py
"""
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

SRC = "/root/exports/sonya_inn.txt"
DST = "/root/exports/sonya_inn_check.txt"
PAGE = "https://service.nalog.ru/invalid-inn-fl.html"
API = "https://service.nalog.ru/invalid-inn-proc.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
PAUSE = 1.0

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", UA)]


def check(inn: str) -> dict:
    body = urllib.parse.urlencode({"k": "fl", "inn": inn}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PAGE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    opener.open(PAGE, timeout=30).read()      # забрать сессионную куку

    rows = []
    for line in open(SRC, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        left, _, right = line.rpartition(" — ")
        m = re.search(r"\s(\d{2}\.\d{2}\.\d{4})$", left)
        fio, dr = (left[:m.start()].strip(), m.group(1)) if m else (left.strip(), "")
        rows.append((fio, dr, re.findall(r"\b\d{12}\b", right)))

    total = sum(len(r[2]) for r in rows)
    print(f"Строк: {len(rows)}   ИНН к проверке: {total}")

    out, hits, errs, done = [], 0, 0, 0
    for fio, dr, inns in rows:
        if not inns:
            out.append(f"{fio} {dr} — нет ИНН")
            continue
        parts = []
        for inn in inns:
            done += 1
            try:
                resp = check(inn)
                extra = {k: v for k, v in resp.items() if k != "inn"}
                if extra:
                    hits += 1
                    parts.append(f"{inn}: НЕДЕЙСТВИТЕЛЕН "
                                 f"{json.dumps(extra, ensure_ascii=False)}")
                    print(f"  !! {fio} {inn} → {extra}")
                else:
                    parts.append(f"{inn}: действующий")
            except Exception as e:
                errs += 1
                parts.append(f"{inn}: ошибка ({e})")
                print(f"  ошибка {inn}: {e}")
            print(f"  [{done}/{total}] {inn}")
            time.sleep(PAUSE)
        out.append(f"{fio} {dr} — " + "; ".join(parts))

    with open(DST, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print(f"\nПроверено {done}, попаданий в реестр: {hits}, ошибок: {errs}")
    print(f"Файл: {DST}")


if __name__ == "__main__":
    main()
