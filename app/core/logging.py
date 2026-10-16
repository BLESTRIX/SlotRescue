import json
import logging

from app.services.common import request_id


class JsonLog(logging.Formatter):
    def format(self, record):
        result = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id.get(),
        }
        if hasattr(record, "job_id"):
            result["job_id"] = record.job_id
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result)


def configure_logging():
    logger = logging.getLogger("slotrescue")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLog())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
