"""Structured JSON logging with request correlation.

Field names follow Cloud Logging's special keys (`severity`, `message`,
`logging.googleapis.com/trace`) so that Cloud Run ingests these with no log router,
no agent and no parsing rules — stdout is the transport. Getting `severity` right is
what makes an ERROR show up as an error rather than as a text line the console colours
grey, and the trace field is what makes a request's logs collapse into one expandable
group in the console.

The correlation ID lives in a ContextVar rather than being threaded through call
signatures: it has to reach a log line emitted five frames deep in a repository without
every intervening function taking a `request_id` parameter it does not otherwise want.
"""

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.config import settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

# Attributes the stdlib puts on every record. Anything outside this set came from an
# `extra={...}` at the call site and belongs in the payload.
_STDLIB_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "msg",
    "message",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}

_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        trace_id = trace_id_var.get()
        if trace_id and settings.gcp_project_id:
            # Cloud Logging joins logs to traces only via this exact key.
            payload["logging.googleapis.com/trace"] = (
                f"projects/{settings.gcp_project_id}/traces/{trace_id}"
            )

        for key, value in record.__dict__.items():
            if key not in _STDLIB_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)

    if settings.app_env == "local":
        # JSON is for machines. Locally, a human is reading this.
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
    else:
        handler.setFormatter(CloudLoggingFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn installs its own handlers; without this every request logs twice, once
    # structured and once not.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
