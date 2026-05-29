"""
Все запросы к БД.
Структура:
  МЕНЕДЖЕРЫ          — управление менеджерами и их ТГ-аккаунтами
  ВОЕННЫЕ            — persons_military
  РОДСТВЕННИКИ       — relatives + military_relatives (связки)
"""
from typing import Optional
from bot.db.connection import get_pool


# ════════════════════════════════════════════════════════════
#                       МЕНЕДЖЕРЫ
# ════════════════════════════════════════════════════════════

async def get_manager_by_telegram_id(telegram_id: int) -> Optional[dict]:
    """Найти менеджера по telegram_id (через таблицу привязок)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT m.* FROM managers m
            JOIN manager_telegram_ids mti ON mti.manager_id = m.id
            WHERE mti.telegram_id = $1 AND m.is_active = TRUE
            """,
            telegram_id
        )
        return dict(row) if row else None


async def create_manager(name: str, telegram_id: int, username: str = "", office: str = None) -> dict:
    """
    Создать нового менеджера с первой привязкой telegram_id.
    office: 'pvl' / 'dp' / None (None — пока не назначен, не сможет пробивать).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            manager = await conn.fetchrow(
                "INSERT INTO managers (name, office) VALUES ($1, $2) RETURNING *",
                name, office
            )
            await conn.execute(
                """
                INSERT INTO manager_telegram_ids (manager_id, telegram_id, username)
                VALUES ($1, $2, $3)
                """,
                manager['id'], telegram_id, username
            )
            return dict(manager)


async def add_telegram_id_to_manager(manager_id: int, telegram_id: int, username: str = "") -> bool:
    """Привязать дополнительный telegram_id к существующему менеджеру"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO manager_telegram_ids (manager_id, telegram_id, username)
                VALUES ($1, $2, $3)
                """,
                manager_id, telegram_id, username
            )
            return True
        except Exception:
            return False


async def list_managers(only_active: bool = True) -> list:
    """Список менеджеров с их telegram_id"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE m.is_active = TRUE" if only_active else ""
        rows = await conn.fetch(
            f"""
            SELECT m.id, m.name, m.is_active, m.created_at,
                   COALESCE(
                       array_agg(mti.telegram_id) FILTER (WHERE mti.telegram_id IS NOT NULL),
                       '{{}}'::bigint[]
                   ) as telegram_ids
            FROM managers m
            LEFT JOIN manager_telegram_ids mti ON mti.manager_id = m.id
            {where}
            GROUP BY m.id
            ORDER BY m.name
            """
        )
        return [dict(r) for r in rows]


