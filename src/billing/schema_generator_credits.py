"""Own-user generator downloads: genuine user JWT AND explicitly enrolled app.

Tools owns result storage and verifies its owner before calling. This endpoint
never trusts X-User-ID, body user IDs, arbitrary plans or global service tokens.
"""
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from src.api_auth import AuthClaims, require_jwt
from src.db.client import get_pool
from src.principals import get_principal_row_by_hash, hash_token
from src.billing.project_credits_service import CreditsExhaustedError, consume_credit_on_connection

logger = logging.getLogger(__name__)

router = APIRouter(tags=['billing'])
PATH = '/v1/billing/schema-generator/credit-consumption'
APP = 'werking-schema-generator'
PLANS = ('report-check-credit', 'report-check-credit-5', 'report-check-credit-1')


class ConsumeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str = Field(pattern=r'^schema-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


async def generator_writer(
    x_schema_generator_key: Optional[str] = Header(default=None),
    x_app_id: Optional[str] = Header(default=None),
) -> str:
    if not x_schema_generator_key or len(x_schema_generator_key) > 4096:
        raise HTTPException(401, 'Missing generator credential')
    try:
        p = await get_principal_row_by_hash(hash_token(x_schema_generator_key))
    except Exception as e:
        logger.warning("Generator authority lookup failed: %s", type(e).__name__)
        raise HTTPException(503, 'Generator authority unavailable') from None
    if not p:
        raise HTTPException(401, 'Invalid generator credential')
    if (x_app_id != APP or APP not in p['allowed_apps'] or PATH not in p['allowed_paths']
            or '*' in p['allowed_apps'] or '*' in p['allowed_paths']):
        raise HTTPException(403, 'Generator scope denied')
    return p['id']


async def consume_once(user_id: uuid.UUID, project_id: str) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Same lock for every plan and worker; lifetime is the transaction.
            # Hash collisions only serialize unrelated requests, never authorize them.
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', f'{APP}:{user_id}:{project_id}')
            receipt = await conn.fetchval(
                'SELECT receipt FROM schema_generator_credit_receipts WHERE user_id=$1 AND project_id=$2',
                user_id, project_id,
            )
            if receipt is not None:
                return json.loads(receipt) if isinstance(receipt, str) else dict(receipt)
            result = None
            for plan in PLANS:
                try:
                    result = await consume_credit_on_connection(conn, user_id, plan, project_id, allowed_credit_plan_ids=PLANS)
                    break
                except CreditsExhaustedError:
                    continue
            if result is None:
                raise CreditsExhaustedError('report-check-credit')
            await conn.execute(
                '''INSERT INTO schema_generator_credit_receipts (user_id, project_id, credit_id, receipt)
                   VALUES ($1,$2,$3,$4::jsonb)''',
                user_id, project_id, uuid.UUID(result['creditId']), json.dumps(result),
            )
            return result


@router.post(PATH)
async def consume_generator_credit(
    body: ConsumeRequest,
    response: Response,
    claims: AuthClaims = Depends(require_jwt),
    principal_id: str = Depends(generator_writer),
) -> dict:
    if not claims.is_user or not claims.user_id:
        raise HTTPException(403, 'User session required')
    try:
        user_id = uuid.UUID(claims.user_id)
    except ValueError:
        raise HTTPException(403, 'Invalid user session') from None
    try:
        receipt = await consume_once(user_id, body.project_id)
    except CreditsExhaustedError:
        raise HTTPException(402, 'No available generator download credit') from None
    except Exception as e:
        # Log only the exception class, never exception details or user data.
        logger.warning("Generator credit consumption failed: %s", type(e).__name__)
        raise HTTPException(503, 'Credit consumption unavailable') from None
    response.headers['Cache-Control'] = 'no-store'
    return receipt
