"""Strict, versioned technical contract. No application vocabulary."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

Positive = Annotated[int, Field(strict=True, gt=0)]
Size = Annotated[int, Field(strict=True, ge=0)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[str, Field(strict=True, min_length=1)]


def safe_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(c) < 32 for c in value)
        or ":" in value
    ):
        raise ValueError("auftrag_ungueltig: relative canonical path required")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class Inline(Contract):
    art: Literal["inline"]
    content_base64: str

    @field_validator("content_base64")
    @classmethod
    def base64_valid(cls, value):
        try:
            base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("auftrag_ungueltig: invalid base64") from None
        return value


class Reference(Contract):
    art: Literal["verweis"]
    url: str

    @field_validator("url")
    @classmethod
    def https_only(cls, value):
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("auftrag_ungueltig: authorized https reference required")
        return value


class Material(Contract):
    id: Name
    ziel: str
    sha256: Digest
    bytes: Size
    quelle: Annotated[Inline | Reference, Field(discriminator="art")]
    _path = field_validator("ziel")(safe_path)


class Tool(Contract):
    name: Name
    parameter: dict[str, Any]


class Output(Contract):
    modus: Literal["text", "json", "datei"]
    datei: str | None = None
    json_schema: dict[str, Any] | None = None
    artefakte: list[str]
    # Targets the run may write; what lies there comes back with provenance
    # like a declared target, absence is no antwort_fehlt (ERKNEUR P2-1).
    artefakte_optional: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def drop_empty_optional(self, handler):
        # Empty means absent: the A3 wire digest and the output contract shown
        # to the model stay byte-identical for every run without the field.
        data = handler(self)
        if not self.artefakte_optional:
            data.pop("artefakte_optional", None)
        return data

    @property
    def ziele(self) -> list[str]:
        """Every path the run may write: declared, optional, answer file."""
        return self.artefakte + self.artefakte_optional + ([self.datei] if self.datei else [])

    @model_validator(mode="after")
    def validate_output(self):
        if (self.datei is not None) != (self.modus == "datei"):
            raise ValueError("auftrag_ungueltig: file mode requires file only")
        if (self.json_schema is not None) != (self.modus == "json"):
            raise ValueError("auftrag_ungueltig: JSON mode requires schema only")
        if self.modus == "datei":
            safe_path(self.datei)
        if self.modus == "json":
            from jsonschema import Draft202012Validator

            try:
                Draft202012Validator.check_schema(self.json_schema)
            except Exception:
                raise ValueError("auftrag_ungueltig: invalid JSON schema") from None

            def check(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in ("$ref", "$dynamicRef") and not value.startswith("#"):
                            raise ValueError("auftrag_ungueltig: external schema reference")
                        check(value)
                elif isinstance(node, list):
                    for value in node:
                        check(value)

            check(self.json_schema)
        for path in self.artefakte:
            safe_path(path)
        if len(set(self.artefakte)) != len(self.artefakte):
            raise ValueError("auftrag_ungueltig: duplicate artifact target")
        for path in self.artefakte_optional:
            safe_path(path)
        if len(set(self.artefakte_optional)) != len(self.artefakte_optional):
            raise ValueError("auftrag_ungueltig: duplicate artifact target")
        # An optional target must not equal, contain or lie inside a declared
        # one or the answer file: the same bytes would be required and optional.
        fest = self.artefakte + ([self.datei] if self.datei else [])
        for a in self.artefakte_optional:
            if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for b in fest):
                raise ValueError("auftrag_ungueltig: optional target overlaps declared target")
        return self


class Review(Contract):
    auftrag: Name
    systemtext: str = ""
    model: Name
    werkzeuge: list[Tool]
    ausgabe: Output
    max_turns: Positive
    max_tokens: Positive


class Limits(Contract):
    zeit_s: Positive
    tokens_gesamt: Positive


class Run(Contract):
    schema_version: Literal["agentischer-lauf/1"] = Field(alias="schema")
    lauf_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
    systemtext: str = ""
    material: list[Material]
    werkzeuge: list[Tool]
    ausgabe: Output
    pruefung: Review | None = None
    grenzen: Limits

    @model_validator(mode="after")
    def no_collisions(self):
        ids = [m.id for m in self.material]
        paths = [m.ziel for m in self.material]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("auftrag_ungueltig: duplicate material")
        for a in paths:
            if any(b.startswith(a + "/") for b in paths):
                raise ValueError("auftrag_ungueltig: material path collision")
        return self


def validate_request(values):
    """Before legacy coercion/defaults: explicit contradictory options are errors."""
    if not isinstance(values, dict) or values.get("agentischer_lauf") is None:
        return values
    forbidden = {
        "research_mode",
        "research_instructions",
        "literatur_planer",
        "privacy_allowlist",
        "depth",
        "strategy",
        "max_hops",
        "confidence_threshold",
        "parallel_searches",
        "source_filter",
        "output_path",
        "cloud_overflow",
    }
    # Full model_dump includes unset legacy defaults. Only an effective
    # non-default value requests legacy behavior in an agentic run.
    from src.models import ResearchRequest

    if any(
        values.get(name) is not None
        and values[name] != ResearchRequest.model_fields[name].default
        for name in forbidden
    ):
        raise ValueError("auftrag_ungueltig: legacy research options")
    for name in ("max_turns", "max_tokens"):
        if type(values.get(name)) is not int or values[name] <= 0:
            raise ValueError("auftrag_ungueltig: explicit positive integer required")
    if type(values.get("antwort_im_text")) is not bool or values.get("async_mode") is not False:
        raise ValueError("auftrag_ungueltig: explicit output/async flags required")
    if not isinstance(values.get("query"), str) or not values["query"].strip():
        raise ValueError("auftrag_ungueltig: empty instruction")
    if not isinstance(values.get("model"), str) or not values["model"].strip():
        raise ValueError("auftrag_ungueltig: explicit model required")
    run = Run.model_validate(values["agentischer_lauf"])
    if values["antwort_im_text"] != (run.ausgabe.modus != "datei"):
        raise ValueError("auftrag_ungueltig: output mode mismatch")
    return values