async def get_manager_by_id(manager_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM managers WHERE id = $1", manager_id)
        return dict(row) if row else None


async def deactivate_manager(manager_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE managers SET is_active = FALSE WHERE id = $1",
            manager_id
        )


async def update_manager_office(manager_id: int, office: str) -> None:
    """
    Поменять офис менеджера ('pvl' / 'dp').
    Все последующие пробивы этого менеджера пойдут через токен нового офиса.
    """
    if office not in ('pvl', 'dp'):
        raise ValueError(f"Неизвестный офис: {office!r}. Допустимо: 'pvl', 'dp'.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE managers SET office = $2 WHERE id = $1",
            manager_id, office
        )


# ════════════════════════════════════════════════════════════
#                       ВОЕННЫЕ
# ════════════════════════════════════════════════════════════

async def find_military_duplicates(full_name: str = None, birth_date=None) -> list:
    """
    Поиск дублей военного.

    Стратегия:
    1. Если есть и ФИО и ДР — ищем точные совпадения ФИО+ДР,
       а также записи с тем же ФИО но БЕЗ ДР (это потенциальный дубль).
    2. Если есть только ФИО (ДР=None) — ищем по ФИО любые записи:
       и без ДР, и с ДР (любой может оказаться этим же человеком).

    Записи с точным совпадением ФИО+ДР идут первыми.
    """
    if not full_name:
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        if birth_date is not None:
            # Есть ДР — ищем строгие дубли + дубли без ДР
            rows = await conn.fetch(
                """
                SELECT pm.*, m.name as manager_name
                FROM persons_military pm
                LEFT JOIN managers m ON pm.added_by = m.id
                WHERE LOWER(pm.full_name) = LOWER($1)
                  AND (pm.birth_date = $2 OR pm.birth_date IS NULL)
                ORDER BY 
                    CASE WHEN pm.birth_date = $2 THEN 0 ELSE 1 END,
                    pm.created_at DESC
                """,
                full_name, birth_date
            )
        else:
            # ДР нет — ищем по ФИО любые записи
            rows = await conn.fetch(
                """
                SELECT pm.*, m.name as manager_name
                FROM persons_military pm
                LEFT JOIN managers m ON pm.added_by = m.id
                WHERE LOWER(pm.full_name) = LOWER($1)
                ORDER BY 
                    CASE WHEN pm.birth_date IS NULL THEN 0 ELSE 1 END,
                    pm.created_at DESC
                """,
                full_name
            )
        return [dict(r) for r in rows]


async def insert_military(data: dict, manager_id: int) -> dict:
    """Вставить нового военного"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO persons_military
                (full_name, birth_date, status, extra, added_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            data.get("full_name"),
            data.get("birth_date"),
            data.get("status"),  # 'killed' / 'missing'
            data.get("extra", {}),
            manager_id
        )
        return dict(row)


async def get_military_by_id(military_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM persons_military WHERE id = $1",
            military_id
        )
        return dict(row) if row else None


async def list_military_by_manager(manager_id: int) -> list:
    """Все военные внесённые менеджером"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM persons_military
            WHERE added_by = $1
            ORDER BY created_at DESC
            """,
            manager_id
        )
        return [dict(r) for r in rows]


async def list_military_without_relatives(manager_id: int = None) -> list:
    """
    Военные у которых нет НИ ОДНОЙ привязки в military_relatives.
    Если manager_id указан — только записи этого менеджера.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if manager_id:
            rows = await conn.fetch(
                """
                SELECT pm.* FROM persons_military pm
                LEFT JOIN military_relatives mr ON mr.military_id = pm.id
                WHERE mr.id IS NULL AND pm.added_by = $1
                ORDER BY pm.created_at DESC
                """,
                manager_id
            )
        else:
            rows = await conn.fetch(
                """
                SELECT pm.* FROM persons_military pm
                LEFT JOIN military_relatives mr ON mr.military_id = pm.id
                WHERE mr.id IS NULL
                ORDER BY pm.created_at DESC
                """
            )
        return [dict(r) for r in rows]



# ════════════════════════════════════════════════════════════
#                       РОДСТВЕННИКИ
# ════════════════════════════════════════════════════════════

async def find_relative_duplicates(full_name: str = None, birth_date=None,
                                    phone: str = None, address: str = None) -> list:
    """
    Дубли родственника: совпадение 2 из 4 (ФИО, ДР, телефон, адрес).
    Адрес сравниваем по нормализованному виду (lower + strip знаков).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, m.name as manager_name,
                   (
                       (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                       (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                       (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                       (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
                   ) as match_count
            FROM relatives r
            LEFT JOIN managers m ON r.added_by = m.id
            WHERE (
                (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
            ) >= 2
            ORDER BY match_count DESC
            """,
            full_name, birth_date, phone, address
        )
        return [dict(r) for r in rows]


async def insert_relative(data: dict, manager_id: int) -> dict:
    """Создать запись родственника"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO relatives
                (full_name, birth_date, phone, address, extra, added_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            data.get("full_name"),
            data.get("birth_date"),
            data.get("phone"),
            data.get("address"),
            data.get("extra", {}),
            manager_id
        )
        return dict(row)


async def link_military_relative(military_id: int, relative_id: int, manager_id: int) -> bool:
    """Привязать родственника к военному. Возвращает True если новая связка."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO military_relatives (military_id, relative_id, added_by)
                VALUES ($1, $2, $3)
                """,
                military_id, relative_id, manager_id
            )
            return True
        except Exception:
            return False  # уже привязан


async def get_relatives_of_military(military_id: int) -> list:
    """Все родственники привязанные к военному"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, mr.created_at as linked_at
            FROM relatives r
            JOIN military_relatives mr ON mr.relative_id = r.id
            WHERE mr.military_id = $1
            ORDER BY mr.created_at DESC
            """,
            military_id
        )
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════
#                       СТАТИСТИКА
# ════════════════════════════════════════════════════════════

async def stats_for_manager(manager_id: int, since=None) -> dict:
    """
    Статистика по одному менеджеру:
      loaded — кол-во военных созданных менеджером (опц. с since)
      filled — кол-во военных с хотя бы одним привязанным родственником
    
    since — datetime начала периода, или None для всего времени.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS loaded,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM military_relatives mr
                            WHERE mr.military_id = pm.id
                        )
                    ) AS filled
                FROM persons_military pm
                WHERE pm.added_by = $1 AND pm.created_at >= $2
                """,
                manager_id, since
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS loaded,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM military_relatives mr
                            WHERE mr.military_id = pm.id
                        )
                    ) AS filled
                FROM persons_military pm
                WHERE pm.added_by = $1
                """,
                manager_id
            )
        return {
            "loaded": row["loaded"] or 0,
            "filled": row["filled"] or 0,
        }


