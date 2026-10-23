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
