"""Execution context set only inside a reaper-owned document subprocess.

Legacy in-process callers keep their existing protective limits until they too
have a resource owner. This flag is not a caller field or environment switch.
"""

managed = False


def vision_timeout():
    if not managed:
        return 300.0
    import httpx
    # TCP establishment may fail; an established inference has no local clock.
    return httpx.Timeout(None, connect=10.0)


provider_directory = None


def provider_started():
    if provider_directory is None:
        return None
    import uuid
    from .document_process import write_json
    path = provider_directory / ("provider-pending-" + uuid.uuid4().hex)
    write_json(path, {"outcome": "unknown"})
    return path


def provider_finished(path):
    if path is not None:
        import os
        path.unlink()
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