async def stats_for_all_managers(since=None) -> list:
    """
    Статистика по всем активным менеджерам.
    Возвращает список: [{manager_id, name, loaded, filled}, ...]
    Сортировка: по убыванию loaded.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """
                SELECT
                    m.id AS manager_id,
                    m.name,
                    COUNT(pm.id) AS loaded,
                    COUNT(pm.id) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM military_relatives mr
                            WHERE mr.military_id = pm.id
                        )
                    ) AS filled
                FROM managers m
                LEFT JOIN persons_military pm
                    ON pm.added_by = m.id AND pm.created_at >= $1
                WHERE m.is_active = TRUE
                GROUP BY m.id, m.name
                ORDER BY loaded DESC, m.name
                """,
                since
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    m.id AS manager_id,
                    m.name,
                    COUNT(pm.id) AS loaded,
                    COUNT(pm.id) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM military_relatives mr
                            WHERE mr.military_id = pm.id
                        )
                    ) AS filled
                FROM managers m
                LEFT JOIN persons_military pm ON pm.added_by = m.id
                WHERE m.is_active = TRUE
                GROUP BY m.id, m.name
                ORDER BY loaded DESC, m.name
                """
            )
        return [dict(r) for r in rows]
    
    
# ════════════════════════════════════════════════════════════
#                       СПИСОК ЛИДОВ (с пагинацией)
# ════════════════════════════════════════════════════════════

async def list_military_paginated(
    manager_id: int = None,
    page: int = 1,
    page_size: int = 20
) -> tuple[list, int]:
    """
    Список ВСЕХ военных с пагинацией.
    Если manager_id указан — только записи этого менеджера, иначе все.
    
    Возвращает кортеж (records, total_count).
    """
    pool = await get_pool()
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        if manager_id:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM persons_military WHERE added_by = $1",
                manager_id
            )
            rows = await conn.fetch(
                """
                SELECT pm.*,
                       (SELECT COUNT(*) FROM military_relatives mr
                        WHERE mr.military_id = pm.id) AS relatives_count
                FROM persons_military pm
                WHERE pm.added_by = $1
                ORDER BY pm.created_at DESC
                LIMIT $2 OFFSET $3
                """,
                manager_id, page_size, offset
            )
        else:
            total = await conn.fetchval("SELECT COUNT(*) FROM persons_military")
            rows = await conn.fetch(
                """
                SELECT pm.*,
                       (SELECT COUNT(*) FROM military_relatives mr
                        WHERE mr.military_id = pm.id) AS relatives_count
                FROM persons_military pm
                ORDER BY pm.created_at DESC
                LIMIT $1 OFFSET $2
                """,
                page_size, offset
            )
        return [dict(r) for r in rows], (total or 0)


# ════════════════════════════════════════════════════════════
#                       УДАЛЕНИЕ
# ════════════════════════════════════════════════════════════

async def delete_military_cascade(military_id: int) -> int:
    """
    Удалить военного. Каскадно удалит связки в military_relatives.
    Также удаляет родственников которые были связаны ТОЛЬКО с этим военным
    (если родственник связан и с другими — оставляем).
    
    Возвращает количество удалённых родственников.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Находим родственников, привязанных только к этому военному
            orphan_ids = await conn.fetch(
                """
                SELECT r.id FROM relatives r
                WHERE EXISTS (
                    SELECT 1 FROM military_relatives mr
                    WHERE mr.relative_id = r.id AND mr.military_id = $1
                )
                AND NOT EXISTS (
                    SELECT 1 FROM military_relatives mr
                    WHERE mr.relative_id = r.id AND mr.military_id != $1
                )
                """,
                military_id
            )
            orphan_ids_list = [r['id'] for r in orphan_ids]

            # Удаляем военного — связки в military_relatives удалятся каскадно (FK ON DELETE CASCADE)
            await conn.execute("DELETE FROM persons_military WHERE id = $1", military_id)

            # Удаляем "осиротевших" родственников
            if orphan_ids_list:
                await conn.execute(
                    "DELETE FROM relatives WHERE id = ANY($1::int[])",
                    orphan_ids_list
                )

            return len(orphan_ids_list)


async def delete_relative_cascade(relative_id: int) -> None:
    """
    Удалить родственника полностью.
    Связки в military_relatives удалятся каскадно (ON DELETE CASCADE).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM relatives WHERE id = $1", relative_id)


