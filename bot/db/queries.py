from bot.db.connection import get_pool


async def get_or_create_manager(telegram_id: int, username: str, full_name: str) -> dict:
    """Получить или создать менеджера по telegram_id"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM managers WHERE telegram_id = $1", telegram_id
        )
        if not row:
            row = await conn.fetchrow(
                """
                INSERT INTO managers (telegram_id, username, full_name)
                VALUES ($1, $2, $3)
                RETURNING *
                """,
                telegram_id, username, full_name
            )
        return dict(row)


async def find_duplicates(full_name: str = None, birth_date=None, phone: str = None) -> list:
    """
    Ищет записи где совпадают 2 из 3 полей: ФИО, дата рождения, телефон
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.*, m.username as manager_username, m.full_name as manager_name,
                   (
                       (CASE WHEN $1::text IS NOT NULL AND LOWER(p.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                       (CASE WHEN $2::date IS NOT NULL AND p.birth_date = $2 THEN 1 ELSE 0 END) +
                       (CASE WHEN $3::text IS NOT NULL AND p.phone = $3 THEN 1 ELSE 0 END)
                   ) as match_count
            FROM persons p
            LEFT JOIN managers m ON p.added_by = m.id
            WHERE (
                (CASE WHEN $1::text IS NOT NULL AND LOWER(p.full_name) = LOWER($1) THEN 1 ELSE 0 END) +
                (CASE WHEN $2::date IS NOT NULL AND p.birth_date = $2 THEN 1 ELSE 0 END) +
                (CASE WHEN $3::text IS NOT NULL AND p.phone = $3 THEN 1 ELSE 0 END)
            ) >= 2
            ORDER BY match_count DESC
            """,
            full_name, birth_date, phone
        )
        return [dict(r) for r in rows]


async def insert_person(data: dict, manager_id: int) -> dict:
    """Вставить новую запись о человеке"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO persons 
                (full_name, birth_date, phone, combat_mission, missing, callsign, military_unit, added_by)
            VALUES 
                ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            data.get("full_name"),
            data.get("birth_date"),
            data.get("phone"),
            data.get("combat_mission"),
            data.get("missing", False),
            data.get("callsign"),
            data.get("military_unit"),
            manager_id
        )
        return dict(row)


async def get_manager_persons(manager_id: int) -> list:
    """Получить все записи менеджера"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM persons WHERE added_by = $1 ORDER BY created_at DESC",
            manager_id
        )
        return [dict(r) for r in rows]
