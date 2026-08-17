"""轻量日志：滚动文件输出，GUI 程序无控制台时的可追溯性来源。

    from applog import log
    log.info(...) / log.warning(...) / log.exception(...)

    文件：logs/app.log（5MB × 3 滚动），级别默认 INFO，
    可用环境变量 RESOURCES_LOG_LEVEL 覆盖（DEBUG/INFO/WARNING/ERROR）。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

import config

_started = False


def setup_logging() -> logging.Logger:
    """初始化根日志（幂等），返回可直接使用的 logger。"""
    global _started
    if _started:
        return logging.getLogger("webscoop")
    log_file = os.environ.get("RESOURCES_LOG_FILE", config.LOG_FILE)
    level = os.environ.get("RESOURCES_LOG_LEVEL", config.LOG_LEVEL).upper()
    try:
        lvl = getattr(logging, level)
    except AttributeError:
        lvl = logging.INFO

    logger = logging.getLogger("webscoop")
    logger.setLevel(lvl)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass  # 日志目录不可写时静默降级，不阻塞主流程
    logger.propagate = False
    _started = True
    return logger


log = setup_logging()