# ════════════════════════════════════════════════════════════
#                       РЕДАКТИРОВАНИЕ РОДСТВЕННИКА
# ════════════════════════════════════════════════════════════

async def get_relative_by_id(relative_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM relatives WHERE id = $1", relative_id)
        return dict(row) if row else None


async def update_relative_field(relative_id: int, field: str, value) -> None:
    """
    Обновить структурное поле родственника (full_name, birth_date, phone, address).
    """
    allowed = {"full_name", "birth_date", "phone", "address"}
    if field not in allowed:
        raise ValueError(f"Поле {field} не разрешено для прямого обновления")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE relatives SET {field} = $1, updated_at = NOW() WHERE id = $2",
            value, relative_id
        )


async def update_relative_extra(relative_id: int, key: str, value) -> None:
    """
    Обновить/добавить поле в JSONB extra.
    Если value = None или '' — удаляет ключ из extra.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if value is None or value == "":
            await conn.execute(
                "UPDATE relatives SET extra = extra - $2, updated_at = NOW() WHERE id = $1",
                relative_id, key
            )
        else:
            await conn.execute(
                """
                UPDATE relatives
                SET extra = jsonb_set(COALESCE(extra, '{}'::jsonb), ARRAY[$2], to_jsonb($3::text)),
                    updated_at = NOW()
                WHERE id = $1
                """,
                relative_id, key, str(value)
            )


# ════════════════════════════════════════════════════════════
#       ДУБЛИ РОДСТВЕННИКА С ИНФОЙ К КОМУ ПРИВЯЗАН
# ════════════════════════════════════════════════════════════

async def find_relative_duplicates_with_links(
    full_name: str = None, birth_date=None,
    phone: str = None, address: str = None
) -> list:
    """
    Дубли родственника + список военных к которым он уже привязан.
    Возвращает список dict с полем linked_to (список словарей с инфой о военных).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*,
                   mgr.name as manager_name,
                   (
                       (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                       (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                       (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                       (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
                   ) as match_count,
                   (
                       SELECT COALESCE(
                           json_agg(
                               json_build_object(
                                   'id', pm.id,
                                   'full_name', pm.full_name,
                                   'birth_date', pm.birth_date::text,
                                   'status', pm.status
                               )
                           ),
                           '[]'::json
                       )
                       FROM military_relatives mr
                       JOIN persons_military pm ON pm.id = mr.military_id
                       WHERE mr.relative_id = r.id
                   ) as linked_to
            FROM relatives r
            LEFT JOIN managers mgr ON r.added_by = mgr.id
            WHERE (
                (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
            ) >= 2
            ORDER BY match_count DESC
            """,
            full_name, birth_date, phone, address
        )
        result = []
        for r in rows:
            d = dict(r)
            # linked_to уже декодируется asyncpg в list[dict] через json_codec
            # но если пришло строкой — парсим
            import json as _json
            if isinstance(d.get('linked_to'), str):
                d['linked_to'] = _json.loads(d['linked_to'])
            result.append(d)
        return result


# ════════════════════════════════════════════════════════════
#                       ЭКСПОРТ
# ════════════════════════════════════════════════════════════

async def count_available_for_export(manager_id: int = None) -> int:
    """
    Сколько военных доступно для экспорта.
    Условия:
    - Есть хотя бы один привязанный родственник
    - exported_at IS NULL (ещё не выгружали)
    - Если manager_id указан — только записи этого менеджера
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if manager_id:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM persons_military pm
                WHERE pm.exported_at IS NULL
                  AND pm.added_by = $1
                  AND EXISTS (
                      SELECT 1 FROM military_relatives mr
                      WHERE mr.military_id = pm.id
                  )
                """,
                manager_id
            )
        else:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM persons_military pm
                WHERE pm.exported_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM military_relatives mr
                      WHERE mr.military_id = pm.id
                  )
                """
            )


async def fetch_military_for_export(manager_id: int = None, limit: int = None) -> list:
    """
    Военные с заполненными родственниками, не выгруженные ранее.
    Сортировка: сначала старые (FIFO).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        params = []
        where = ["pm.exported_at IS NULL",
                 "EXISTS (SELECT 1 FROM military_relatives mr WHERE mr.military_id = pm.id)"]

        if manager_id:
            where.append(f"pm.added_by = ${len(params) + 1}")
            params.append(manager_id)

        sql = f"""
            SELECT pm.*, m.name AS manager_name
            FROM persons_military pm
            LEFT JOIN managers m ON pm.added_by = m.id
            WHERE {' AND '.join(where)}
            ORDER BY pm.created_at ASC
        """
        if limit:
            sql += f" LIMIT ${len(params) + 1}"
            params.append(limit)

        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def fetch_relatives_for_military_ids(military_ids: list[int]) -> list:
    """
    Все родственники привязанные к указанным военным +
    к каким военным они привязаны (с учётом m2m).
    
    Возвращает: список dict с полем linked_military
    (список dict {id, full_name, birth_date}) — все военные родственника,
    включая тех, что не входят в military_ids.
    """
    if not military_ids:
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (r.id) r.*,
                   mgr.name AS manager_name,
                   (
                       SELECT COALESCE(
                           json_agg(
                               json_build_object(
                                   'id', pm2.id,
                                   'full_name', pm2.full_name,
                                   'birth_date', pm2.birth_date::text
                               )
                           ),
                           '[]'::json
                       )
                       FROM military_relatives mr2
                       JOIN persons_military pm2 ON pm2.id = mr2.military_id
                       WHERE mr2.relative_id = r.id
                   ) AS linked_military
            FROM relatives r
            LEFT JOIN managers mgr ON r.added_by = mgr.id
            JOIN military_relatives mr ON mr.relative_id = r.id
            WHERE mr.military_id = ANY($1::int[])
            ORDER BY r.id, r.created_at ASC
            """,
            military_ids
        )
        result = []
        import json as _json
        for r in rows:
            d = dict(r)
            if isinstance(d.get('linked_military'), str):
                d['linked_military'] = _json.loads(d['linked_military'])
            result.append(d)
        return result


async def mark_military_exported(military_ids: list[int]) -> None:
    """Пометить военных как выгруженных (exported_at = NOW())"""
    if not military_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE persons_military SET exported_at = NOW() WHERE id = ANY($1::int[])",
            military_ids
        )


async def update_military_extra_field(military_id: int, key: str, value: str) -> None:
    """
    Обновить одно поле в extra JSONB у военного.
    Если поля нет — добавит, если есть — перезапишет.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE persons_military
            SET extra = COALESCE(extra, '{}'::jsonb) || jsonb_build_object($2::text, $3::text),
                updated_at = NOW()
            WHERE id = $1
            """,
            military_id, key, value
        )
        
        
async def find_birth_date_by_name(full_name: str) -> "date | None":
    """
    Найти в БД дату рождения человека с таким же ФИО.
    Ищем сначала среди родственников, потом среди военных.
    Возвращаем первую найденную ДР или None.
    """
    if not full_name:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Сначала родственники
        row = await conn.fetchrow(
            """
            SELECT birth_date
            FROM relatives
            WHERE LOWER(full_name) = LOWER($1) AND birth_date IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            full_name
        )
        if row and row["birth_date"]:
            return row["birth_date"]

        # Потом военные
        row = await conn.fetchrow(
            """
            SELECT birth_date
            FROM persons_military
            WHERE LOWER(full_name) = LOWER($1) AND birth_date IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            full_name
        )
        if row and row["birth_date"]:
            return row["birth_date"]

    return None


# ════════════════════════════════════════════════════════════
#                       ЛОГ ПРОБИВОВ
# ════════════════════════════════════════════════════════════

async def insert_probiv_log(
    provider: str,
    context: str,
    manager_id: int | None = None,
    full_name: str | None = None,
    birth_date=None,
    cost: float = 0,
    currency: str = "USD",
    military_id: int | None = None,
    success: bool = True,
    error: str | None = None,
    office: str | None = None,
) -> None:
    """
    Записать факт запроса к провайдеру пробива.

    Вызывается всегда — даже при ошибках провайдера (за них часто платим).
    Не бросает исключений наверх — учёт расходов не должен ломать основной флоу.

    Args:
        provider: 'sauron' / 'kody' / ...
        context: 'auto' (автопробив лида) / 'next' (Пробить далее) /
                 'tool' (скрипты) / 'other'
        manager_id: ID менеджера или None для админских прогонов через tools/
        full_name: ФИО пробиваемого
        birth_date: ДР пробиваемого (date или None)
        cost: стоимость запроса в валюте provider
        currency: валюта (по умолчанию USD — как у Sauron)
        military_id: ID военного, если пробив был сделан в его контексте
        success: успешен ли запрос
        error: текст ошибки если success=False (обрезается до 255 символов)
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO probiv_log (
                    manager_id, provider, full_name, birth_date,
                    cost, currency, context, military_id,
                    success, error, office
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                manager_id,
                provider,
                full_name,
                birth_date,
                cost,
                currency,
                context,
                military_id,
                success,
                (error or "")[:255] if error else None,
                office,
            )
    except Exception:
        # Сознательно подавляем любые ошибки логирования —
        # учёт расходов не должен ронять пробив для пользователя.
        import logging
        logging.getLogger(__name__).exception("insert_probiv_log failed")
        
        
# ════════════════════════════════════════════════════════════
#                СТАТИСТИКА РАСХОДА НА ПРОБИВ
# ════════════════════════════════════════════════════════════

async def cost_stats_total(since=None) -> dict:
    """
    Сводный отчёт по ВСЕМ запросам к провайдерам пробива.
    Включает все contexts ('auto', 'next', 'tool', 'other')
    и привязанные/непривязанные к менеджеру записи.

    Args:
        since: datetime начала периода. None = всё время.

    Returns:
        dict с полями:
          total_count, total_cost,
          auto_count, auto_cost,
          next_count, next_cost,
          tool_count, tool_cost,
          failed_count, failed_cost
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_count,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(*) FILTER (WHERE context = 'auto') AS auto_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'auto'), 0) AS auto_cost,
                    COUNT(*) FILTER (WHERE context = 'next') AS next_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'next'), 0) AS next_cost,
                    COUNT(*) FILTER (WHERE context = 'tool') AS tool_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'tool'), 0) AS tool_cost,
                    COUNT(*) FILTER (WHERE success = FALSE) AS failed_count,
                    COALESCE(SUM(cost) FILTER (WHERE success = FALSE), 0) AS failed_cost
                FROM probiv_log
                WHERE created_at >= $1
                """,
                since,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_count,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(*) FILTER (WHERE context = 'auto') AS auto_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'auto'), 0) AS auto_cost,
                    COUNT(*) FILTER (WHERE context = 'next') AS next_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'next'), 0) AS next_cost,
                    COUNT(*) FILTER (WHERE context = 'tool') AS tool_count,
                    COALESCE(SUM(cost) FILTER (WHERE context = 'tool'), 0) AS tool_cost,
                    COUNT(*) FILTER (WHERE success = FALSE) AS failed_count,
                    COALESCE(SUM(cost) FILTER (WHERE success = FALSE), 0) AS failed_cost
                FROM probiv_log
                """
            )
        return dict(row)


