# AML Watchlist Screening — Runbook

Daily pipeline that screens the account book against a watchlist of known
reference accounts and writes a dated snapshot to SQL Server for Power BI.

## One-time setup

1. **Create the reporting tables.** Run `schema.sql` against the *reporting*
   database (not the Finacle replica). This creates `aml_watchlist`,
   `aml_config`, `aml_screening_results`, `aml_run_log`.

2. **Seed the watchlist.** Insert your reference accounts, e.g.
   ```sql
   INSERT INTO dbo.aml_watchlist (acid, acct_name, notes)
   VALUES ('UG6246002', 'FRONTIER TEXTILE DEALERS CO LIMITED', 'Pilot reference');
   ```
   Add more rows as investigators confirm further reference accounts.
   Set `active = 0` to retire one without deleting its history.

3. **Install dependencies** (service account's environment):
   ```
   pip install pandas numpy scikit-learn sqlalchemy pyodbc
   ```

4. **Set the credentials as environment variables** in `.env` (copied from
   `.env.example`) — never hardcode the password (the development notebook
   did; that must not ship). Each value goes on its own line; passwords are
   typed raw and SQLAlchemy escapes them, so no percent-encoding is needed:
   ```
   AML_READ_USER / AML_READ_PWD / AML_READ_HOST / AML_READ_PORT / AML_READ_DB
   AML_WRITE_USER / AML_WRITE_PWD / AML_WRITE_HOST / AML_WRITE_PORT / AML_WRITE_DB
   AML_ODBC_DRIVER  (must match `odbcinst -q -d`)
   ```
   (A full `AML_READ_URL` / `AML_WRITE_URL` is still honoured if set, and
   takes precedence.) Prefer a service account with Windows auth or a secret
   store over an inline password.

## Running

```
python aml_screening.py
```

- **First run** computes the two population-percentile thresholds, writes the
  full parameter set to `aml_config` under `model_version`, then screens.
- **Every later run** reads those frozen parameters back, so "why flagged"
  wording and trends stay stable. To recalibrate deliberately, bump
  `MODEL_VERSION` in the script (the old version's config and history are kept).
- Re-running on the same date **replaces** that date's snapshot (idempotent).
- Every run writes a row to `aml_run_log`; a failure exits non-zero and is
  logged with the error, so the scheduler can alert on it.

## Managing the watchlist (add / retire a seed from the terminal)

The watchlist lives in `dbo.aml_watchlist` in the **reporting** database. Adding
a reference account needs **no code change and no version bump** — the next
nightly run (or a manual run) picks it up automatically, screens against it, and
recomputes bands and `seed_overlap` across all seeds.

**Add a new seed.** From the server, with `.env` sourced (uses the write-DB
connection vars):

```
source .env && sqlcmd -S "$AML_WRITE_HOST,$AML_WRITE_PORT" -d "$AML_WRITE_DB" \
  -U "$AML_WRITE_USER" -P "$AML_WRITE_PWD" \
  -Q "INSERT INTO dbo.aml_watchlist (acid, acct_name, notes) VALUES ('UG0000000','ACCOUNT NAME','Confirmed mule - added by <you> <date>');"
```

- `acid` is the Finacle account ID (the same `ACID` used everywhere else).
- `acct_name` and `notes` are free text; put who added it and why in `notes` for
  the audit trail.
- `active` defaults to `1`, so the new seed is live on the next run.

**Verify it landed:**

```
source .env && sqlcmd -S "$AML_WRITE_HOST,$AML_WRITE_PORT" -d "$AML_WRITE_DB" \
  -U "$AML_WRITE_USER" -P "$AML_WRITE_PWD" -W \
  -Q "SELECT acid, acct_name, active, added_date FROM dbo.aml_watchlist ORDER BY added_date DESC;"
```

**Retire a seed** (stop screening against it, keep its history) — set `active = 0`
rather than deleting:

```
source .env && sqlcmd -S "$AML_WRITE_HOST,$AML_WRITE_PORT" -d "$AML_WRITE_DB" \
  -U "$AML_WRITE_USER" -P "$AML_WRITE_PWD" \
  -Q "UPDATE dbo.aml_watchlist SET active = 0 WHERE acid = 'UG0000000';"
```

**Apply it now** (optional — otherwise it takes effect at the next scheduled run):

```
source .env && bash run.sh
```

Notes:
- The pipeline only loads seeds where `active = 1`
  (`SELECT acid FROM aml_watchlist WHERE active = 1`).
- A new seed only matches accounts in the screened population (USD, opened since
  2024). A confirmed mule outside that scope won't produce neighbours, though it
  will still enrich discovery.
- This is the **flywheel step**: every confirmed mule added here sharpens the
  next run — new matches, and higher `seed_overlap` on accounts resembling
  several seeds.

## Scheduling

**Linux (cron)** — nightly at 02:00:
```
0 2 * * *  cd /opt/aml && /opt/aml/venv/bin/python aml_screening.py >> /var/log/aml/run.log 2>&1
```

**Windows (Task Scheduler)** — daily trigger, action:
```
Program:   C:\aml\venv\Scripts\python.exe
Arguments: aml_screening.py
Start in:  C:\aml
```
Set env vars at machine/service scope, and "Run whether user is logged on or
not".

## Power BI (recommended: Import + scheduled refresh)

Use **Import mode**, not DirectQuery. The data changes once a day, Import is
faster for the dashboard, and it lets you build trend visuals off the daily
`run_date` snapshots. Point the dataset at `aml_screening_results` (and
`aml_run_log` for a small "last run status" tile). Set the dataset's scheduled
refresh to ~30–60 minutes after the cron/Task job is expected to finish.

Suggested visuals:
- Current-day flagged accounts by `priority_band`, sliced by `reference_acid`.
- `risk_reason` shown as a table/tooltip so investigators see the rationale.
- Trend: an account's `distance` or `rank_within_ref` over `run_date`
  (an account climbing toward a reference is the signal to watch).
- New-today: accounts present in the latest `run_date` but not the prior one.

## Priority bands (rank-based, v2.1)

Bands are assigned by an account's **rank within its reference account**, not by
an absolute distance cutoff: the closest matches to a seed are High, the next
tier Medium, the rest Low. This keeps bands comparable across reference accounts
even when they sit in neighbourhoods of different density (a denser seed's
closest match may be at distance 0.44 while a looser seed's is 0.53 — under
absolute cutoffs the looser seed would unfairly get more High matches).

Defaults (in `aml_config`, tunable): High = ranks 1-3, Medium = ranks 4-10,
Low = ranks 11-30. An absolute `max_surface_distance` (0.85) still applies as a
floor, so genuinely unrelated accounts are never surfaced regardless of rank.
To change the tiers, edit the config values and bump `MODEL_VERSION`.