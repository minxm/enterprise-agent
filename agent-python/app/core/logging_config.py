import logging

from app.core.request_context import current_request_id

_factory_installed = False


class RequestIdLogFilter(logging.Filter):
    """Attach requestId on the handler.

    A filter on the root logger is skipped when a child logger propagates
    the record, which makes ``%(requestId)s`` raise during formatting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "requestId", None):
            record.requestId = current_request_id() or "-"
        return True


def _install_record_factory() -> None:
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.requestId = current_request_id() or "-"
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


def _ensure_filter(handler: logging.Handler) -> None:
    if not any(isinstance(item, RequestIdLogFilter) for item in handler.filters):
        handler.addFilter(RequestIdLogFilter())


def configure_logging() -> None:
    _install_record_factory()
    root = logging.getLogger()
    for handler in root.handlers:
        _ensure_filter(handler)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [requestId=%(requestId)s] %(name)s: %(message)s")
        )
        _ensure_filter(handler)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
