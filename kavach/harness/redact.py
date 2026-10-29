"""Redaction for everything we log or persist outside the answer files (traces, incidents, casefiles, server logs).

redact(obj) returns a deep copy of dicts, lists, tuples and strings with:
  - the value of every environment variable whose name contains KEY, SECRET, PASSWORD or TOKEN (values of 8+ chars,
    read from os.environ at call time) replaced by [REDACTED:<VAR NAME>];
  - bearer tokens replaced by "Bearer [REDACTED]";
  - email addresses masked to the first character of the local part plus the domain ("a***@gmail.com");
  - string values under keys that name a credential (authorization, api_key, password, secret, token) replaced.
Email domains on their own (P_emaildomain values such as "gmail.com") are data and are left alone.

install_logging_filter() applies the same masking to every log record created in the process.
"""
import logging
import os
import re

SECRET_NAME = re.compile(r"KEY|SECRET|PASSWORD|TOKEN", re.I)
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
SECRET_KEY = re.compile(r"(?i)^(authorization|proxy-authorization|x-api-key|api[_-]?key|.*secret.*|.*password.*|token|.*_token|token_.*)$")


def _secrets() -> list[tuple[str, str]]:
    out = [(k, v.strip()) for k, v in os.environ.items() if SECRET_NAME.search(k) and v and len(v.strip()) >= 8]
    return sorted(out, key=lambda kv: -len(kv[1]))   # longest first, so a secret containing another is masked whole


def redact_text(s: str, secrets: list[tuple[str, str]] | None = None) -> str:
    if not s:
        return s
    for name, val in (secrets if secrets is not None else _secrets()):
        if val in s:
            s = s.replace(val, f"[REDACTED:{name}]")
    s = BEARER.sub("Bearer [REDACTED]", s)
    return EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", s)


def redact(obj, _secrets_cache=None):
    sec = _secrets() if _secrets_cache is None else _secrets_cache
    if isinstance(obj, str):
        return redact_text(obj, sec)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and isinstance(v, str) and v and SECRET_KEY.match(k):
                out[k] = "[REDACTED]"
            else:
                out[redact_text(k, sec) if isinstance(k, str) else k] = redact(v, sec)
        return out
    if isinstance(obj, list):
        return [redact(v, sec) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v, sec) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return [redact(v, sec) for v in obj]
    if isinstance(obj, BaseException):
        return redact_text(f"{type(obj).__name__}: {obj}", sec)
    return obj   # numbers, bools, None, timestamps: nothing to mask


class RedactingFilter(logging.Filter):
    """Masks a record's message and arguments in place. Always lets the record through."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def _redact_record(record: logging.LogRecord) -> None:
    if getattr(record, "_kavach_redacted", False):
        return
    sec = _secrets()
    # mask the message and each argument in place (formatters such as uvicorn's access log need args as they were)
    if isinstance(record.msg, str):
        record.msg = redact_text(record.msg, sec)
    else:
        record.msg = redact_text(str(record.msg), sec)
    if isinstance(record.args, tuple):
        record.args = tuple(redact_text(a, sec) if isinstance(a, str) else a for a in record.args)
    elif isinstance(record.args, dict):
        record.args = {k: (redact_text(v, sec) if isinstance(v, str) else v) for k, v in record.args.items()}
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = redact_text(record.exc_text)
    record._kavach_redacted = True


_installed = False
FILTER = RedactingFilter()


def install_logging_filter(logger_names=("", "kavach", "uvicorn", "uvicorn.error", "uvicorn.access")) -> RedactingFilter:
    """Mask every log record created from now on (record factory), and attach the filter to the named loggers and
    their handlers. Idempotent."""
    global _installed
    if not _installed:
        base = logging.getLogRecordFactory()

        def factory(*a, **kw):
            rec = base(*a, **kw)
            _redact_record(rec)
            return rec

        logging.setLogRecordFactory(factory)
        _installed = True
    for name in logger_names:
        lg = logging.getLogger(name)
        if FILTER not in lg.filters:
            lg.addFilter(FILTER)
        for h in lg.handlers:
            if FILTER not in h.filters:
                h.addFilter(FILTER)
    return FILTER
