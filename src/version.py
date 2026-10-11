"""Public, immutable build provenance and wire contract revision."""
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

BUILD_METADATA = Path(__file__).with_name("build-metadata.json")
CONTRACT_VERSION = "1"
router = APIRouter()


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    built_at: str = Field(json_schema_extra={"format": "date-time"})
    contract_version: Literal["1"]

    @field_validator("built_at")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("Build timestamp must include timezone")
        return value


@router.get("/v1/version", response_model=VersionResponse)
async def get_version() -> VersionResponse:
    try:
        metadata = json.loads(BUILD_METADATA.read_text())
        return VersionResponse(
            git_sha=metadata["git_sha"],
            built_at=metadata["built_at"],
            contract_version=CONTRACT_VERSION,
        )
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        raise HTTPException(503, "Build metadata unavailable") from None
