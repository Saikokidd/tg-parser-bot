from typing import Optional
from bot.db.connection import get_pool


# ============== МЕНЕДЖЕРЫ ==============

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


async def create_manager(name: str, telegram_id: int, username: str = "") -> dict:
    """Создать нового менеджера с первой привязкой telegram_id"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            manager = await conn.fetchrow(
                "INSERT INTO managers (name) VALUES ($1) RETURNING *",
                name
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
            return False  # telegram_id уже занят


async def list_managers(only_active: bool = True) -> list:
    """Список всех менеджеров с их привязанными telegram_id"""
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
    """Деактивировать менеджера (мягкое удаление)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE managers SET is_active = FALSE WHERE id = $1",
            manager_id
        )


# ============== ЗАПИСИ О ЛЮДЯХ ==============

async def find_duplicates(full_name: str = None, birth_date=None, phone: str = None) -> list:
    """Поиск дублей: совпадение 2 из 3 полей"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.*, m.name as manager_name,
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO persons 
                (full_name, birth_date, phone, combat_mission, missing, callsign, military_unit, added_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM persons WHERE added_by = $1 ORDER BY created_at DESC",
            manager_id
        )
        return [dict(r) for r in rows]