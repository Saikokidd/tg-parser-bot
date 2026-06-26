"""
Хелперы форматирования телефонов из relative_phones (multi-phones фича).

Используется в:
- bot/handlers/leads.py — карточка лида
- bot/handlers/search.py — карточка поиска
- bot/services/export_service.py — выгрузка xlsx (там свой _fmt_phones,
  потому что многострочный формат для ячейки)
"""

# Маппинг hlr_status → emoji
HLR_STATUS_EMOJI = {
    "available":         "✅",
    "unavailable":       "❌",
    "not_exists":        "⛔",
    "in_work":           "⏳",
    "pending":           "⏳",
    "skipped_operator":  "⊝",
    "error":             "⚠️",
}


def fmt_phone_compact(
    phones: list[dict],
    legacy_phone: str | None,
    legacy_operator: str | None,
) -> str:
    """
    Компактный формат для одной строки в карточке.
    Показывает primary номер + сколько ещё есть.
    
    Формат:
      "+79... (МТС) ✅"                    — если 1 номер
      "+79... (МТС) ✅ +3 ещё"             — если 4 номера
      "+79... (Билайн)"                    — если статуса нет
      legacy_phone (без HLR-данных)        — для старых записей
    """
    if not phones:
        if legacy_phone:
            parts = [legacy_phone]
            if legacy_operator:
                parts.append(f"({legacy_operator})")
            return " ".join(parts)
        return ""
    
    primary = phones[0]
    rest = len(phones) - 1
    
    phone = primary.get("phone") or ""
    op = primary.get("operator")
    status = primary.get("hlr_status")
    
    parts = [phone]
    if op:
        parts.append(f"({op})")
    emoji = HLR_STATUS_EMOJI.get(status or "")
    if emoji:
        parts.append(emoji)
    
    line = " ".join(parts)
    if rest > 0:
        line = f"{line} +{rest} ещё"
    return line


def fmt_phones_full(
    phones: list[dict],
    legacy_phone: str | None,
    legacy_operator: str | None,
) -> str:
    """
    Полный многострочный формат для детальной карточки.
    Все номера через перенос, с оператором и статусом.
    """
    if not phones:
        if legacy_phone:
            parts = [legacy_phone]
            if legacy_operator:
                parts.append(f"({legacy_operator})")
            return " ".join(parts)
        return ""
    
    lines = []
    for p in phones:
        phone = p.get("phone") or ""
        op = p.get("operator")
        status = p.get("hlr_status")
        parts = [phone]
        if op:
            parts.append(f"({op})")
        emoji = HLR_STATUS_EMOJI.get(status or "")
        if emoji:
            parts.append(emoji)
        lines.append(" ".join(parts))
    return "\n".join(lines)
