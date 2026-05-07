"""
Конфигурация логирования.

Логи пишутся:
- В консоль (видно через journalctl)
- В файл logs/bot.log — все INFO+ записи, ротация по дням, хранить 14 дней
- В файл logs/bot.error.log — только WARNING/ERROR/CRITICAL, хранить 30 дней
"""
import logging
import logging.handlers
from pathlib import Path


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str = "logs") -> None:
    """
    Настроить логирование. Вызывается один раз при старте бота.
    """
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Корневой логгер
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Чистим обработчики если уже что-то настроено
    root.handlers.clear()

    # 1. Консоль (попадёт в journalctl)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 2. Общий файл — все INFO+, ротация по дням, 14 файлов
    file_all = logging.handlers.TimedRotatingFileHandler(
        filename=log_path / "bot.log",
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8"
    )
    file_all.setLevel(logging.INFO)
    file_all.setFormatter(formatter)
    root.addHandler(file_all)

    # 3. Файл ошибок — WARNING+, хранить дольше (30 дней)
    file_err = logging.handlers.TimedRotatingFileHandler(
        filename=log_path / "bot.error.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_err.setLevel(logging.WARNING)
    file_err.setFormatter(formatter)
    root.addHandler(file_err)

    # Снижаем шум от aiogram (он любит логать на DEBUG)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
