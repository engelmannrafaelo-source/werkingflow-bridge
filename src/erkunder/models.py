"""Versioned wire contracts; customer text never belongs in job metadata."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BerichtId = Annotated[str, Field(pattern=r"^[a-z0-9-]{8,80}$")]
SchrittName = Literal[
    "erkunder-1",
    "erkunder-2",
    "erkunder-3",
    "harmonisierung",
    "pruefung",
    "harmonisierung-korrektur",
    "pruefung-korrektur",
]


class Vertrag(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Datenstand(Vertrag):
    von: date
    bis: date
    heute: date


class Datei(Vertrag):
    ziel: str = Field(pattern=r"^(messdaten|unterlagen|plan)/[^/]{1,200}$")
    url: str = Field(pattern=r"^https://")
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    bytes: int = Field(ge=0, strict=True)

    @field_validator("ziel")
    @classmethod
    def safe_target(cls, value: str) -> str:
        if ".." in value or "\x00" in value or "\\" in value:
            raise ValueError("ungueltiger Dateipfad")
        return value


class Auftrag(Vertrag):
    schema_: Literal["erkunder-auftrag/1"] = Field(alias="schema")
    bericht_id: BerichtId
    gegenstand: str
    datenstand: Datenstand
    auftrag: str | None
    zweck: str
    vorwissen_md: str
    vertiefung_md: str | None
    dateien: list[Datei]
    korrekturkreis: Literal[1]

    @field_validator("korrekturkreis", mode="before")
    @classmethod
    def exact_one(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("korrekturkreis muss 1 sein")
        return value


class Tokens(Vertrag):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int = Field(ge=0)
    cache_creation: int = Field(ge=0)


class Schritt(Vertrag):
    name: SchrittName
    versuch: int = Field(ge=1, le=2)
    status: Literal["ok", "abbruch"]
    abbruch_grund: str | None = Field(
        pattern=(r"^(zeit|speicher|cli_fehler|geheimnis_im_ergebnis|"
                 r"platz_neustart|konto|unbekannt)(: .*)?$")
    )
    dauer_s: float = Field(ge=0)
    zuege: int = Field(ge=0)
    tokens: Tokens
    ram_spitze_mb: float = Field(ge=0)
    worker: str


class Ausfall(Vertrag):
    schritt: SchrittName
    grund: str


class Ergebnis(Vertrag):
    schema_: Literal["erkunder-ergebnis/1"] = Field(alias="schema")
    bericht_id: BerichtId
    prompt_version: Literal["erkunder-prompts/1"]
    modell: Literal["claude-sonnet-5-5"]
    schritte: list[Schritt]
    erkunder_ausgefallen: list[Ausfall]
    korrekturkreis_gelaufen: bool


class Texte(Vertrag):
    schema_: Literal["erkunder-texte/1"] = Field(alias="schema")
    bericht_id: BerichtId
    texte: dict[SchrittName, str]
    skripte: dict[SchrittName, dict[str, str]]
    skripte_gekuerzt: list[SchrittName]
    gutachten_final: str
    pruefung_final: str
