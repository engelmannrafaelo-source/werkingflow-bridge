"""Versioned wire contracts; customer text never belongs in job metadata."""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

BerichtId = Annotated[str, Field(pattern=r"^[a-z0-9-]{8,80}$")]
SchrittName = Annotated[str, Field(pattern=(
    r"^(erkunder-[123]|harmonisierung|pruefung|"
    r"(?:harmonisierung|pruefung)-korrektur(?:-[2-5])?)$"
))]


class Vertrag(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Datenstand(Vertrag):
    von: date
    bis: date
    heute: date


# Eingangsordner unter eingang/. `pruefwissen/` traegt ausgewaehlte Fachdokumente
# der Pruefbibliothek (je Datei ein Dokument), getrennt von den Kundenunterlagen.
EINGANGSORDNER = ("messdaten", "unterlagen", "plan", "pruefwissen")


class Datei(Vertrag):
    ziel: str = Field(pattern=rf"^({'|'.join(EINGANGSORDNER)})/[^/]{{1,200}}$")
    url: str = Field(pattern=r"^https://")
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    bytes: int = Field(ge=0, strict=True)

    @field_validator("ziel")
    @classmethod
    def safe_target(cls, value: str) -> str:
        steuerzeichen = any(ord(c) < 32 or ord(c) == 127 for c in value)
        if ".." in value or "\\" in value or steuerzeichen:
            raise ValueError("ungueltiger Dateipfad")
        return value


class Pruefpunkt(Vertrag):
    anlage: str = Field(min_length=1)
    dokument_id: str = Field(min_length=1)
    fehlerbild_id: str = Field(min_length=1)
    fehlende_kanaele: list[str]


ModellName = Literal["claude-sonnet-5-5", "claude-haiku-5-5"]
VORGABE_MODELL: ModellName = "claude-sonnet-5-5"


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
    korrekturkreis: int = Field(ge=1, le=5, strict=True)
    pruefliste: list[Pruefpunkt] = Field(default_factory=list)
    modell: ModellName = VORGABE_MODELL


class Tokens(Vertrag):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int = Field(ge=0)
    cache_creation: int = Field(ge=0)


class Lesezugriff(Vertrag):
    """Ein Werkzeugaufruf des Modells auf einen Pfad unter eingang/ (kein Inhalt).

    `bash` heisst: der Pfad steht in einem Shell-Befehl. Das ist ein Hinweis,
    kein Lesebeweis; Lesen aus Python-Skripten bleibt unsichtbar.
    """

    werkzeug: Literal["read", "grep", "glob", "bash"]
    pfad: str = Field(min_length=1)


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
    # Nur vorhanden, wenn der Platz die Zugriffe erhoben hat. Ein Platz ohne
    # Erhebung (alter Stand) liefert das Feld nicht, nie als leere Liste.
    lesezugriffe: list[Lesezugriff] | None = None

    @model_serializer(mode="wrap")
    def _ohne_unerhobene(self, handler: SerializerFunctionWrapHandler) -> dict:
        data = handler(self)
        if self.lesezugriffe is None:
            data.pop("lesezugriffe", None)
        return data


class Ausfall(Vertrag):
    schritt: SchrittName
    grund: str


class Ergebnis(Vertrag):
    schema_: Literal["erkunder-ergebnis/1"] = Field(alias="schema")
    bericht_id: BerichtId
    prompt_version: Literal[
        "erkunder-prompts/1", "erkunder-prompts/2", "erkunder-prompts/3"
    ]
    modell: ModellName
    schritte: list[Schritt]
    erkunder_ausgefallen: list[Ausfall]
    korrekturkreis_gelaufen: bool
    offene_befunde_anzahl: int | None = Field(ge=0)
    pruefstatus: Literal["offen", "widerspruchsfrei", "altauftrag_ungeprueft"]
    korrekturrunden: int | None = Field(ge=0, le=5)


class Texte(Vertrag):
    schema_: Literal["erkunder-texte/1"] = Field(alias="schema")
    bericht_id: BerichtId
    texte: dict[SchrittName, str]
    skripte: dict[SchrittName, dict[str, str]]
    # VERTRAG (kein stilles Kappen): Skripte werden je Schritt mit einem Budget
    # von 200 KB ausgeliefert. Wird gekuerzt, steht der Schritt in
    # `skripte_gekuerzt` (die letzte passende Datei ist dann abgeschnitten) und
    # jede Datei, die das Budget gar nicht mehr erreicht hat, in
    # `skripte_uebersprungen` (nie als leerer String in `skripte`). Der Aufrufer
    # (Energy) MUSS beide Felder auswerten und laut melden, wenn eines nicht leer ist.
    skripte_gekuerzt: list[SchrittName]
    skripte_uebersprungen: list[str] = Field(default_factory=list)
    gutachten_final: str
    pruefung_final: str


class Urteil(Vertrag):
    anlage: str
    fehlerbild_id: str
    dokument_id: str
    status: Literal["bestaetigt", "widerlegt", "teilweise", "nicht_pruefbar"]
    fehlende_kanaele: list[str]
    befund_verweis: str | None
    sicherheit: float = Field(ge=0, le=1, allow_inf_nan=False)
    begruendung: str = Field(min_length=1)
