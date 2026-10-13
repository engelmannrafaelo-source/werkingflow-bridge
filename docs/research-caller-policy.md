# Caller-owned research policy

`POST /v1/research` and jobs with `op: research` accept the optional nonblank
string `research_instructions`. It contains the complete app-owned role,
source/provenance rules and response format. `query` remains the task and
material; `antwort_im_text` still selects final-message versus file delivery.

When supplied, both CLI and cloud use that instruction instead of the legacy
research/report templates, source ranking and Perplexity editorial guidance.
Library access, search/fetch tools, budgets, isolation, privacy checks, billing
and completion/truncation signals remain Bridge services. Tool descriptions
only document access. Instructions on the cloud route pass the same mandatory
anonymization gate as the query. Caller instructions are forwarded unchanged by
the job transport.

App-specific review and repair belong to the app: a caller-owned request never
starts the old Bridge gap classifier or its repair round, even if the legacy
review flag is on. The optional legacy scholarly enrichment is likewise not
injected. The app sends any needed material and runs its own subsequent check
or repair requests. No generic JSON parser infers app verdicts.

Omitting the field (or null) preserves the existing behavior. The previous
texts and classifier are retained in `src/compat/` solely for this migration;
this is not a claim that every old caller has already been migrated. Blank
instructions fail validation. Do not add new app rules to the compatibility
modules. The coordinator adapter preserves the existing schema and safe error
labels until the separate `src/erkunder/` migration replaces that contract.

## Privacy exceptions and remaining migration

`privacy_allowlist` is an optional array of exact caller-owned terms. With
`research_instructions`, omission means an empty list: the Bridge does not
consult its old Energy/Brandschutz list, including an environment override.
The same list applies to query, instructions and Perplexity tool queries on
both transports. It only affects the existing name/location exception
mechanism; hard identifiers such as email and phone remain protected. A nonempty
list requires the postprocessor to be active; otherwise the cloud gate fails.
Without either field the frozen legacy behavior remains available.

## Literature layer (F19)

The open-access literature layer is a Bridge service: plan search terms with
a model call, fetch OpenAlex/CORE material, rank it and format source blocks
(title, year, DOI/URL, provenance). It lives in `src/literatur.py` and carries
no app vocabulary. What to search for is the caller's: `literatur_planer` is
the planner instruction (its answer is a JSON array of search terms). It is
accepted only together with `research_instructions`. The layer runs when
`research_mode` is `academic`, `BRIDGE_SCHOLARLY_ENABLED=true` and
`BRIDGE_SCHOLARLY_PROFILE=full`. Every planned term passes the research
privacy gate (with the request's `privacy_allowlist`) before it leaves for
OpenAlex/CORE; on the cloud route the planner already sees the anonymized
query. The block carries a neutral heading only; what the material is for
belongs in `research_instructions`.

The run never fails because of the layer, but its outcome is reported:
`ResearchResponse.literatur` is `{"status": "eingefuegt", "quellen",
"suchbegriffe"}` or `{"status": "uebersprungen", "grund"}`. Possible reasons:
`schicht_aus`, `research_mode_nicht_academic`, `profil_light`,
`keine_suchbegriffe`, `anonymisierung_gescheitert`, `keine_quellen`,
`zeitbudget`, `fehler: <Typ>`. Without `literatur_planer` the field is absent
and no literature is fetched.

The legacy Energy planner prompt and context header are frozen in
`compat/scholarly.py` on top of the same mechanism; `scholarly.py` is only its
import adapter. New research requests never invoke it.
F20 is frozen in `compat/fach_allowlist.json`. Apps own the decision which
exact exceptions to send; no app-specific defaults are added to new requests.
These are transitional copies for unmigrated clients, not a completed app rollout.
Delete them and their import adapters after research callers and privacy callers
have migrated. Delete the coordinator adapter after the Erkunder migration.
Shared parsers and turn limits live in `research_protocol.py`, search/fetch
limits in `research_budgets.py`; neither needs compat for caller-owned prompts.

CXA merge requirement: if `agentischer_lauf` is merged, its request validator
must reject `research_instructions` and `privacy_allowlist` unless it actually
implements them. Silently ignoring either field is not a supported contract.
