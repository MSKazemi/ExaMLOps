"""Unit tests for SeanergysLogger."""

import logging



def test_logger_instantiation():
    """SeanergysLogger instantiates with defaults."""
    from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger

    logger = SeanergysLogger()
    assert logger is not None
    assert logger.name == "Senergys logs"
    assert logger.level == logging.INFO


def test_logger_custom_name():
    """SeanergysLogger accepts custom name."""
    from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger

    logger = SeanergysLogger(name="custom-logger")
    assert logger.name == "custom-logger"


def test_logger_log_levels():
    """SeanergysLogger has standard log methods."""
    from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger

    logger = SeanergysLogger()
    assert hasattr(logger, "info")
    assert hasattr(logger, "warning")
    assert hasattr(logger, "error")
    assert hasattr(logger, "debug")
    assert callable(logger.info)
    assert callable(logger.error)