async def cost_stats_by_manager(since=None) -> list[dict]:
    """
    Расходы по менеджерам, отсортированные по убыванию суммы.
    Включает только записи с manager_id IS NOT NULL.

    Возвращает список:
      [{manager_id, name, is_active, total_count, total_cost,
        auto_count, auto_cost, next_count, next_cost}, ...]
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """
                SELECT
                    m.id AS manager_id,
                    m.name,
                    m.is_active,
                    COUNT(pl.id) AS total_count,
                    COALESCE(SUM(pl.cost), 0) AS total_cost,
                    COUNT(pl.id) FILTER (WHERE pl.context = 'auto') AS auto_count,
                    COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'auto'), 0) AS auto_cost,
                    COUNT(pl.id) FILTER (WHERE pl.context = 'next') AS next_count,
                    COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'next'), 0) AS next_cost
                FROM probiv_log pl
                JOIN managers m ON m.id = pl.manager_id
                WHERE pl.created_at >= $1
                GROUP BY m.id, m.name, m.is_active
                HAVING COUNT(pl.id) > 0
                ORDER BY total_cost DESC, m.name
                """,
                since,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    m.id AS manager_id,
                    m.name,
                    m.is_active,
                    COUNT(pl.id) AS total_count,
                    COALESCE(SUM(pl.cost), 0) AS total_cost,
                    COUNT(pl.id) FILTER (WHERE pl.context = 'auto') AS auto_count,
                    COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'auto'), 0) AS auto_cost,
                    COUNT(pl.id) FILTER (WHERE pl.context = 'next') AS next_count,
                    COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'next'), 0) AS next_cost
                FROM probiv_log pl
                JOIN managers m ON m.id = pl.manager_id
                GROUP BY m.id, m.name, m.is_active
                HAVING COUNT(pl.id) > 0
                ORDER BY total_cost DESC, m.name
                """
            )
        return [dict(r) for r in rows]


