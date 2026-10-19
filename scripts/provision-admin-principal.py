"""Offline bootstrap: run only in admin provisioning environment, never an app.

BRIDGE_ADMIN_TOKEN must be a newly generated secret supplied by Infisical.
Only its hash is persisted; the script emits no token or digest. No HTTP bypass.
"""

import asyncio
import hashlib
import os

import asyncpg


async def main():
    token = os.environ["BRIDGE_ADMIN_TOKEN"]
    if len(token) < 32 or any(
        token == os.environ.get(name)
        for name in (
            "BRIDGE_SERVICE_TOKEN",
            "AI_BRIDGE_API_KEY",
            "SANDBOX_BROKER_TOKEN",
        )
    ):
        raise RuntimeError("Admin credential must be strong and independent")
    conn = await asyncpg.connect(os.environ["BRIDGE_DB_URL"])
    try:
        await conn.execute(
            """INSERT INTO service_principals
               (name, token_hash, token_prefix, allowed_apps, allowed_paths, scopes)
               VALUES ($1, $2, $3, '{}', '{}', '{bridge:admin}')""",
            os.environ["BRIDGE_ADMIN_PRINCIPAL_NAME"],
            hashlib.sha256(token.encode()).hexdigest(),
            token[:8],
        )
    finally:
        await conn.close()
    print(
        "Admin principal provisioned; keep credential only in admin Infisical workspace"
    )


if __name__ == "__main__":
    asyncio.run(main())
