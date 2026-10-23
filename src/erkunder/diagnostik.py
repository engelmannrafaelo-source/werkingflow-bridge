"""Value-free failure evidence. Raw SDK stderr may contain credentials/content.

Only structural error classes, exit codes and fixed technical diagnoses cross
this boundary. An arbitrary 'redacted' stderr line is still customer content.
"""
import re

from .ereignisse import snapshot

STEPS = {"daten", "wiederaufnahme", "erkunder-1", "erkunder-2", "erkunder-3",
         "harmonisierung", "pruefung", "harmonisierung-korrektur", "pruefung-korrektur",
         *(f"{kind}-korrektur-{n}" for kind in ("harmonisierung", "pruefung") for n in range(2, 6))}
KINDS = {"ProcessError", "CLIConnectionError", "CLINotFoundError", "CLIJSONDecodeError",
         "MessageParseError", "RuntimeError", "SDKResultFailure", "Exception", "TimeoutError",
         "OSError", "PermissionError", "ConnectionResetError", "ConnectionError", "HTTPError",
         "ReadError", "ConnectError", "ReadTimeout", "ConnectTimeout", "ValueError", "KeyError",
         "BrokenPipeError", "FileNotFoundError", "MemoryError", "CancelledError",
         "ModuleNotFoundError", "ImportError", "ExceptionGroup", "BaseExceptionGroup",
         "TypeError", "AttributeError", "SyntaxError", "ChildProcessError"}
REASONS = {"stille", "zeit", "speicher", "cli_fehler", "geheimnis_im_ergebnis", "platz_neustart",
           "Ergebnis-Integritaet", "Nachweis-Integritaet", "Wiederanhaengen", "konto", "unbekannt"}
LINES = {"Connection reset by peer", "Connection refused", "Permission denied", "Broken pipe",
         "ENOTFOUND", "ECONNRESET", "ETIMEDOUT", "certificate verify failed",
         "Invalid API key", "authentication_error", "rate_limit_error", "overloaded_error",
         "OAuth token has expired", "Invalid bearer token", "model_not_found",
         "Could not resolve proxy", "Proxy connection ended before receiving CONNECT response",
         "EACCES", "ENOENT", "No module named", "error: unknown option",
         "Could not find Claude Code", "not_found_error"}
CODES = {"auth_failed", "model_unavailable", "proxy_failed", "network_failed",
         "permission_denied", "dependency_missing", "cli_start_failed",
         "rate_limited", "upstream_overloaded", "child_diagnostic_missing"}
LINE_CODES = {
    "Invalid API key": "auth_failed", "authentication_error": "auth_failed",
    "OAuth token has expired": "auth_failed", "Invalid bearer token": "auth_failed",
    "model_not_found": "model_unavailable",
    "HTTP 401": "auth_failed", "HTTP 407": "proxy_failed",
    "HTTP 429": "rate_limited",
    "Could not resolve proxy": "proxy_failed",
    "Proxy connection ended before receiving CONNECT response": "proxy_failed",
    "Permission denied": "permission_denied", "EACCES": "permission_denied",
    "ENOENT": "cli_start_failed", "Could not find Claude Code": "cli_start_failed",
    "error: unknown option": "cli_start_failed", "No module named": "dependency_missing",
    "rate_limit_error": "rate_limited", "overloaded_error": "upstream_overloaded",
    **{name: "network_failed" for name in ("Connection reset by peer", "Connection refused",
       "Broken pipe", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT", "certificate verify failed")},
}
TYPE_CODES = {"ModuleNotFoundError": "dependency_missing", "ImportError": "dependency_missing",
              "PermissionError": "permission_denied", "CLINotFoundError": "cli_start_failed",
              "FileNotFoundError": "cli_start_failed", "SyntaxError": "cli_start_failed"}


def technical_line(text) -> str | None:
    if not isinstance(text, str):
        return None
    # Classify the last recognizable technical line; never return the input.
    for line in reversed(text[-8192:].splitlines()):
        for known in sorted(LINES):
            if known.lower() in line.lower():
                return known
        match = re.search(r"(?:HTTP(?:/\d(?:\.\d)?)?\s+|API Error:\s*)([45]\d\d)\b", line)
        if match:
            return "HTTP " + match[1]
    return None


def details(value) -> dict:
    value = value if isinstance(value, dict) else {}
    result = {}
    if value.get("fehlerart") in KINDS:
        result["fehlerart"] = value["fehlerart"]
    if value.get("fehlercode") in CODES:
        result["fehlercode"] = value["fehlercode"]
    code = value.get("exitcode")
    if type(code) is int and -255 <= code <= 255:
        result["exitcode"] = code
    line = value.get("stderr_letzte_zeile")
    if isinstance(line, str) and (line in LINES or re.fullmatch(r"HTTP [45]\d\d", line)):
        result["stderr_letzte_zeile"] = line
    return result


def exception_details(error: BaseException) -> dict:
    # TaskGroup/SDK wrappers must not erase the actionable leaf exception.
    nested = getattr(error, "exceptions", ())
    if isinstance(nested, (tuple, list)):
        for child in nested:
            if isinstance(child, BaseException):
                leaf = exception_details(child)
                if leaf:
                    return leaf
    line = technical_line(getattr(error, "stderr", None) or str(error))
    kind = type(error).__name__
    return details({"fehlerart": kind,
                    "fehlercode": TYPE_CODES.get(kind) or LINE_CODES.get(line),
                    "exitcode": getattr(error, "exit_code", None),
                    "stderr_letzte_zeile": line})


def safe_reason(reason) -> str:
    prefix = str(reason or "unbekannt").split(";", 1)[0].split(":", 1)[0]
    if prefix not in REASONS:
        return "unbekannt"
    match = re.fullmatch(r"cli_fehler: SDK-Lauf: ([A-Za-z][A-Za-z0-9_]*)", str(reason))
    return str(reason) if match and match[1] in KINDS else prefix


def step_record(value: dict) -> dict:
    return {
        "name": value.get("name") if value.get("name") in STEPS else "unbekannt",
        "versuch": value.get("versuch") if type(value.get("versuch")) is int else 0,
        "abbruch_grund": safe_reason(value.get("abbruch_grund")) if value.get("status") != "ok" else None,
        **details(value.get("diagnose", value)),
        "status": value.get("status") if value.get("status") in {"ok", "abbruch", "laeuft"} else "unbekannt",
        "sdk": snapshot(value.get("sdk")),
    }


def summary(records: list[dict]) -> str:
    parts = []
    for value in records:
        record = step_record(value)
        extra = ", ".join(f"{key}={record[key]}" for key in
                          ("fehlerart", "exitcode", "stderr_letzte_zeile") if key in record)
        parts.append(f"{record['name']} Versuch {record['versuch']}: {record['abbruch_grund']}" +
                     (f" ({extra})" if extra else ""))
    return "; ".join(parts)


async def classify_stderr(stream) -> dict:
    """Drain stderr continuously; retain at most 8 KiB, return fixed classes only."""
    tail = b""
    diagnosis = {}
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-8192:]
        text = tail.decode("utf-8", errors="replace")
        line = technical_line(text)
        code = LINE_CODES.get(line)
        if code:
            diagnosis.update(fehlerart="ProcessError", fehlercode=code)
        for name in KINDS:
            if re.search(r"(?:^|\n)" + re.escape(name) + r":", text):
                diagnosis["fehlerart"] = name
                if name in TYPE_CODES:
                    diagnosis["fehlercode"] = TYPE_CODES[name]
    return details(diagnosis)
