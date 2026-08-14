# SIR source-URL tables

Per-state/UT spreadsheets mapping every `(district, AC no, AC name, part
no)` to the original source PDF/ZIP link for that part — mostly the
Election Commission's centralized SIR (Special Intensive Revision) portal
(`eci.gov.in/sir/f1`..`f4`), plus a handful of state CEO portals that
publish their own rolls directly (Karnataka's CSV, Gujarat's ZIP-of-PDFs,
West Bengal's `RollPDF/GetDraft` endpoint, Chandigarh, Daman & Diu).
Received as `sir_excel.zip`; committed here as-is (one `.xlsx` per
state/UT, `State`/`District`/`AC No`/`AC Name`/`Part No`/`PDF Link`
columns — Karnataka excluded, since its own CSV source link needs no
per-part table).

`state_roll_years.json` / `.xlsx` gives each state/UT's roll year, source
host, and format at a glance.

**Coverage: every state/UT except Jammu & Kashmir** (`U08` in
`state_roll_years.json` — its ECI portal entry is `"ceo.jk.gov.in (Coming
Soon)"`, `roll_year: null`).

Not yet wired into any connector or the search app — this is the raw
reference data. See `voter_search_engine`'s
`docs/TASK_source_link_per_result.md` for the consuming task (per-result
"view source" links), which uses this table in place of mirroring PDFs
into a separate GCS bucket.
