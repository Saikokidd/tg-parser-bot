"""
Хендлер пробива через внешние API (Sauron + future providers).

Флоу:
1. Сразу после сохранения военного — автоматически запускается пробив.
   Показываем "Возможные связи по адресу" + кнопки "Пробить" по каждому найденному.

2. Менеджер жмёт "🔍 [имя]" → новый запрос к API по этому человеку →
   возвращаем заполненный шаблон родственника (с самыми частыми значениями).
   Менеджер копирует шаблон и через "✍️ Заполнить" вносит в БД.
"""
import logging
from datetime import date
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.services.voxlink_service import lookup_phone
from bot.services.probiv_service import (
    probit_person, format_probiv_result, ProbivResult
)
from bot.parser.sauron_parser import (
    format_relative_template,
    format_address_relations_page,
    _dedup_persons_from_blocks,
)
from bot.parser.relative_parser import normalize_phone
from bot.keyboards.menus import (
    probiv_persons_kb,
    attach_relative_kb,
    attach_duplicate_kb,
)
from bot.utils.long_message import safe_edit_or_send
from bot.db.queries import (
    link_military_relative,
    get_military_by_id,
    find_relative_global_dup, insert_relative_v2, get_manager_by_id,   # B1
    insert_relative_phones, is_phone_taken,                            # multi-phones
)

logger = logging.getLogger(__name__)
router = Router()


# ════════════════════════════════════════════════════════════
#  Шаг 1: автоматический пробив сразу после сохранения военного
# ════════════════════════════════════════════════════════════

