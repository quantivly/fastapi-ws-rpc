from __future__ import annotations

import logging
import os
from enum import Enum
from logging.config import dictConfig
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Logger as LoguruLogger  # type: ignore[import-not-found]

ENV_VAR = "WS_RPC_LOGGING"


class LoggingModes(Enum):
    # don't produce logs
    NO_LOGS = 0
    # Log alongside uvicorn
    UVICORN = 1
    # Simple log calls (no config)
    SIMPLE = 2
    # log via the loguru module
    LOGURU = 3


class LoggingConfig:
    def __init__(self) -> None:
        self._mode: LoggingModes | None = None

    config_template = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(asctime)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
            },
        },
        "loggers": {},
    }

    UVICORN_LOGGERS = {
        "uvicorn.error": {
            "propagate": False,
            "handlers": ["default"],
        },
        "fastapi_ws_rpc": {
            "handlers": ["default"],
            "propagate": False,
            "level": logging.INFO,
        },
    }

    def get_mode(self) -> LoggingModes:
        # if no one set the mode - set default from ENV or hardcoded default
        if self._mode is None:
            mode = LoggingModes.__members__.get(
                os.environ.get(ENV_VAR, "").upper(), LoggingModes.SIMPLE
            )
            self.set_mode(mode)
        # Runtime check to protect against -O optimization flag that disables assertions
        if self._mode is None:
            raise RuntimeError("Logging mode must be set by set_mode() method")
        return self._mode

    def set_mode(
        self, mode: LoggingModes = LoggingModes.UVICORN, level: int = logging.INFO
    ) -> None:
        """
        Configure logging. this method calls 'logging.config.dictConfig()' to enable
        quick setup of logging. Call this method before starting the app.
        For more advanced cases use 'logging.config' directly (loggers used by this
        library are all nested under "fastapi_ws_rpc" logger name)

        Args:
            mode (LoggingModes, optional): The mode to set logging to. Defaults to
            LoggingModes.UVICORN.
            level (int, optional): The logging level. Defaults to logging.INFO.
        """
        self._mode = mode
        logging_config = self.config_template.copy()
        # add logs beside uvicorn
        if mode == LoggingModes.UVICORN:
            logging_config["loggers"] = self.UVICORN_LOGGERS.copy()
            # Type ignore needed because dict structure is dynamic
            logging_config["loggers"]["fastapi_ws_rpc"]["level"] = level  # type: ignore[index]
            dictConfig(logging_config)
        elif mode == LoggingModes.SIMPLE or mode == LoggingModes.LOGURU:
            pass
        # no logs
        else:
            logging_config["loggers"] = {}
            dictConfig(logging_config)


# Singelton for logging configuration
logging_config = LoggingConfig()


def get_logger(name: str) -> logging.Logger | LoguruLogger:
    """
    Get a logger object to log with.
    Called by inner modules for logging.

    Args:
        name (str): The name of the logger module.

    Returns:
        Logger object (either standard logging.Logger or loguru logger).
    """
    mode = logging_config.get_mode()
    # logging through loguru
    if mode == LoggingModes.LOGURU:
        from loguru import logger

        return logger
    # regular python logging
    return logging.getLogger(f"fastapi_ws_rpc.{name}")
