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


async def list_managers(only_active: bool = True, office_filter: str | None = None) -> list:
    """
    Список менеджеров с их telegram_id и офисом.

    office_filter: если задан ('pvl' / 'dp' / 'ha') — вернёт только менеджеров
                   этого офиса. None — вернёт всех (для super_admin).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params: list = []
        if only_active:
            conditions.append("m.is_active = TRUE")
        if office_filter is not None:
            params.append(office_filter)
            conditions.append(f"m.office = ${len(params)}")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        rows = await conn.fetch(
            f"""
            SELECT m.id, m.name, m.office, m.role, m.is_active, m.created_at,
                   COALESCE(
                       array_agg(mti.telegram_id) FILTER (WHERE mti.telegram_id IS NOT NULL),
                       '{{}}'::bigint[]
                   ) as telegram_ids
            FROM managers m
            LEFT JOIN manager_telegram_ids mti ON mti.manager_id = m.id
            {where}
            GROUP BY m.id
            ORDER BY m.office NULLS LAST, m.name
            """,
            *params
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
    Поменять офис менеджера ('pvl' / 'dp' / 'ha').
    Все последующие пробивы этого менеджера пойдут через токен нового офиса.
    """
    if office not in ('pvl', 'dp', 'ha'):
        raise ValueError(f"Неизвестный офис: {office!r}. Допустимо: 'pvl', 'dp', 'ha'.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE managers SET office = $2 WHERE id = $1",
            manager_id, office
        )


# ════════════════════════════════════════════════════════════
#                       ВОЕННЫЕ
# ════════════════════════════════════════════════════════════

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


