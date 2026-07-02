import logging
from datetime import datetime
from pathlib import Path

from lib.config import cfg

# Timestamp fixo para toda a sessão (processo). Cada rodagem gera seu próprio arquivo.
_SESSION_TS = datetime.now().strftime("%Y-%m-%d_%H%M%S")


def get_logger(name: str) -> logging.Logger:
    """
    Retorna logger com:
      - stdout (INFO)
      - arquivo em data/logs/{name}/{YYYY-MM-DD_HHMMSS}.log (DEBUG)

    Cada execução do processo cria um arquivo novo (timestamp no nome).
    Idempotente: não duplica handlers se chamado mais de uma vez com o mesmo nome.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_dir = Path(cfg["paths"]["logsDir"]) / name
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    file_handler = logging.FileHandler(
        log_dir / f"{_SESSION_TS}.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
