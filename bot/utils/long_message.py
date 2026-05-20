"""
Утилиты для отправки длинных сообщений в Telegram.

Telegram-лимит на одно сообщение — 4096 символов.
Если ответ длиннее — разбиваем на части по последнему переносу строки,
чтобы не резать в середине слова или Markdown-блока.
"""

TELEGRAM_MAX_LEN = 4096
SAFETY_MARGIN = 50  # запас, на случай если Markdown форматирование разрастётся


def split_long_text(text: str, max_len: int = TELEGRAM_MAX_LEN - SAFETY_MARGIN) -> list[str]:
    """
    Разбить текст на куски по max_len символов, стараясь резать на переносах строк.
    Возвращает список кусков, каждый <= max_len.
    """
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""

    for line in text.splitlines(keepends=True):
        # Если строка сама по себе длиннее лимита — режем её жёстко
        if len(line) > max_len:
            if current:
                parts.append(current.rstrip())
                current = ""
            for i in range(0, len(line), max_len):
                parts.append(line[i:i + max_len])
            continue

        # Если добавление строки превысит лимит — закрываем текущий кусок
        if len(current) + len(line) > max_len:
            parts.append(current.rstrip())
            current = line
        else:
            current += line

    if current:
        parts.append(current.rstrip())

    return parts


async def safe_edit_or_send(status_msg, text: str, *,
                            parse_mode: str = None,
                            reply_markup=None):
    """
    Отредактировать status_msg первым куском; если кусков несколько —
    дополнительные отправляются как новые сообщения в том же чате.
    Клавиатура крепится к последнему куску.
    """
    parts = split_long_text(text)

    if len(parts) == 1:
        await status_msg.edit_text(
            parts[0],
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return

    # Первая часть — редактируем status_msg (без клавиатуры — будет на последней)
    await status_msg.edit_text(parts[0], parse_mode=parse_mode)

    # Промежуточные части — просто текст
    for part in parts[1:-1]:
        await status_msg.answer(part, parse_mode=parse_mode)

    # Последняя часть — с клавиатурой если есть
    await status_msg.answer(
        parts[-1],
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )
