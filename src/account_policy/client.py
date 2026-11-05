"""Fresh authority read per admission. No stale allow-cache and no alternate authority."""
import os
import httpx
from .model import validate

class PolicyUnavailable(RuntimeError):
    pass

def connection():
    url = os.environ.get("BRIDGE_ACCOUNT_POLICY_URL", "").rstrip("/")
    token = os.environ.get("BRIDGE_ACCOUNT_POLICY_TOKEN", "")
    if not url or not token:
        raise PolicyUnavailable("bridge account policy authority not configured")
    return url, {"X-Bridge-Service-Token": token}

async def fetch_policy():
    url, headers = connection()
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise PolicyUnavailable("bridge account policy authority HTTP " + str(response.status_code))
        return validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise PolicyUnavailable("bridge account policy unavailable or invalid") from exc

def fetch_policy_sync():
    url, headers = connection()
    try:
        with httpx.Client(timeout=3.0, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        if response.status_code != 200:
            raise PolicyUnavailable("bridge account policy authority HTTP " + str(response.status_code))
        return validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise PolicyUnavailable("bridge account policy unavailable or invalid") from exc
