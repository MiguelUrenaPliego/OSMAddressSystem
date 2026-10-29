# Cloud handoff: preliminary requirements, 2026-10-27

Scope: hosting the current Fotouni application, not a public production
deployment or a purchased hosting plan. The pilot date, editor count, public
viewer count, uptime target and number of communities remain team decisions.

## Architecture the host must support

- Python/FastAPI/Uvicorn with GeoPandas/Shapely, graph libraries, SQLAlchemy
  and SciPy. Local testing uses Python 3.13.7 on Windows; the existing Docker
  image targets Python 3.12 on Linux. Linux build/runtime has not been tested
  in this work package.
- One application process/worker per project. Computed networks and addresses
  live in memory, so multiple workers or replicas are not interchangeable.
  There is one background computation at a time per process. PostgreSQL does
  not solve the process-local cache constraint.
- Persistent project input/network files and a SQLite database in WAL mode;
  optional PostgreSQL through ADDRESS_DB_URL. Separate data roots, database
  ownership, processes and proxy routes for separate communities.
- HTTPS reverse proxy and individual editor accounts. The local application now
  defaults to invited accounts with server-enforced roles, verified author IDs,
  revocable sessions and retained audit evidence. Bootstrap the first admin on
  the host before exposure; configure trusted proxy headers and the root path.
  See `accounts-and-history.md`. `/simple` also requires project membership;
  public approved address lookup remains separate work.
- Stable base-path routing (MIT currently uses a path prefix), 60 MB request
  limit, health checks, process restart policy and captured application logs.
- Outbound basemap tile access; Leaflet is vendored but basemap tiles are not
  offline. Tile licensing/usage and festival connectivity need separate review.

## Measurements on an isolated practice copy

These are local Windows observations under concurrent test load, not a load
test, Linux benchmark, percentile or guaranteed hosting capacity. Recorded in
`cache/workpackage-4m6zx56t/measurements.json`; reproduction scripts are local
scratch artifacts. No source project was recomputed for these measurements.

| Measurement | Observed |
|---|---|
| Network computation, initial practice/bundled copy | 10.68 / 9.87 seconds, 216 computed streets each |
| Network computation during the measurement run | 36.695 seconds |
| Address computation after a fresh network | 19.257 seconds |
| Peak process working set | 222.86 MiB |
| Process private commit after addressing | 621.94 MiB |
| Input files / network files / computed CSVs | 3,607,365 / 1,557,574 / 10,418 bytes |
| Survey buildings | 2,127 |
| Addressed objects including reference points | 2,189 total; 1,248 named, 586 pending name, 355 no street |
| Survey buildings with a numeric azimuth | 1,539 of 2,127 (~72.4%); numeric presence is not accuracy validation |

The todo reports 1–4 minute pilot computations on the existing server. Different
hardware, state, naming plans, load and versions can explain differences; the
current measurements do not disprove that report. Concurrent read/editor
latency, upload peaks, maximum datasets, memory leaks and long-run database
growth have not been measured.

## Provisional capacity envelope

The todo's proposed 2 vCPU / 4 GB RAM / 20 GB persistent disk is a reasonable
**staging configuration to validate**, not an approved production specification.
Leave memory for the OS, proxy, GIS native allocations, solver and backups;
private commit and working-set measurements describe different things. Dataset
storage is small today; dependencies, images, retained backups and future
communities dominate the disk allowance. No GPU is used.

Before selecting the final plan, measure three representative recomputations
and end-to-end editor response times with the agreed simultaneous editor/viewer
count. Include long edits/imports, a restart, the largest expected next community
and at least one recovery drill. Repeat on the intended Linux runtime without
changing dependencies silently.

## Recovery and operations

- Back up SQLite through its backup API (`python -m osmaddresssystem.app backup`)
  rather than copying only `project.db` while a WAL-backed server is running.
  PostgreSQL uses pg_dump. Back up input/network files and deployment
  configuration too; database-only recovery is incomplete.
- Keep versioned, encrypted, off-host copies, an explicit retention schedule
  and restore instructions. Agree backup interval, acceptable data loss (RPO)
  and acceptable recovery time (RTO); daily is a proposal, not an established need.
- Restore into a new project root/database, validate the revision, named
  checkpoints, road decisions, computed result and exports, then switch routing.
  Never overwrite the only source project to test recovery.
- Existing Docker/Caddy/systemd examples are starting points. Docker installs
  from pyproject rather than the lockfile; resolve reproducible builds before
  production. The older-schema refusal is not a migration strategy.
- Confirm service user/file permissions, TLS renewal, health monitoring,
  logs/rotation, disk alerts, timeouts, database readiness and rollback steps.

## Still required before production approval

Concurrency/load measurements; Linux build test; validated restore; authenticated
authors and permission model; per-project isolation; agreed pilot tasks and
acceptance criteria; structured-name/sign migrations; baseline reproducibility.
No cloud resources, accounts or deployment have been created.