async def run_probiv_after_save(
    target: Message,
    state: FSMContext,
    full_name: str,
    birth_date: date | None,
    military_id: int | None = None,
    office: str | None = None,
):
    """
    Вызывается из military.py / relatives.py после успешного сохранения военного
    или при ручном запуске пробива.

    office определяет на какой счёт Sauron спишутся деньги — pvl или dp.
    Если None — пробив завершится ошибкой "офис не указан".
    """
    status_msg = await target.answer("🔍 Пробиваю через Sauron на наличие возможных связей...")

    # Берём manager_id из FSM — он туда кладётся хендлерами
    fsm_data = await state.get_data()
    manager_id = fsm_data.get("manager_id") or fsm_data.get("saved_manager_id")
    # saved_manager_id используется при автопробиве после создания военного,
    # manager_id — при "Заполнить → Пробить через Sauron"

    # Office — на какой счёт Sauron списать. Если хендлер забыл передать,
    # пробуем достать из FSM (на случай если он там лежит).
    if office is None:
        office = fsm_data.get("office") or fsm_data.get("saved_office")

    # Диагностика: если manager_id не подтянулся — пишем в лог что было в FSM.
    # Это поможет найти все возможные ветки кода где manager_id не сохраняется.
    if manager_id is None:
        logger.warning(
            f"run_probiv_after_save: manager_id is None для {full_name} "
            f"(military_id={military_id}). FSM keys: {list(fsm_data.keys())}"
        )

    # Без office дальше не идём — иначе спишутся деньги с дефолтного счёта.
    if not office:
        logger.warning(
            f"run_probiv_after_save: office is None для {full_name}. FSM keys: {list(fsm_data.keys())}"
        )
        await status_msg.edit_text(
            "⚠️ У вас не указан офис. Обратитесь к админу."
        )
        return

    try:
        result: ProbivResult = await probit_person(
            full_name, birth_date,
            manager_id=manager_id,
            context="auto",
            military_id=military_id,
            office=office,
        )
    except ValueError as e:
        await status_msg.edit_text(f"⚠️ Пробив не запущен: {e}")
        return
    except Exception as e:
        logger.exception("Ошибка пробива")
        await status_msg.edit_text(f"❌ Ошибка пробива: {e}")
        return

    # Если все провайдеры упали
    if not result.raw_results:
        await status_msg.edit_text(format_probiv_result(result, header="🔍 *Пробив*"))
        return

    # Сохраняем найденных людей в FSM для кнопок "Пробить далее"
    persons_index = []
    seen = set()
    for block in result.address_relations:
        for p in block["persons"]:
            key = f"{p['full_name']}|{p['birth_date_str']}"
            if key in seen:
                continue
            seen.add(key)
            persons_index.append(p)

    # Запоминаем контекст для будущих "Закрепить":
    # - какой именно военный пробивался
    # - его ФИО для подписи кнопки
    # - индекс шаблонов родственников (заполняется по мере кликов "Пробить далее")
    await state.update_data(
        probiv_persons=persons_index,
        probiv_origin_military_name=full_name,
        probiv_origin_military_id=military_id,
        probiv_templates={},  # idx → распарсенный шаблон
    )
    # Пагинация: собираем плоский список людей один раз и используем
    # его и для текста, и для клавиатуры. Стартуем со страницы 1.
    persons_flat = _dedup_persons_from_blocks(result.address_relations)

    text_parts = ["🔍 *Пробив выполнен*", ""]
    text_parts.append(format_address_relations_page(persons_flat, page=1, page_size=15))
    text = "\n".join(text_parts)

    keyboard = probiv_persons_kb(persons_flat, page=1, page_size=15) if persons_flat else None
    await safe_edit_or_send(
        status_msg, text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ════════════════════════════════════════════════════════════
#  Шаг 2: пробив выбранного человека → шаблон родственника
# ════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("probiv:next:"))
async def probiv_next(callback: CallbackQuery, state: FSMContext, manager: dict | None = None):
    # Сразу подтверждаем нажатие — защита от тройного срабатывания при двойном клике
    await callback.answer()

    idx = int(callback.data.split(":")[2])
    data = await state.get_data()
    persons = data.get("probiv_persons", [])
    if idx >= len(persons):
        await callback.message.answer("⚠️ Запись не найдена.")
        return
    person = persons[idx]
    full_name = person["full_name"]
    birth_str = person["birth_date_str"]


    # Парсим дату обратно в date
    birth_date = None
    if birth_str:
        try:
            d, m, y = birth_str.split(".")
            birth_date = date(int(y), int(m), int(d))
        except Exception:
            birth_date = None

    # Если в найденной связи нет ДР — пробуем найти такое же ФИО в нашей БД
    # (часто этот человек уже есть как родственник у другого лида с заполненной ДР).
    # Без ДР Sauron возвращает гораздо меньше данных.
    if birth_date is None:
        from bot.db.queries import find_birth_date_by_name
        birth_date = await find_birth_date_by_name(full_name)
        if birth_date:
            logger.info(f"probiv_next: ДР для {full_name} взята из БД ({birth_date})")

    status_msg = await callback.message.answer(
        f"🔍 Пробиваю *{full_name}*...",
        parse_mode="Markdown"
    )

    manager_id = manager["id"] if manager else None
    office = manager.get("office") if manager else None

    if not office:
        await callback.message.answer(
            "⚠️ У вас не указан офис. Обратитесь к админу."
        )
        return

    try:
        result: ProbivResult = await probit_person(
            full_name, birth_date,
            manager_id=manager_id,
            context="next",
            office=office,
        )
    except ValueError as e:
        # ФИО не прошло валидацию (например, после очистки от "Оглы" не хватает слов)
        await status_msg.edit_text(
            f"⚠️ Не удалось обработать имя «{full_name}»:\n"
            f"`{e}`\n\n"
            "_Внесите данные родственника вручную через «✍️ Заполнить»._",
            parse_mode="Markdown"
        )
        return
    except Exception as e:
        logger.exception("Ошибка пробива (next)")
        err_text = str(e)
        # Sauron не принял имя (нестандартные символы)
        if "Invalid characters" in err_text:
            await status_msg.edit_text(
                f"⚠️ Sauron не смог обработать имя «{full_name}»\n"
                "(нестандартные символы или приставка типа Оглы/Кызы).\n\n"
                "_Внесите данные родственника вручную через «✍️ Заполнить»._",
                parse_mode="Markdown"
            )
            return
        await status_msg.edit_text(f"❌ Ошибка пробива: {err_text[:200]}")
        return

    if not result.raw_results:
        await status_msg.edit_text(format_probiv_result(result, header="🔍 *Пробив*"))
        return

    # Шаблон родственника
    if result.relative_template:
        template = result.relative_template

        # Проверяем — есть ли минимально необходимые поля для кнопки "Закрепить"?
        # ФИО + ДР обязательны, телефон опционален (можно добавить позже).
        has_key_fields = bool(
            template.get("full_name")
            and template.get("birth_date_str")
        )

        # Сохраняем результат пробива через обогащение (operator, region, valid_emails)
        # Эти поля уже добавлены в template внутри probit_person
        kb = None
        if has_key_fields:
            # Сохраняем шаблон в FSM для последующего "Закрепить"
            templates = data.get("probiv_templates", {})
            templates[str(idx)] = template  # ключи в JSON всегда строки
            await state.update_data(probiv_templates=templates)

            # Берём ФИО исходного военного для подписи кнопки
            origin_name = data.get("probiv_origin_military_name", "лидом")
            # Только фамилия + первая буква имени для краткости
            label_parts = origin_name.split()
            short_label = label_parts[0] if label_parts else origin_name
            if len(label_parts) >= 2:
                short_label = f"{label_parts[0]} {label_parts[1][:1]}."

            kb = attach_relative_kb(idx, short_label)

        text = format_relative_template(template)
        if not has_key_fields:
            text += "\n\n⚠️ _Для автоматического закрепления нужны минимум ФИО и ДР._\n_Внесите данные вручную через «✍️ Заполнить»._"

        await status_msg.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        await status_msg.edit_text(
            f"⚠️ Данные по «{full_name}» найдены, но шаблон собрать не удалось.\n"
            "Возможно, в результатах пробива нет ключевых полей.\n\n"
            "_Внесите данные родственника вручную через «✍️ Заполнить»._",
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "probiv:page:noop")
async def probiv_page_noop(callback: CallbackQuery):
    """Инфо-кнопка '1/2' — просто закрываем спиннер, ничего не делаем."""
    await callback.answer()


@router.callback_query(F.data.startswith("probiv:page:"))
async def probiv_page(callback: CallbackQuery, state: FSMContext):
    """
    Переключение страницы возможных связей.
    Берёт probiv_persons из FSM, формирует новый текст+клавиатуру,
    редактирует то же сообщение.
    """
    await callback.answer()
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return

    data = await state.get_data()
    persons = data.get("probiv_persons") or []
    if not persons:
        # Список потерян (например бот рестартовал) — гасим клавиатуру
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    text_parts = ["🔍 *Пробив выполнен*", ""]
    text_parts.append(format_address_relations_page(persons, page=page, page_size=15))
    text = "\n".join(text_parts)

    keyboard = probiv_persons_kb(persons, page=page, page_size=15)

    try:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        # Например 'message is not modified' если страница та же — пропускаем тихо
        logger.debug(f"probiv_page edit_text failed: {e}")

@router.callback_query(F.data == "probiv:done")
async def probiv_done(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.answer("Готово")
    
    
# ════════════════════════════════════════════════════════════
#  Шаг 3: закрепление родственника за военным
# ════════════════════════════════════════════════════════════

async def _save_relative_phones_from_template(
    template: dict, relative_id: int, primary_phone: str | None,
    office: str | None = None,
) -> int:
    """
    Сохранить все номера из Sauron-template в relative_phones.
    
    Фича работает только для офиса pvl (HLR-проверка для них одних).
    Для dp/ha записи в relative_phones не создаём — это мусор который
    никогда не будет использован.
    
    Phone-dedup: пропускаем номера которые уже заняты в БД
    (не сохраняем чужие, согласно решению F2(d)).
    
    primary_phone — нормализованный номер который ушёл в relatives.phone
                    (помечается is_primary=true).
    office — офис родственника, для фильтрации.
    Возвращает количество вставленных записей.
    """
    # Фильтр по офису: фича только для pvl
    if office and office != "pvl":
        return 0
    
    try:
        candidates = template.get("phone_candidates_with_freq") or []
        logger.info(
            f"multi-phones: relative_id={relative_id} got "
            f"{len(candidates)} candidates, primary={primary_phone}"
        )
        if not candidates:
            return 0
        
        from bot.parser.relative_parser import normalize_phone
        
        # Сохраняем только primary номер (самый частый из Sauron-ответа).
        # Multi-phones фича откатана — для HLR-проверки достаточно одного.
        MAX_PHONES_PER_RELATIVE = 1
        
        phones_to_insert = []
        seen_normalized = set()
        
        for cand in candidates:
            raw_phone = cand.get("phone")
            freq = cand.get("frequency", 1)
            if not raw_phone:
                continue
            normalized = normalize_phone(raw_phone)
            if not normalized:
                continue
            norm_key = normalized.lstrip("+")[-10:]
            if norm_key in seen_normalized:
                continue
            seen_normalized.add(norm_key)
            is_primary = (normalized == primary_phone)
            if not is_primary:
                taken = await is_phone_taken(normalized)
                if taken:
                    logger.info(f"multi-phones: skip taken {normalized}")
                    continue
            phones_to_insert.append({
                "phone": normalized,
                "source_frequency": freq,
                "is_primary": is_primary,
            })
            # Достигли лимита — выходим
            if len(phones_to_insert) >= MAX_PHONES_PER_RELATIVE:
                break
        
        if not phones_to_insert:
            logger.info(f"multi-phones: relative_id={relative_id} nothing to insert")
            return 0
        inserted = await insert_relative_phones(relative_id, phones_to_insert)
        logger.info(
            f"multi-phones: relative_id={relative_id} → "
            f"inserted={inserted}, primary={primary_phone}"
        )
        return inserted
    except Exception:
        logger.exception(
            f"multi-phones: failed for relative_id={relative_id}"
        )
        return 0


async def _build_relative_data_from_template(template: dict) -> dict:
    """
    Преобразовать шаблон из Sauron в формат данных для insert_relative_v2.
    Извлекает все доступные поля + кладёт обогащение (operator, region) в extra.
    Оператор/регион определяются СИНХРОННО через voxlink здесь, чтобы менеджер
    видел их сразу в шаблоне (фоновый enricher остаётся как подстраховка).
    """
    # birth_date_str → date
    from datetime import date as _date
    birth_date = None
    birth_str = template.get("birth_date_str")
    if birth_str:
        try:
            d, m, y = birth_str.split(".")
            birth_date = _date(int(y), int(m), int(d))
        except Exception:
            pass

    extra = {}
    for key in ("snils", "inn", "passport", "operator", "region"):
        val = template.get(key)
        if val:
            extra[key] = val

    # email — берём первый из valid_emails если он есть, иначе из обычного email
    valid_emails = template.get("valid_emails") or []
    if valid_emails:
        extra["email"] = valid_emails[0]
    elif template.get("email"):
        extra["email"] = template["email"]

    phone = normalize_phone(template.get("phone"))

    # Синхронное обогащение voxlink — чтобы оператор/регион были сразу в шаблоне.
    # Если оператора ещё нет в extra и есть телефон — спрашиваем voxlink.
    if phone and not extra.get("operator"):
        try:
            info = await lookup_phone(phone)
            if info:
                if info.get("operator"):
                    extra["operator"] = info["operator"]
                if info.get("region"):
                    extra["region"] = info["region"]
                if info.get("tz_offset"):
                    extra["tz_offset"] = info["tz_offset"]
        except Exception:
            pass  # voxlink недоступен — фоновый enricher добьёт позже

    return {
        "full_name": template.get("full_name"),
        "birth_date": birth_date,
        "phone": phone,
        "address": template.get("address"),
        "extra": extra,
    }


async def _get_attach_context(callback: CallbackQuery, state: FSMContext, idx_str: str, manager: dict):
    """
    Извлечь из FSM весь нужный контекст для закрепления.
    manager — приходит из middleware (как в других хендлерах).
    Возвращает (manager_id, military_id, template) или None при ошибке.
    """
    data = await state.get_data()

    logger.info(
        f"_get_attach_context: idx={idx_str}, keys={list(data.keys())}, "
        f"manager={'yes' if manager else 'no'}, "
        f"military_id={data.get('probiv_origin_military_id')}, "
        f"templates_keys={list((data.get('probiv_templates') or {}).keys())}"
    )

    templates = data.get("probiv_templates", {})
    template = templates.get(idx_str)

    if not template:
        await callback.message.answer("⚠️ Данные родственника не найдены. Попробуйте пробить заново.")
        return None

    manager_id = manager.get("id") if manager else None
    military_id = data.get("probiv_origin_military_id")

    if not manager_id or not military_id:
        await callback.message.answer(
            "⚠️ Контекст лида утерян (возможно прошло слишком много времени).\n"
            "Внесите данные вручную через «✍️ Заполнить»."
        )
        return None

    return manager_id, military_id, template


@router.callback_query(F.data.startswith("attach:later:"))
async def attach_later(callback: CallbackQuery, state: FSMContext, manager: dict):
    """📂 Закрепить позже — просто убираем кнопки, шаблон остаётся для копирования"""
    await callback.answer("Шаблон остался для копирования")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("attach:do:"))
async def attach_do(callback: CallbackQuery, state: FSMContext, manager: dict):
    """📌 Закрепить за военным — дубль-чек и сохранение"""
    await callback.answer()
    idx_str = callback.data.split(":")[2]

    ctx = await _get_attach_context(callback, state, idx_str, manager)
    if not ctx:
        return
    manager_id, military_id, template = ctx

    relative_data = await _build_relative_data_from_template(template)

    # Глобальный дубль-чек (этап B1): если дубль из чужого офиса — жёсткий отказ
    my_office = manager.get("office") if manager else None
    global_dup = await find_relative_global_dup(
        full_name=relative_data["full_name"],
        birth_date=relative_data["birth_date"],
        phone=relative_data["phone"],
        address=relative_data["address"],
    )
    if global_dup:
        dup_office = global_dup.get("office")
        if dup_office and my_office and dup_office != my_office:
            mgr_name = global_dup.get("manager_name") or "—"
            await callback.message.answer(
                f"⛔ Этот родственник уже в работе у офиса "
                f"<b>{dup_office}</b> (менеджер: <b>{mgr_name}</b>).\n\n"
                f"Закрепление запрещено.",
                parse_mode="HTML",
            )
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

    # B4: дубль-чек переиспользует global_dup из B1-блока выше.
    # Сюда мы попадаем только если дубля нет совсем, ИЛИ дубль в своём офисе.
    # global_dup имеет все поля для плашки (id, full_name, birth_date, phone, address).
    if global_dup:
        # Запомним что хотим закрепить — для последующих коллбэков "Закрепить как нового / Использовать"
        data = await state.get_data()
        pending = data.get("attach_pending", {})
        pending[idx_str] = {
            "duplicate_id": global_dup["id"],
        }
        await state.update_data(attach_pending=pending)

        dup = global_dup
        dup_birth = dup.get("birth_date")
        dup_birth_str = dup_birth.strftime("%d.%m.%Y") if dup_birth else "—"
        # Экранирование пользовательских строк для HTML.
        # В ФИО/адресе из БД могут быть символы '<', '>', '&' — без экранирования
        # Telegram упадёт с "can't parse entities".
        import html as _html
        dup_name = _html.escape(dup.get("full_name", "—") or "—")
        lines = [
            "⚠️ <b>В базе уже есть похожий родственник:</b>",
            "",
            f"• <b>{dup_name}</b> ({dup_birth_str})",
        ]
        if dup.get("phone"):
            lines.append(f"  📞 {_html.escape(dup['phone'])}")
        if dup.get("address"):
            addr = dup["address"]
            if len(addr) > 70:
                addr = addr[:67] + "..."
            lines.append(f"  🏠 {_html.escape(addr)}")
        lines.append("")
        lines.append("Что делаем?")
        await callback.message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=attach_duplicate_kb(int(idx_str)),
        )
        return

    # Дубля нет — сразу сохраняем + создаём связку
    rel = await insert_relative_v2(relative_data, manager_id)
    await link_military_relative(military_id, rel["id"], manager_id)
    # Сохраняем все номера из Sauron-template в relative_phones (только pvl)
    await _save_relative_phones_from_template(
        template, rel["id"], relative_data.get("phone"),
        office=rel.get("office"),
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await _finalize_attach(callback, state, relative_data["full_name"], reused=False)


@router.callback_query(F.data.startswith("attach:dup:new:"))
async def attach_dup_new(callback: CallbackQuery, state: FSMContext, manager: dict):
    """➕ Закрепить как нового (несмотря на найденный дубль)"""
    await callback.answer()
    idx_str = callback.data.split(":")[3]

    ctx = await _get_attach_context(callback, state, idx_str, manager)
    if not ctx:
        return
    manager_id, military_id, template = ctx

    relative_data = await _build_relative_data_from_template(template)
    rel = await insert_relative_v2(relative_data, manager_id)
    await link_military_relative(military_id, rel["id"], manager_id)
    await _save_relative_phones_from_template(
        template, rel["id"], relative_data.get("phone"),
        office=rel.get("office"),
    )
    await callback.message.edit_text(
        f"➕ Создан новый родственник: *{relative_data['full_name']}*",
        parse_mode="Markdown",
    )
    await _finalize_attach(callback, state, relative_data["full_name"], reused=False)


@router.callback_query(F.data.startswith("attach:dup:reuse:"))
async def attach_dup_reuse(callback: CallbackQuery, state: FSMContext, manager: dict):
    """♻ Использовать существующего (только создаём связку)"""
    await callback.answer()
    idx_str = callback.data.split(":")[3]

    data = await state.get_data()
    pending = data.get("attach_pending", {}).get(idx_str) or {}
    dup_id = pending.get("duplicate_id")

    ctx = await _get_attach_context(callback, state, idx_str, manager)
    if not ctx or not dup_id:
        await callback.message.edit_text("⚠️ Не найден существующий родственник для использования.")
        return
    manager_id, military_id, template = ctx

    created = await link_military_relative(military_id, dup_id, manager_id)
    if created:
        await callback.message.edit_text(
            f"♻ Существующий родственник привязан к лиду.",
        )
    else:
        await callback.message.edit_text(
            f"ℹ️ Этот родственник уже был привязан к лиду ранее.",
        )

    await _finalize_attach(callback, state, template.get("full_name") or "родственник", reused=True)


@router.callback_query(F.data == "attach:dup:cancel")
async def attach_dup_cancel(callback: CallbackQuery, state: FSMContext):
    """❌ Отмена закрепления при дубле"""
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Закрепление отменено.")


async def _finalize_attach(callback: CallbackQuery, state: FSMContext,
                            relative_name: str, reused: bool):
    """
    После успешного закрепления — сообщаем и предлагаем продолжить
    с остальными кандидатами из списка.
    """
    data = await state.get_data()
    persons = data.get("probiv_persons") or []
    templates = data.get("probiv_templates") or {}

    # Сколько ещё не обработано (не было кликов "Пробить далее")
    not_yet_clicked = sum(1 for i, _ in enumerate(persons) if str(i) not in templates)

    if reused:
        msg = f"♻ *{relative_name}* привязан к лиду."
    else:
        msg = f"✅ *{relative_name}* закреплён за лидом."

    if not_yet_clicked > 0:
        msg += f"\n\n_Остальных кандидатов из списка можно пробить кнопками выше ({not_yet_clicked} осталось)._"

    await callback.message.answer(msg, parse_mode="Markdown")