async def stats_for_all_managers(since=None, office_filter: str | None = None) -> list:
    """
    Статистика по всем активным менеджерам.
    Возвращает список: [{manager_id, name, loaded, filled}, ...]
    Сортировка: по убыванию loaded.

    office_filter: если задан ('pvl'/'dp'/'ha') — только менеджеры этого офиса.
                   None — все офисы (для super_admin).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Собираем WHERE и параметры динамически
        params: list = []
        office_cond = ""
        if office_filter is not None:
            params.append(office_filter)
            office_cond = f" AND m.office = ${len(params)}"

        if since:
            params.append(since)
            since_param = f"${len(params)}"
            rows = await conn.fetch(
                f"""
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
                    ON pm.added_by = m.id AND pm.created_at >= {since_param}
                WHERE m.is_active = TRUE{office_cond}
                GROUP BY m.id, m.name
                ORDER BY loaded DESC, m.name
                """,
                *params
            )
        else:
            rows = await conn.fetch(
                f"""
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
                WHERE m.is_active = TRUE{office_cond}
                GROUP BY m.id, m.name
                ORDER BY loaded DESC, m.name
                """,
                *params
            )
        return [dict(r) for r in rows]
    
    
# ════════════════════════════════════════════════════════════
#                       СПИСОК ЛИДОВ (с пагинацией)
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

async def cost_stats_total(since=None, office_filter: str | None = None) -> dict:
    """
    Сводный отчёт по ВСЕМ запросам к провайдерам пробива.
    Включает все contexts ('auto', 'next', 'tool', 'other')
    и привязанные/непривязанные к менеджеру записи.

    office_filter: если задан — считаем только запросы менеджеров этого офиса
                   (через JOIN на managers по текущему офису менеджера).
                   None — вся система (super_admin).

    Примечание: фильтр идёт по m.office (текущий офис менеджера), а не
    pl.office, т.к. исторические записи (до миграции 10) имеют pl.office=NULL.
    При office_filter записи без менеджера (cron/enricher) не попадают в выборку.

    Returns:
        dict с полями:
          total_count, total_cost, auto_count, auto_cost,
          next_count, next_cost, tool_count, tool_cost,
          failed_count, failed_cost
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        params: list = []
        conditions: list = []

        # Если фильтруем по офису — нужен JOIN на managers
        if office_filter is not None:
            join_clause = "JOIN managers m ON m.id = pl.manager_id"
            params.append(office_filter)
            conditions.append(f"m.office = ${len(params)}")
        else:
            join_clause = ""

        if since:
            params.append(since)
            conditions.append(f"pl.created_at >= ${len(params)}")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(pl.cost), 0) AS total_cost,
                COUNT(*) FILTER (WHERE pl.context = 'auto') AS auto_count,
                COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'auto'), 0) AS auto_cost,
                COUNT(*) FILTER (WHERE pl.context = 'next') AS next_count,
                COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'next'), 0) AS next_cost,
                COUNT(*) FILTER (WHERE pl.context = 'tool') AS tool_count,
                COALESCE(SUM(pl.cost) FILTER (WHERE pl.context = 'tool'), 0) AS tool_cost,
                COUNT(*) FILTER (WHERE pl.success = FALSE) AS failed_count,
                COALESCE(SUM(pl.cost) FILTER (WHERE pl.success = FALSE), 0) AS failed_cost
            FROM probiv_log pl
            {join_clause}
            {where}
            """,
            *params
        )
        return dict(row)


async def cost_stats_by_manager(since=None, office_filter: str | None = None) -> list[dict]:
    """
    Расходы по менеджерам, отсортированные по убыванию суммы.
    Включает только записи с manager_id IS NOT NULL.

    office_filter: если задан — только менеджеры этого офиса (по m.office).

    Возвращает список:
      [{manager_id, name, is_active, total_count, total_cost,
        auto_count, auto_cost, next_count, next_cost}, ...]
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        params: list = []
        conditions: list = []

        if office_filter is not None:
            params.append(office_filter)
            conditions.append(f"m.office = ${len(params)}")

        if since:
            params.append(since)
            conditions.append(f"pl.created_at >= ${len(params)}")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        rows = await conn.fetch(
            f"""
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
            {where}
            GROUP BY m.id, m.name, m.is_active
            HAVING COUNT(pl.id) > 0
            ORDER BY total_cost DESC, m.name
            """,
            *params
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
    
    
async def move_manager_with_data_to_office(manager_id: int, new_office: str) -> dict:
    """
    Полный перенос менеджера в другой офис: обновляет managers.office,
    persons_military.office (по added_by) и relatives.office (по added_by).
    Всё в одной транзакции — либо всё, либо ничего.

    Возвращает словарь со счётчиками: {'military_moved': N, 'relatives_moved': N}.
    """
    if new_office not in ('pvl', 'dp', 'ha'):
        raise ValueError(f"Неизвестный офис: {new_office!r}.")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE managers SET office = $1 WHERE id = $2",
                new_office, manager_id
            )
            mil_count = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE persons_military SET office = $1
                    WHERE added_by = $2 AND (office IS DISTINCT FROM $1)
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
                """,
                new_office, manager_id
            )
            rel_count = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE relatives SET office = $1
                    WHERE added_by = $2 AND (office IS DISTINCT FROM $1)
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
                """,
                new_office, manager_id
            )
            return {
                'military_moved': mil_count or 0,
                'relatives_moved': rel_count or 0,
            }
            
            
# ──────────── Универсальный поиск лидов (Q-Найти лида) ────────────

# Парсер запроса: что в нём — дата, телефон, или текст ФИО.
import re as _re_search


def _parse_search_query(query: str) -> dict:
    """
    Разбирает строку поиска на компоненты.
    Возвращает {
        'birth_date': date | None,      # если в строке есть дата ДД.ММ.ГГГГ
        'phone_last10': str | None,     # последние 10 цифр если в строке ≥10 цифр подряд
        'name_part': str | None,        # текст без даты и телефона (для ФИО)
    }
    """
    from datetime import date as _date

    result = {'birth_date': None, 'phone_last10': None, 'name_part': None}
    q = (query or '').strip()
    if not q:
        return result

    # Дата ДД.ММ.ГГГГ или ДД-ММ-ГГГГ или ДД/ММ/ГГГГ
    m_date = _re_search.search(
        r'(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})',
        q,
    )
    if m_date:
        try:
            d, mn, y = m_date.groups()
            result['birth_date'] = _date(int(y), int(mn), int(d))
            q = q.replace(m_date.group(0), ' ').strip()
        except ValueError:
            pass  # некорректная дата — игнорируем

    # Телефон: ≥10 цифр подряд (с возможными разделителями + ( ) - пробел)
    # Чистим все нецифры, если осталось ≥10 — берём последние 10
    digits_only = _re_search.sub(r'\D', '', q)
    if len(digits_only) >= 10:
        result['phone_last10'] = digits_only[-10:]
        # Убираем из текста всё что похоже на телефон (длинная последовательность цифр и разделителей)
        q = _re_search.sub(r'[\d\s\+\(\)\-]{10,}', ' ', q).strip()

    # Остальное — имя
    q = _re_search.sub(r'\s+', ' ', q).strip()
    if q:
        result['name_part'] = q

    return result


