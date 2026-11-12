import structlog

from issue_publisher.config import Settings
from issue_publisher.logging import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings)
    structlog.get_logger().info("worker started", log_level=settings.LOG_LEVEL)


if __name__ == "__main__":
    main()
