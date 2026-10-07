"""Authenticated worker-facing access to report texts and explicit deletion."""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import TypeAdapter, ValidationError

from src.auth import security, verify_api_key
from src.erkunder.models import BerichtId
from src.erkunder.zugang import erkunder_schluessel_erlaubt, intern_config

router = APIRouter()


async def _forward(
    method: str,
    action: str,
    bericht_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
):
    await verify_api_key(request, credentials)
    erkunder_schluessel_erlaubt(request, credentials)
    try:
        TypeAdapter(BerichtId).validate_python(bericht_id)
    except ValidationError:
        raise HTTPException(400, "ungueltige bericht_id") from None
    url, headers = intern_config()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(
                method, f"{url}/{action}/{bericht_id}", headers=headers
            )
    except httpx.HTTPError:
        raise HTTPException(502, "Erkunder-Leitstand nicht erreichbar") from None
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Erkunder-Anfrage fehlgeschlagen")
    return response.json()


@router.get("/v1/erkunder/bericht/{bericht_id}/ergebnis")
async def ergebnis(
    bericht_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    return await _forward("GET", "ergebnis", bericht_id, request, credentials)


@router.post("/v1/erkunder/bericht/{bericht_id}/aufraeumen")
async def aufraeumen(
    bericht_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    return await _forward("POST", "aufraeumen", bericht_id, request, credentials)
