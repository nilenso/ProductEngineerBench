# Modelling

Describes how to model entities over time.

## Goals

Model an entity with a set of known fields which changes over time such that:

1. The time when a change occurred is tracked separately from when the change
   was recorded

2. The state of an entity (a view) can be queried for any point in time to
   fetch the values of fields in that entity at that point in time

3. A change that occurred in the past should affect all views of the entity
   that are requested after that point in time

4. The end user should not have to declare changes but instead just edits the
   entity as a whole at a given point in time

## CRDTs

Ref: https://en.wikipedia.org/wiki/Conflict-free_replicated_data_type

Typically used for resolving conflicts between information created from two different sources, we are able to use CRDTs for our usecase by treating a change made to an entity's history as a change from a disconnected user that's converging with other changes.

### LWW Element Set

Ref: https://en.wikipedia.org/wiki/Conflict-free_replicated_data_type#LWW-Element-Set_(Last-Write-Wins-Element-Set)

An LWW Element Set is a well known CRDT. It consists of an 'add set', 'remove
set' and a time stamp for each element in it.

The timestamp will be the time at which the change occurred. The value is a map
with all the fields and corresponding values that are updated. In our case, the
remove set does not exist since nothing is meant to be removed, just edited. We
may mimic a remove by updating a field with a null value if necessary.

## Schema

The schema to store this in SQLite.

```sql

create table changes(
  -- This would be the personId in our case
  entityId text,

  -- Time when the event occurred
  timestamp datetime,

  -- json with fields that have changed and the corresponding values
  -- eg. {'name': 'Name1', 'age': 22}, {'age': 29}
  value text,
);

```

Here’s a concise tech spec for “Grand Central.” It captures current stack choices and the target architecture, flags risks, and proposes guardrails.

# 1) Overview

* Purpose: Single source of truth for employee data. Visual analysis now. Workflow automation later.
* Users: Internal staff. Auth via Google.
* Non-goals: Public API, multi-tenant SaaS, heavy analytics.

# 2) Core Requirements

* CRUD for people and changesets.
* Authenticated access only.
* Visual graphs of trends.
* Low-ops deploy. Small team ownership.
* SQLite durability with simple backups. Path to Postgres when needed.

# 3) Tech Stack (current)

* Web framework: Remix (React + SSR). TypeScript everywhere.
* Styling: Tailwind.
* ORM: Prisma.
* DB: SQLite (file URL defaults to `file:/data/sqlite.db`).
* Auth: `remix-auth` + Google OAuth.
* Charts: D3 components.
* Testing: Vitest for unit, Cypress for E2E.
* Packaging: Docker multi-stage build. Separate Datasette image for read/admin.
* Runtime: Node process started by `start.sh` which runs `prisma migrate deploy` then `remix-serve`.
* Ops: Procfile targets (`web`, `datasette`). Fly volume mounted at `/data`. Healthcheck at `/healthcheck`.
  Sources: repo Dockerfiles, README, Fly config.&#x20;

# 4) Architecture

## 4.1 Logical components

* **Remix app**

  * Server routes: SSR HTML, actions for mutations, loaders for reads.
  * Session: Cookie session storage.
  * Auth: Google Strategy. On first login auto-creates user.
  * Data access: Prisma client.
  * Graphs: D3 line and multiseries components rendered client-side.
* **SQLite**

  * Deployed at `/data/sqlite.db` with a persistent volume.
* **Datasette**

  * Read and exploratory UI at `/datasette/`.
  * Insert bot capability gated by token for controlled writes.
* **Reverse proxy**

  * Local dev uses Caddy via `docker-compose.local.yml` to route to Node and Datasette.

## 4.2 Runtime topology

* **Fly.io app**

  * Service ports: 80/443 -> internal 8080.
  * Volume mount `/data` for DB durability.
  * Concurrency limits tuned to \~20–25 connections.
* **Datasette sidecar**

  * Runs as a separate process/container when enabled.
  * Base URL set to `/datasette/`.

## 4.3 Request flow

