import logging


class SafeFormatter(logging.Formatter):
    """Log operational events, never framework request/task arguments or exceptions.

    Application log statements must contain only fixed messages and opaque IDs.
    Unreviewed framework messages are represented by logger/level, not payloads.
    """

    def format(self, record):
        message = record.getMessage() if record.name == "evidence.tasks" and not record.exc_info else "event"
        error = f" error={record.exc_info[0].__name__}" if record.exc_info else ""
        status = getattr(record, "status_code", None)
        return f"{self.formatTime(record)} {record.levelname} {record.name} {message}{error}" + (f" status={status}" if status else "")
