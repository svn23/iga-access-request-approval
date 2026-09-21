import logging
import logging.handlers
import json
from datetime import datetime
from pythonjsonlogger import jsonlogger

def setup_logging():
    """Configure structured JSON logging for all modules"""

    # Create logs directory if doesn't exist
    import os
    os.makedirs("logs", exist_ok=True)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # JSON formatter for structured logging
    json_formatter = jsonlogger.JsonFormatter(
        fmt='%(timestamp)s %(level)s %(name)s %(message)s %(request_id)s %(user)s %(duration)s'
    )

    # Console handler (JSON)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(json_formatter)
    root_logger.addHandler(console_handler)

    # File handler - all logs (JSON)
    file_handler = logging.handlers.RotatingFileHandler(
        'logs/teams-bot.log',
        maxBytes=10485760,  # 10MB
        backupCount=10
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # File handler - errors only
    error_handler = logging.handlers.RotatingFileHandler(
        'logs/teams-bot-errors.log',
        maxBytes=10485760,
        backupCount=5
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(json_formatter)
    root_logger.addHandler(error_handler)

    # File handler - requests/responses
    request_handler = logging.handlers.RotatingFileHandler(
        'logs/teams-bot-requests.log',
        maxBytes=10485760,
        backupCount=10
    )
    request_handler.setLevel(logging.DEBUG)
    request_handler.setFormatter(json_formatter)
    request_logger = logging.getLogger('teams_bot.requests')
    request_logger.addHandler(request_handler)

    return root_logger


class StructuredLogger:
    """Wrapper for structured logging with request context"""

    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.request_id = None
        self.user = None

    def set_context(self, request_id, user=None):
        self.request_id = request_id
        self.user = user

    def info(self, message, **kwargs):
        self.logger.info(message, extra={
            'timestamp': datetime.utcnow().isoformat(),
            'request_id': self.request_id,
            'user': self.user,
            **kwargs
        })

    def error(self, message, **kwargs):
        self.logger.error(message, extra={
            'timestamp': datetime.utcnow().isoformat(),
            'request_id': self.request_id,
            'user': self.user,
            **kwargs
        })

    def debug(self, message, **kwargs):
        self.logger.debug(message, extra={
            'timestamp': datetime.utcnow().isoformat(),
            'request_id': self.request_id,
            'user': self.user,
            **kwargs
        })

    def warning(self, message, **kwargs):
        self.logger.warning(message, extra={
            'timestamp': datetime.utcnow().isoformat(),
            'request_id': self.request_id,
            'user': self.user,
            **kwargs
        })