1. Browser → Remix route.
2. Loader checks session. If needed, Google OAuth redirects.
3. Loader/Action hits Prisma → SQLite.
4. Response rendered via SSR + client hydration.
5. Optional: Datasette used for admin/read operations.

# 5) Configuration and Secrets

* Required env:

  * `SESSION_SECRET`
  * `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
  * `APP_URL` (for OAuth callback)
  * `DATABASE_URL` (defaults to `file:/data/sqlite.db`)
  * `PORT` (default 8080)
  * `INSERT_TOKEN` for Datasette insert bot
* Secret handling: Store in Fly secrets or CI secrets. Never in repo. Rotate quarterly.

# 6) Build and Deploy

* **Build**: Multi-stage Docker builds:

  * `deps` installs dev deps, `production-deps` prunes, `build` runs `prisma generate` and `npm run build`, final image copies `/build`, `/public`, `.prisma`.
* **Start**: `start.sh` applies migrations then serves.
* **Datasette image**: Minimal Python Alpine, installs `datasette`, `datasette-insert`, `datasette-auth-tokens`, serves `/data/sqlite.db`.
* **CI/CD**: GitHub Actions push images to registry and deploy. Branching model: `dev` → staging, `main` → production.
  Note: README mentions DigitalOcean registry and droplet; repo also contains `fly.toml`. Standardize on one target (recommend Fly for volume + healthchecks).

# 7) Data Model and Migrations

* ORM-driven schema via Prisma.
* Migration policy:

  * All schema changes via Prisma migrations committed to VCS.
  * On boot, `prisma migrate deploy` runs. Fail fast if migration cannot be applied.
* Backups:

  * Nightly snapshot of `/data/sqlite.db` to object storage.
  * Pre-migration snapshot in CI for production.

# 8) Observability

* Logs: stdout structured JSON from app. Retain 14–30 days in platform logs.
* Health: `/healthcheck` HTTP 200.
* Metrics (proposed):

  * Basic RED metrics via runtime counters.
  * Optionally expose `/metrics` for scrape.

# 9) Security

* HTTPS enforced.
* Cookie `__session`: httpOnly, sameSite=lax, secure in production.
* OAuth scopes: minimal email identity.
* RBAC (future): roles for viewer, editor, admin.
* Datasette:

  * Base URL scoped under `/datasette/`.
  * Token-gated inserts via `datasette-auth-tokens`.
  * Default deny policy except for authenticated actor.

# 10) Performance and Scale

* SQLite fits current workload. Single writer, low read concurrency.
* Concurrency guard at app level to avoid lock thrash.
* Thresholds to move to Postgres:

  * > 10 req/sec sustained writes.
  * Multi-region rollout.
  * Complex reporting joins.
* Migration path:

  * Replace `DATABASE_URL` to Postgres.
  * Run Prisma `migrate deploy` on Postgres.
  * Cutover via maintenance window.

# 11) Testing Strategy

* Unit: Vitest, `happy-dom` environment.
* E2E: Cypress with `login()` helper and `cleanupUser()` per test file.
* CI gates: typecheck, lint, unit, E2E against mocked services.

# 12) Risks and Mitigations

* **SQLite file lock contention** → keep mutations small; queue bulk imports; consider Postgres if contention spikes.
* **Dual deployment targets confusion** → choose Fly or DO. Remove unused manifests.
* **Secrets leakage** → enforce CI secret scanning; restrict Procfile usage in prod if not needed.

# 13) Open Questions

* Is Datasette exposed in production or only behind VPN? If exposed, add auth proxy.
* Which registry is canonical: DOCR or Fly’s? Align CI/CD.
* Backup RTO/RPO targets?

# 14) Acceptance Criteria

* Auth works with Google in staging and prod.
* App boots with `prisma migrate deploy` and serves on 8080 behind TLS.
* `/datasette/` loads with token auth.
* E2E tests pass in CI on each PR to `dev` and `main`.

Sources: project Dockerfiles, Procfile, README, Fly config, package.json, Prisma bootstrap scripts.&#x20;