async def cost_stats_no_attach(since=None) -> dict:
    """
    Расходы НЕ привязанные к менеджеру (manager_id IS NULL).
    Обычно это запуски tools/enrich_missing_data.py через cron/админа.

    Returns:
        dict с полями: total_count, total_cost,
                       failed_count, failed_cost.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_count,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(*) FILTER (WHERE success = FALSE) AS failed_count,
                    COALESCE(SUM(cost) FILTER (WHERE success = FALSE), 0) AS failed_cost
                FROM probiv_log
                WHERE manager_id IS NULL AND created_at >= $1
                """,
                since,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_count,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(*) FILTER (WHERE success = FALSE) AS failed_count,
                    COALESCE(SUM(cost) FILTER (WHERE success = FALSE), 0) AS failed_cost
                FROM probiv_log
                WHERE manager_id IS NULL
                """
            )
        return dict(row)
    
    
# ════════════════════════════════════════════════════════════
#       МУЛЬТИ-ОФИСНОСТЬ — ЭТАП B
#
# Здесь живут функции которые знают про office.
# Старые функции (find_military_duplicates, find_relative_duplicates*,
# list_military_paginated, list_military_without_relatives) пока
# оставлены для backward compat — будут удалены в этапе B3
# после того как все хендлеры переведены на новые.
# ════════════════════════════════════════════════════════════


# ──────────── Глобальная дубль-детекция ────────────

async def find_military_global_dup(full_name: str, birth_date=None) -> Optional[dict]:
    """
    Глобальный дубль-чек военного по всей БД (игнорирует office).

    Логика:
    - Если ДР указана — точное совпадение ФИО+ДР, ИЛИ ФИО без ДР (потенциальный дубль)
    - Если ДР не указана — совпадение по ФИО среди записей без ДР

    Возвращает dict с инфой о найденном дубле или None.
    Поля результата: id, full_name, birth_date, office, manager_name, added_by.

    Если дублей несколько — возвращает первый по приоритету:
    1. Точное совпадение ФИО+ДР
    2. Совпадение ФИО без ДР
    """
    if not full_name:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        if birth_date is not None:
            row = await conn.fetchrow(
                """
                SELECT pm.id, pm.full_name, pm.birth_date, pm.office,
                       pm.added_by, m.name AS manager_name
                FROM persons_military pm
                LEFT JOIN managers m ON m.id = pm.added_by
                WHERE LOWER(pm.full_name) = LOWER($1)
                  AND (pm.birth_date = $2 OR pm.birth_date IS NULL)
                ORDER BY
                    CASE WHEN pm.birth_date = $2 THEN 0 ELSE 1 END,
                    pm.created_at ASC
                LIMIT 1
                """,
                full_name, birth_date
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT pm.id, pm.full_name, pm.birth_date, pm.office,
                       pm.added_by, m.name AS manager_name
                FROM persons_military pm
                LEFT JOIN managers m ON m.id = pm.added_by
                WHERE LOWER(pm.full_name) = LOWER($1)
                ORDER BY
                    CASE WHEN pm.birth_date IS NULL THEN 0 ELSE 1 END,
                    pm.created_at ASC
                LIMIT 1
                """,
                full_name
            )
        return dict(row) if row else None


