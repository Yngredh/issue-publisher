import logging

import structlog

from issue_publisher.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog to emit JSON logs at the level defined in `settings`.

    `event_id` (the case `origin_event_id`) is the correlation id: the consumer binds it with
    `structlog.contextvars.bind_contextvars(event_id=...)` while it processes each message,
    and it is added to every log line.
    """
    level = logging.getLevelNamesMapping()[settings.LOG_LEVEL.upper()]
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=settings.SERVICE_NAME)
