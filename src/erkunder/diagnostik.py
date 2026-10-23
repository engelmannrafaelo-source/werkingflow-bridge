"""Value-free failure evidence. Raw SDK stderr may contain credentials/content.

Only structural error classes, exit codes and fixed technical diagnoses cross
this boundary. An arbitrary 'redacted' stderr line is still customer content.
"""
import re

STEPS = {"daten", "wiederaufnahme", "erkunder-1", "erkunder-2", "erkunder-3",
         "harmonisierung", "pruefung", "harmonisierung-korrektur", "pruefung-korrektur",
         *(f"{kind}-korrektur-{n}" for kind in ("harmonisierung", "pruefung") for n in range(2, 6))}
KINDS = {"ProcessError", "CLIConnectionError", "CLINotFoundError", "CLIJSONDecodeError",
         "MessageParseError", "RuntimeError", "SDKResultFailure", "Exception", "TimeoutError",
         "OSError", "PermissionError", "ConnectionResetError", "ConnectionError", "HTTPError",
         "ReadError", "ConnectError", "ReadTimeout", "ConnectTimeout", "ValueError", "KeyError",
         "BrokenPipeError", "FileNotFoundError", "MemoryError", "CancelledError"}
REASONS = {"zeit", "speicher", "cli_fehler", "geheimnis_im_ergebnis", "platz_neustart",
           "Ergebnis-Integritaet", "Nachweis-Integritaet", "Wiederanhaengen", "konto", "unbekannt"}
LINES = {"Connection reset by peer", "Connection refused", "Permission denied", "Broken pipe",
         "ENOTFOUND", "ECONNRESET", "ETIMEDOUT", "certificate verify failed",
         "Invalid API key", "authentication_error", "rate_limit_error", "overloaded_error"}


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
    code = value.get("exitcode")
    if type(code) is int and -255 <= code <= 255:
        result["exitcode"] = code
    line = value.get("stderr_letzte_zeile")
    if isinstance(line, str) and (line in LINES or re.fullmatch(r"HTTP [45]\d\d", line)):
        result["stderr_letzte_zeile"] = line
    return result


def exception_details(error: Exception) -> dict:
    return details({"fehlerart": type(error).__name__,
                    "exitcode": getattr(error, "exit_code", None),
                    "stderr_letzte_zeile": technical_line(getattr(error, "stderr", None) or str(error))})


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
        "abbruch_grund": safe_reason(value.get("abbruch_grund")),
        **details(value.get("diagnose", value)),
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