async def find_relative_global_dup(
    full_name: str = None, birth_date=None,
    phone: str = None, address: str = None,
) -> Optional[dict]:
    """
    Глобальный дубль-чек родственника по всей БД (игнорирует office).
    Дубль = совпадение 2 из 4 (ФИО, ДР, телефон, адрес).

    Возвращает первый по релевантности (наибольшее число совпадений).
    Поля: id, full_name, birth_date, phone, address, office, added_by, manager_name.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.id, r.full_name, r.birth_date, r.phone, r.address,
                   r.office, r.added_by, m.name AS manager_name,
                   (
                       (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                       (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                       (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                       (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
                   ) AS match_count
            FROM relatives r
            LEFT JOIN managers m ON m.id = r.added_by
            WHERE (
                (CASE WHEN $1::text IS NOT NULL AND LOWER(r.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                (CASE WHEN $2::date IS NOT NULL AND r.birth_date = $2 THEN 1 ELSE 0 END) +
                (CASE WHEN $3::text IS NOT NULL AND r.phone = $3 THEN 1 ELSE 0 END) +
                (CASE WHEN $4::text IS NOT NULL AND LOWER(r.address) = LOWER($4) THEN 1 ELSE 0 END)
            ) >= 2
            ORDER BY match_count DESC, r.created_at ASC
            LIMIT 1
            """,
            full_name, birth_date, phone, address
        )
        return dict(row) if row else None


