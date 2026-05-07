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


# ════════════════════════════════════════════════════════════
#                       ВОЕННЫЕ
# ════════════════════════════════════════════════════════════

async def find_military_duplicates(full_name: str = None, birth_date=None) -> list:
    """Поиск дублей военного по ФИО + ДР (нужно совпадение обоих)"""
    if not full_name and not birth_date:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pm.*, m.name as manager_name
            FROM persons_military pm
            LEFT JOIN managers m ON pm.added_by = m.id
            WHERE LOWER(pm.full_name) = LOWER($1) AND pm.birth_date = $2
            ORDER BY pm.created_at DESC
            """,
            full_name, birth_date
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
            VALUES ($1, $2, $3::military_status, $4, $5)
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
    """Военные по которым ещё не собраны родственники (relatives_collected = FALSE).
    Если manager_id указан — только его записи."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if manager_id:
            rows = await conn.fetch(
                """
                SELECT * FROM persons_military
                WHERE relatives_collected = FALSE AND added_by = $1
                ORDER BY created_at DESC
                """,
                manager_id
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM persons_military
                WHERE relatives_collected = FALSE
                ORDER BY created_at DESC
                """
            )
        return [dict(r) for r in rows]


async def mark_relatives_collected(military_id: int, value: bool = True) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE persons_military SET relatives_collected = $1, updated_at = NOW() WHERE id = $2",
            value, military_id
        )


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
