# Grand Central — MVP PRD

**DRI:**&#x20;
**Stakeholders:** People Ops, Partners, Engineering, Design, Data
**Status:** Draft v1
**Date:**

---

## 1) Problem statement

Partners and Execs cannot quickly answer basic workforce questions because employee data is scattered across spreadsheets, lacks a single source of truth, and has incomplete history of state changes. This creates slow decisions, duplicated work, and error-prone analysis.

**Evidence to collect pre-ship**

* Time spent per week answering roster questions.
* Number of duplicate or conflicting records in current Sheets.
* Top 5 recurring questions stakeholders cannot answer reliably today.

---

## 2) Who is the customer

* **Primary:** People Ops admin who maintains records and exports data.
* **Secondary:** Partners/Managers who need roster and trend views.
* **Tertiary:** Employees who look up colleague basics.

**Jobs-to-be-done**

* Maintain canonical person records with auditable history.
* Import legacy data once with minimal cleanup.
* Run simple trend analyses without a data analyst.

---

## 3) Goals and non-goals

**Goals (MVP)**

1. Create, edit, and view **person** records with append-only **changesets** and effective dates.
2. **One-time bulk import** from Google Sheets with dedupe by work email.
3. **Directory** with search and CSV export of the **current snapshot**.
4. **Analytics v0:** monthly headcount, joiners vs leavers, level distribution over time; simple “level vs prior experience” view.
5. **Security:** Google SSO, domain allowlist, audit log on writes.

**Non-goals (MVP)**

* Payroll, performance reviews, external HRIS sync.
* Complex RBAC beyond Admin vs Viewer.
* Compensation reporting UI. *Schema ready; UI deferred to v2.*

**Why now**

* Operational risk from spreadsheet fragmentation.
* Quick wins unlock broader analytics later.

---

## 5) Requirements

### 5.1 Functional

* **Auth:** Google SSO; domain allowlist; logout; session security.
* **Directory:** list with columns *Name, Work email, Level, Status, Start date*; search by name/email; link to person detail; CSV export of current snapshot.
* **Person detail:** core fields, plus **History** tab showing changesets in reverse chronological order; show who changed what and when.
* **Create/Edit:** forms capture effective dates; all edits append a changeset; current snapshot recomputed.
* **Analytics v0:**

  * *Headcount trend* by month (active count, joiners, leavers).
  * *Level mix* over time (banded junior/mid/senior).
  * *Level vs prior experience* scatter.
* **Import:** one-time CSV importer for legacy Sheets; idempotent; validation report; dedupe by work email; mapping guide.

### 5.2 Data model (MVP)

* **Person**: id, firstName, lastName, workEmail (unique), phone?, role, level, status, startDate, endDate?, priorWorkExperienceYears.
* **Changeset**: id, personId, field(s) changed, newValue, effectiveDate, author, createdAt.
* **Status enum**: FULLTIME, CONTRACTOR, EXEC, PARTNER, RESIGNED.
* **Compensation (v2-ready)**: CompChange(personId, amount, currency, effectiveDate, notes).
* **Snapshot rule**: latest effective changes per field as of “now”.

### 5.4 UX principles

* Defaults fast data entry over perfect taxonomy.
* Make history obvious before saving edits.
* Show what changed, by whom, and when.

---

## 8) Risks and mitigations

* **Import correctness** → schema mapping guide, dry-run, row-level report.
* **Duplicate records** → unique email constraint; surface potential duplicates; merge flow later.
* **Bad effective dates** → inline validation; preview of resulting history.
* **OAuth misconfig** → automated env checks in CI; clear runbooks.

---

## 9) Acceptance tests (MVP)

1. **Create person**: Authenticated user submits required fields → person appears in directory; audit entry created; event `person_created` emitted.
2. **Edit with history**: Update level with effective date → new changeset stored; History tab shows entry; snapshot updated.
3. **Import**: Run importer on validated CSV → ≥95% rows ingested; reconciliation report shows any rejects with reasons.
4. **Export**: Click Export on directory → CSV downloads with one row per current person; header spec matches appendix.
5. **Analytics**: Open Analytics → monthly headcount, joiners vs leavers, and level mix charts render from production data; “level vs experience” view loads.
6. **Security**: Unauthenticated user → redirected to login; export requires Admin.

---

## 10) Open questions

* Exact mapping of legacy Sheets to entities and enums.
* Admin vs Viewer permissions beyond export.
* Compensation governance and who can view amounts in v2.
* Do managers need edit rights or view-only in v1?

---

## 11) Appendix

**A. CSV header spec (current snapshot)**
`firstName,lastName,workEmail,phone,role,level,status,startDate,endDate,priorWorkExperienceYears`

**B. Glossary**

* **Changeset**: append-only record of a field change with an effective date.
* **Snapshot**: latest effective value per field at a point in time.
* **Headcount**: number of active employees in a period.
* **Joiners/Leavers**: counts of start/end effective events in a period.

**C. Decision log**

* Compensation UI deferred to v2; schema included now.
* Unique workEmail enforced; no merge UI in v1.
* SQLite acceptable for MVP, to be revisited post-M6.