async def search_leads(query: str, role: str, office: str | None,
                       manager_id: int | None, limit: int = 20) -> dict:
    """
    Универсальный поиск военных и родственников.

    Поиск:
    - Если в query есть text → ищем по full_name (ILIKE %text%)
    - Если есть дата → AND по birth_date = date
    - Если есть телефон (≥10 цифр) → ищем родственников по phone (last 10)

    Доступ (изоляция офисов B1):
    - role='super_admin' → видит всё
    - role='office_admin'/'office_supervisor' → только свой office
    - role='manager' → только свои лиды (pm.added_by = manager_id) + свой office

    Возвращает:
        {
            'military': [...],         # до limit штук
            'military_total': int,      # сколько всего найдено (для "найдено N, показано M")
            'relatives': [...],
            'relatives_total': int,
        }

    Каждая запись в 'military':
        id, full_name, birth_date, office, added_by, manager_name,
        relatives_count (привязанных)

    Каждая запись в 'relatives':
        id, full_name, birth_date, phone, office, added_by, manager_name,
        attached_to: list[{military_id, full_name, birth_date}]
    """
    parsed = _parse_search_query(query)
    if not parsed['birth_date'] and not parsed['phone_last10'] and not parsed['name_part']:
        return {'military': [], 'military_total': 0,
                'relatives': [], 'relatives_total': 0}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # ───── Военные ─────
        mil_where = []
        mil_params = []

        if parsed['name_part']:
            mil_params.append(f"%{parsed['name_part']}%")
            mil_where.append(f"pm.full_name ILIKE ${len(mil_params)}")

        if parsed['birth_date']:
            mil_params.append(parsed['birth_date'])
            mil_where.append(f"pm.birth_date = ${len(mil_params)}")

        # Доступ
        if role == 'manager':
            mil_params.append(manager_id)
            mil_where.append(f"pm.added_by = ${len(mil_params)}")
            if office:
                mil_params.append(office)
                mil_where.append(f"pm.office = ${len(mil_params)}")
        elif role in ('office_admin', 'office_supervisor') and office:
            mil_params.append(office)
            mil_where.append(f"pm.office = ${len(mil_params)}")
        # super_admin → без фильтра

        military = []
        military_total = 0

        # Телефонный запрос → военных не ищем (у них нет phone)
        if mil_where and not (parsed['phone_last10'] and not parsed['name_part'] and not parsed['birth_date']):
            mil_where_sql = " AND ".join(mil_where)
            military_total = await conn.fetchval(
                f"SELECT COUNT(*) FROM persons_military pm WHERE {mil_where_sql}",
                *mil_params,
            )
            mil_params_with_limit = mil_params + [limit]
            rows = await conn.fetch(
                f"""
                SELECT pm.id, pm.full_name, pm.birth_date, pm.office, pm.added_by,
                       m.name AS manager_name,
                       (SELECT COUNT(*) FROM military_relatives mr 
                        WHERE mr.military_id = pm.id) AS relatives_count
                FROM persons_military pm
                LEFT JOIN managers m ON m.id = pm.added_by
                WHERE {mil_where_sql}
                ORDER BY pm.created_at DESC
                LIMIT ${len(mil_params_with_limit)}
                """,
                *mil_params_with_limit,
            )
            military = [dict(r) for r in rows]

        # ───── Родственники ─────
        rel_where = []
        rel_params = []

        if parsed['name_part']:
            rel_params.append(f"%{parsed['name_part']}%")
            rel_where.append(f"r.full_name ILIKE ${len(rel_params)}")

        if parsed['birth_date']:
            rel_params.append(parsed['birth_date'])
            rel_where.append(f"r.birth_date = ${len(rel_params)}")

        if parsed['phone_last10']:
            # Сравниваем "только цифры", берём last 10
            # regexp_replace убирает нецифры из r.phone, потом сравниваем хвост
            rel_params.append(parsed['phone_last10'])
            rel_where.append(
                f"RIGHT(regexp_replace(COALESCE(r.phone,''), '\\D', '', 'g'), 10) = ${len(rel_params)}"
            )

        # Доступ
        if role == 'manager':
            rel_params.append(manager_id)
            rel_where.append(f"r.added_by = ${len(rel_params)}")
            if office:
                rel_params.append(office)
                rel_where.append(f"r.office = ${len(rel_params)}")
        elif role in ('office_admin', 'office_supervisor') and office:
            rel_params.append(office)
            rel_where.append(f"r.office = ${len(rel_params)}")

        relatives = []
        relatives_total = 0

        if rel_where:
            rel_where_sql = " AND ".join(rel_where)
            relatives_total = await conn.fetchval(
                f"SELECT COUNT(*) FROM relatives r WHERE {rel_where_sql}",
                *rel_params,
            )
            rel_params_with_limit = rel_params + [limit]
            rows = await conn.fetch(
                f"""
                SELECT r.id, r.full_name, r.birth_date, r.phone, r.office, r.added_by,
                       m.name AS manager_name
                FROM relatives r
                LEFT JOIN managers m ON m.id = r.added_by
                WHERE {rel_where_sql}
                ORDER BY r.created_at DESC
                LIMIT ${len(rel_params_with_limit)}
                """,
                *rel_params_with_limit,
            )
            relatives = [dict(r) for r in rows]

            # Подтянем "к каким военным привязан" каждый найденный родственник
            if relatives:
                rel_ids = [r['id'] for r in relatives]
                links = await conn.fetch(
                    """
                    SELECT mr.relative_id, pm.id AS military_id, pm.full_name, pm.birth_date
                    FROM military_relatives mr
                    JOIN persons_military pm ON pm.id = mr.military_id
                    WHERE mr.relative_id = ANY($1::int[])
                    ORDER BY mr.created_at DESC
                    """,
                    rel_ids,
                )
                by_rel = {}
                for ln in links:
                    by_rel.setdefault(ln['relative_id'], []).append({
                        'military_id': ln['military_id'],
                        'full_name': ln['full_name'],
                        'birth_date': ln['birth_date'],
                    })
                for r in relatives:
                    r['attached_to'] = by_rel.get(r['id'], [])

        return {
            'military': military,
            'military_total': military_total or 0,
            'relatives': relatives,
            'relatives_total': relatives_total or 0,
        }
        
        
