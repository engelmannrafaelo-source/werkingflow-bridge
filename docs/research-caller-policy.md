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
