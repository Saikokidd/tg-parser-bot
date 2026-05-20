"""
Защита FSM-хендлеров от случайного перехвата текста кнопок главного меню.

Когда менеджер находится в FSM (например, "введите ФИО"), а нажимает
reply-кнопку из главного меню — её текст уходит в FSM-обработчик и
попадает в БД как ФИО. Эта утилита ловит такие случаи.
"""
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


# Список текстов кнопок главного меню (синхронизирован с keyboards/menus.py)
MAIN_MENU_TEXTS = {
    "🔍 Пробить",
    "✍️ Заполнить",
    "Список лидов",
    "📊 Статистика",
    "📤 Выгрузить лидов",
    "⚙️ Управление ботом",
}


async def is_menu_button_pressed(message: Message, state: FSMContext) -> bool:
    """
    Если текст сообщения — это нажатие на кнопку главного меню,
    то сбрасываем FSM и пишем подсказку. Возвращает True если перехватили.

    Использование в FSM-хендлерах:
        async def my_handler(message, state):
            if await is_menu_button_pressed(message, state):
                return
            # ... обычная обработка
    """
    text = (message.text or "").strip()
    if text in MAIN_MENU_TEXTS:
        await state.clear()
        await message.answer(
            f"⚠️ Текущий ввод отменён — вы нажали «{text}».\n"
            f"Нажмите кнопку ещё раз чтобы перейти в раздел."
        )
        return True
    return False
