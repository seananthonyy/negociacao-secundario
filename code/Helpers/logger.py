import logging
from datetime import datetime
from pathlib import Path

from config import cfg

# Timestamp fixo para toda a sessão (processo). Cada rodagem gera seu próprio arquivo.
TS_SESSAO = datetime.now().strftime("%Y-%m-%d_%H%M%S")


def ObterLogger(name: str) -> logging.Logger:
    """
    Retorna logger com:
      - stdout (INFO)
      - arquivo em codigos/{name}/logs/{YYYY-MM-DD_HHMMSS}.log (DEBUG)

    O log cai DENTRO da pasta do proprio script (cada script mora em
    codigos/<nome>/ e chama ObterLogger(<nome>)), entao a pasta e autocontida:
    codigo e historico de execucao no mesmo lugar. Quem nao tem pasta propria
    (uso avulso, notebook) cai em files/logs/{name}/.

    Cada execução do processo cria um arquivo novo (timestamp no nome).
    Idempotente: não duplica handlers se chamado mais de uma vez com o mesmo nome.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    dirPropria = Path(cfg["paths"]["codigosDir"]) / name
    dirLog = (dirPropria / "logs" if dirPropria.is_dir()
              else Path(cfg["paths"]["logsDir"]) / name)
    dirLog.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    handlerArquivo = logging.FileHandler(
        dirLog / f"{TS_SESSAO}.log",
        encoding="utf-8",
    )
    handlerArquivo.setLevel(logging.DEBUG)
    handlerArquivo.setFormatter(fmt)

    handlerConsole = logging.StreamHandler()
    handlerConsole.setLevel(logging.INFO)
    handlerConsole.setFormatter(fmt)

    logger.addHandler(handlerArquivo)
    logger.addHandler(handlerConsole)

    return logger