# ──────────── Проверка занятости номера телефона ────────────

async def is_phone_taken(phone: str) -> bool:
    """
    Проверяет, есть ли уже в БД родственник с таким номером телефона.
    Сравниваем по последним 10 цифрам (нечувствительно к +/без+, форматированию).

    Возвращает True если номер уже у кого-то в БД, иначе False.
    Пустой/мусорный (<10 цифр) phone → False.
    """
    if not phone:
        return False
    import re as _re_phone
    digits = _re_phone.sub(r'\D', '', phone)
    if len(digits) < 10:
        return False
    last10 = digits[-10:]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM relatives
            WHERE RIGHT(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), 10) = $1
            LIMIT 1
            """,
            last10,
        )
        return row is not None


async def find_phone_owner_office(phone: str) -> str | None:
    """
    Найти офис родственника по номеру телефона.
    Сравниваем по последним 10 цифрам (нечувствительно к +/без+).

    Возвращает:
    - 'pvl' / 'dp' / 'ha' — если родственник найден и у него есть office
    - None — если номер не в БД или родственник без office

    Если у нескольких родственников один номер (legacy дубли) —
    берём первого по id (самого старого).
    """
    if not phone:
        return None
    import re as _re_phone
    digits = _re_phone.sub(r'\D', '', phone)
    if len(digits) < 10:
        return None
    last10 = digits[-10:]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT office FROM relatives
            WHERE RIGHT(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), 10) = $1
            ORDER BY id ASC
            LIMIT 1
            """,
            last10,
        )
        return row["office"] if row else None


