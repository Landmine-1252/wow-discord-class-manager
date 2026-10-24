"""JSON logs with a final redaction pass, including exception messages."""

import json
import logging
import re
from datetime import UTC, datetime


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()):
        self.secrets = tuple(secret for secret in secrets if secret)

    def __call__(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"https://(?:[\w.-]+\.)?discord(?:app)?\.com/api/"
            r"(?:v\d+/)?webhooks/[^\s\"<>]+",
            "[REDACTED_WEBHOOK]",
            value,
        )
        value = re.sub(
            r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}",
            "[REDACTED_TOKEN]",
            value,
        )
        # SDK rate-limit warnings may contain REST paths with individual IDs.
        value = re.sub(r"(/(?:members|users)/)\d+", r"\1[REDACTED]", value)
        # Interaction callback URLs contain short-lived bearer tokens too.
        return re.sub(r"(/interactions/\d+/)[^/\s]+", r"\1[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redactor(record.getMessage()),
        }
        if record.exc_info:
            data["exception"] = self.redactor(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str, *secrets: str) -> Redactor:
    redactor = Redactor(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(redactor))
    # Voice is intentionally outside this bot's scope; do not imply that its
    # optional dependencies are missing requirements. Keep all other warnings.
    handler.addFilter(
        lambda record: not (
            record.name == "discord.client"
            and record.getMessage()
            in {
                "PyNaCl is not installed, voice will NOT be supported",
                "davey is not installed, voice will NOT be supported",
            }
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library debug logs may contain Gateway payloads. Never enable them.
    for name in ("discord", "aiohttp", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return redactor
