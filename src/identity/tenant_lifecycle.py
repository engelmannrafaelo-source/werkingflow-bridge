"""
Mandanten-Lebenszyklus — der fehlende Gegenpart zur Mandanten-Anlage.

Jeder Weg, der einen Nutzer anlegt, legt auch einen Mandanten an:

  * ``identity/routes.py::register``      → ``Personal tenant for <email>`` (neue UUID)
  * ``db/admin_routes.py::create_user``   → ``Auto-tenant for <email>``     (neue UUID)
  * ``sandbox/lease_service.py``          → ``JIT tenant for <app>``        (tenant_id = App-Name)

Bis 2026-09-02 gab es dazu keinen Gegenweg. ``DELETE /v1/users/{id}`` löschte die
Nutzerzeile; ``tenants.owner_user_id`` ist ``ON DELETE SET NULL``, der Mandant blieb
also als leere Hülle liegen. Registrierte sich dieselbe Adresse erneut, entstand ein
zweiter Mandant mit exakt demselben Namen. Befund auf der Prod-Bridge (02.09.2026):
13 Mandanten ohne Nutzer, sechs Namen mehrfach.

Warum löschen und nicht wiederverwenden
---------------------------------------
Einen leeren Mandanten bei erneuter Registrierung derselben Adresse wiederzuverwenden
wäre die naheliegende, aber falsche Antwort: ein Mandant trägt Budgets
(``project_budgets``), Abrechnungs-Stammdaten, Zustimmungen und Nutzungsdaten. Wer ihn
recycelt, vererbt die Reste des vorigen Inhabers der Adresse an eine neue Person. Ein
Mandant OHNE solche Daten ist dagegen verlustfrei löschbar — es geht nichts verloren,
was nicht ohnehin nur die leere Hülle war. Deshalb: löschen, wenn leer; behalten und
laut melden, wenn nicht.

Was einen Mandanten festhält
----------------------------
Die Liste der mandanten-gebundenen Tabellen wird NICHT gepflegt, sondern zur Laufzeit
aus dem Katalog gelesen (jede Basistabelle mit einer Spalte ``tenant_id``). Das ist
absichtlich fail-closed: eine künftige Tabelle blockiert die Löschung automatisch,
solange niemand sie hier bewusst freigibt. Eine gepflegte Positivliste wäre binnen
Wochen unvollständig — und ihr Fehler wäre stiller Datenverlust.

Bewusst NICHT blockierend ist einzig ``activities``: reines Protokoll, ohne
Fremdschlüssel und mit nullbarer Spalte. Seine Zeilen behalten die Mandanten-ID als
historische Zeichenkette; das ist bei einem Logbuch die richtige Semantik und der
Grund, warum die Bereinigung überhaupt greifen kann (fast jeder Mandant hat
Protokollzeilen).

Zur Einordnung der Schärfe dieser Prüfung: ``usage_events``, ``invoices``,
``subscriptions``, ``sandbox_leases`` u.a. hängen auch an ``users.id`` mit
``ON DELETE RESTRICT`` — ein Nutzer mit Verbrauch oder Rechnung lässt sich gar nicht
hart löschen (409). Die Prüfung hier fängt genau die Fälle, die dieser Schutz NICHT
sieht: Tabellen ohne Nutzer-Fremdschlüssel (``project_budgets``,
``sandbox_usage_events``) und Team-Mandanten mit weiteren Mitgliedern.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Sequence

logger = logging.getLogger(__name__)


# Mitgliedschafts-Tabelle: getrennt geprüft (Anzahl verbleibender Nutzer), nie als
# "Datenbestand" gewertet — sonst würde ein Mandant sich selbst blockieren.
_MEMBERSHIP_TABLE = "users"

# Bewusst nicht blockierend. Jede Aufnahme in diese Menge ist eine Entscheidung:
# "diese Zeilen sind Protokoll, kein Bestand". Siehe Modul-Docstring.
_NON_BLOCKING_TENANT_TABLES = frozenset({"activities"})

# Katalog-Namen sind keine Nutzereingabe, werden aber trotzdem verifiziert, bevor sie
# in SQL interpoliert werden. Alles, was nicht diesem Muster entspricht, ist ein Grund
# zum Abbruch — nicht zum Überspringen.
_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class TenantCleanupError(RuntimeError):
    """
    Die Mandanten-Bereinigung ist auf etwas gestoßen, das sie nicht einordnen kann.

    Wird nie geschluckt: der Aufrufer bricht die Transaktion ab, damit nicht der
    halbe Vorgang (Nutzer weg, Mandant unklar) stehen bleibt.
    """


@dataclass
class TenantCleanupResult:
    tenant_id: str
    deleted: bool
    remaining_members: int
    blocked_by: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tenantId": self.tenant_id,
            "deleted": self.deleted,
            "remainingMembers": self.remaining_members,
            "blockedBy": list(self.blocked_by),
        }


def _quote_ident(name: str) -> str:
    if not _SAFE_IDENT.match(name):
        raise TenantCleanupError(
            f"Tabellenname '{name}' aus dem Katalog entspricht nicht dem erwarteten "
            f"Muster — Bereinigung abgebrochen statt geraten."
        )
    return f'"{name}"'


async def tenant_scoped_tables(conn: Any) -> List[str]:
    """
    Alle Basistabellen mit einer ``tenant_id``-Spalte — die Bestandsträger.

    Ohne ``users`` und ohne die bewusst freigegebenen Protokoll-Tabellen.
    """
    rows = await conn.fetch(
        """
        SELECT c.table_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema
           AND t.table_name  = c.table_name
         WHERE c.table_schema = current_schema()
           AND c.column_name  = 'tenant_id'
           AND t.table_type   = 'BASE TABLE'
         ORDER BY c.table_name
        """
    )
    names = [r["table_name"] for r in rows]

    if _MEMBERSHIP_TABLE not in names:
        # users.tenant_id ist die Grundlage des ganzen Modells. Fehlt sie, sehen wir
        # nicht das Schema, das wir zu sehen glauben — dann wird nichts gelöscht.
        raise TenantCleanupError(
            "Schema-Invariante verletzt: keine Spalte users.tenant_id gefunden. "
            "Mandanten-Bereinigung abgebrochen."
        )

    return [
        n for n in names
        if n != _MEMBERSHIP_TABLE and n not in _NON_BLOCKING_TENANT_TABLES
    ]


async def tenant_member_count(conn: Any, tenant_id: str) -> int:
    """Verbleibende Nutzerzeilen des Mandanten — inklusive anonymisierter Stubs."""
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE tenant_id = $1", tenant_id
        )
    )


async def tenant_blocking_tables(
    conn: Any,
    tenant_id: str,
    tables: Sequence[str] | None = None,
) -> List[str]:
    """
    Tabellen, die für diesen Mandanten mindestens eine Zeile führen.

    Eine einzige Abfrage (UNION ALL über EXISTS) statt N Rundreisen.
    """
    table_names = list(tables) if tables is not None else await tenant_scoped_tables(conn)
    if not table_names:
        return []

    parts = [
        f"SELECT {_sql_literal(name)} AS table_name "
        f"WHERE EXISTS (SELECT 1 FROM {_quote_ident(name)} WHERE tenant_id = $1)"
        for name in table_names
    ]
    rows = await conn.fetch(" UNION ALL ".join(parts), tenant_id)
    return sorted(r["table_name"] for r in rows)


def _sql_literal(name: str) -> str:
    # Nur für die schon per _quote_ident validierten Katalognamen.
    _quote_ident(name)
    return f"'{name}'"


async def drop_tenant_if_orphaned(
    conn: Any,
    tenant_id: str,
    *,
    reason: str,
) -> TenantCleanupResult:
    """
    Löscht den Mandanten, wenn er nach dem Löschen des Nutzers leer UND datenlos ist.

    Muss in derselben Transaktion laufen wie das Löschen des Nutzers — sonst gäbe es
    ein Fenster, in dem der Nutzer weg und der Mandant noch da ist.

    Rückgabe beschreibt, was passiert ist; NICHTS wird stillschweigend entschieden:
      * Team-Mandant (weitere Mitglieder)  → ``deleted=False``, ``remaining_members>0``
      * Mandant mit Bestand (Budgets, …)   → ``deleted=False``, ``blocked_by=[…]``
      * leer und datenlos                  → ``deleted=True``
    """
    if not tenant_id:
        raise TenantCleanupError("drop_tenant_if_orphaned ohne tenant_id aufgerufen")

    members = await tenant_member_count(conn, tenant_id)
    if members > 0:
        logger.info(
            "tenant-cleanup: Mandant %s behalten — noch %d Mitglied(er) (%s)",
            tenant_id, members, reason,
        )
        return TenantCleanupResult(tenant_id, False, members)

    blocking = await tenant_blocking_tables(conn, tenant_id)
    if blocking:
        # Kein Fehler, sondern der Sollzustand für Mandanten mit Bestand: dieselbe
        # Haltung wie beim 409 auf Kaufhistorie — Daten wiegen schwerer als Ordnung.
        logger.warning(
            "tenant-cleanup: Mandant %s ist ohne Nutzer, wird aber wegen Bestand "
            "behalten (%s) — Tabellen: %s",
            tenant_id, reason, ", ".join(blocking),
        )
        return TenantCleanupResult(tenant_id, False, 0, blocking)

    try:
        result = await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    except Exception as exc:  # asyncpg.ForeignKeyViolationError u.a.
        # Hierher kommt man nur, wenn eine Tabelle über eine ANDERS benannte Spalte
        # auf tenants zeigt — dann greift die Katalog-Erkennung nicht und wir wissen
        # nicht, was wir zerstören würden. Laut abbrechen, Transaktion zurückrollen.
        raise TenantCleanupError(
            f"Mandant '{tenant_id}' liess sich nicht löschen, obwohl er nach "
            f"tenant_id-Prüfung leer war: {exc}. Vermutlich verweist eine Tabelle "
            f"über eine anders benannte Spalte auf tenants — diese Tabelle muss in "
            f"tenant_lifecycle eingeordnet werden, bevor hier weiter gelöscht wird."
        ) from exc

    if not str(result).endswith(" 1"):
        raise TenantCleanupError(
            f"Mandant '{tenant_id}' war nicht (mehr) vorhanden — erwartet 'DELETE 1', "
            f"erhalten '{result}'."
        )

    logger.info("tenant-cleanup: Mandant %s geloescht (%s)", tenant_id, reason)
    return TenantCleanupResult(tenant_id, True, 0)


async def strip_email_from_tenant_name(
    conn: Any,
    tenant_id: str,
    email: str,
    replacement: str,
) -> bool:
    """
    Ersetzt eine E-Mail-Adresse im Mandantennamen — für den Anonymisierungs-Pfad.

    Bei der Anonymisierung (Art. 17) bleibt die Nutzerzeile als Stub stehen, der
    Mandant also bewohnt und damit erhalten. Sein Name trug bis dahin weiter die
    E-Mail-Adresse der Person (``Personal tenant for max@firma.at``) — eine
    Personenbezogenheit, die die Löschung überlebt hat, und zugleich die Quelle des
    zweiten Namens-Doppels: registriert sich dieselbe Adresse neu, steht der alte
    Name noch da.

    Team-Mandanten mit sachlichem Namen (``Muster GmbH``) enthalten die Adresse nicht
    und bleiben unberührt — die Ersetzung ist bewusst an das Vorkommen der Adresse
    gebunden und nicht an ein Namensmuster.

    Rechnungen führen ihre Anschrift als eigenen Schnappschuss
    (``invoices.billing_address``); der Mandantenname ist ein Anzeigeetikett, kein
    Beleginhalt. Umbenennen verfälscht daher keine Buchhaltung.
    """
    if not email:
        raise TenantCleanupError("strip_email_from_tenant_name ohne E-Mail aufgerufen")

    result = await conn.execute(
        """
        UPDATE tenants
           SET name = replace(name, $2, $3)
         WHERE id = $1
           AND position($2 in name) > 0
        """,
        tenant_id, email, replacement,
    )
    renamed = str(result).endswith(" 1")
    if renamed:
        logger.info(
            "tenant-cleanup: Mandantenname von %s anonymisiert (E-Mail ersetzt)",
            tenant_id,
        )
    return renamed