# ──────────── Вставка с автоматическим office ────────────

async def insert_military_v2(data: dict, manager_id: int) -> dict:
    """
    Вставка военного с автоматическим определением office.

    Office берётся из manager.office создателя. Если у менеджера office IS NULL —
    запись пройдёт с office=NULL (но такие менеджеры не должны вообще доходить
    до пробива — у них нет Sauron-токена).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Получаем office создателя одним запросом-вставкой
        row = await conn.fetchrow(
            """
            INSERT INTO persons_military
                (full_name, birth_date, status, extra, added_by, office)
            VALUES (
                $1, $2, $3, $4, $5,
                (SELECT office FROM managers WHERE id = $5)
            )
            RETURNING *
            """,
            data.get("full_name"),
            data.get("birth_date"),
            data.get("status"),
            data.get("extra", {}),
            manager_id,
        )
        return dict(row)


async def insert_relative_v2(data: dict, manager_id: int) -> dict:
    """Вставка родственника с автоматическим office из manager.office создателя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO relatives
                (full_name, birth_date, phone, address, extra, added_by, office)
            VALUES (
                $1, $2, $3, $4, $5, $6,
                (SELECT office FROM managers WHERE id = $6)
            )
            RETURNING *
            """,
            data.get("full_name"),
            data.get("birth_date"),
            data.get("phone"),
            data.get("address"),
            data.get("extra", {}),
            manager_id,
        )
        return dict(row)


# ──────────── Office-aware листинги ────────────

async def list_military_paginated_v2(
    manager_id: int = None,
    office_filter: str = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list, int]:
    """
    Список военных с пагинацией. Office-aware.

    Логика фильтров:
    - manager_id (для роли manager) → видит только свои
    - office_filter (для office_admin/supervisor) → весь офис
    - оба None (для super_admin) → всё

    Если оба указаны — применяются оба (на всякий случай для будущих сценариев).
    """
    pool = await get_pool()
    offset = (page - 1) * page_size

    where_parts = []
    params = []
    if manager_id is not None:
        where_parts.append(f"pm.added_by = ${len(params) + 1}")
        params.append(manager_id)
    if office_filter is not None:
        where_parts.append(f"pm.office = ${len(params) + 1}")
        params.append(office_filter)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM persons_military pm {where_sql}",
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT pm.*,
                   (SELECT COUNT(*) FROM military_relatives mr
                    WHERE mr.military_id = pm.id) AS relatives_count
            FROM persons_military pm
            {where_sql}
            ORDER BY pm.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params, page_size, offset,
        )
        return [dict(r) for r in rows], (total or 0)


async def list_military_without_relatives_v2(
    manager_id: int = None,
    office_filter: str = None,
) -> list:
    """
    Военные без привязанных родственников. Office-aware.
    Логика фильтров такая же как у list_military_paginated_v2.
    """
    where_parts = ["mr.id IS NULL"]
    params = []
    if manager_id is not None:
        where_parts.append(f"pm.added_by = ${len(params) + 1}")
        params.append(manager_id)
    if office_filter is not None:
        where_parts.append(f"pm.office = ${len(params) + 1}")
        params.append(office_filter)

    where_sql = "WHERE " + " AND ".join(where_parts)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT pm.* FROM persons_military pm
            LEFT JOIN military_relatives mr ON mr.military_id = pm.id
            {where_sql}
            ORDER BY pm.created_at DESC
            """,
            *params,
        )
        return [dict(r) for r in rows]


async def get_military_by_id_office_check(military_id: int, office_filter: str = None) -> Optional[dict]:
    """
    Получить военного по id, но только если он в указанном офисе.
    office_filter=None → без фильтра (super_admin).

    Возвращает None если военного нет ИЛИ он в чужом офисе.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if office_filter is None:
            row = await conn.fetchrow(
                "SELECT * FROM persons_military WHERE id = $1",
                military_id,
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM persons_military WHERE id = $1 AND office = $2",
                military_id, office_filter,
            )
        return dict(row) if row else None