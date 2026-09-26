# Mouse Colony Management GUI

A simple mouse colony tracker with a shared lab-access question. It listens on loopback by default at
`http://127.0.0.1:8765` and can also sit behind a trusted local reverse proxy.

An optional cage-card CSV supplies cage identity, status, counts, room, and
census dates. An optional Excel workbook can add DOB, sex, genotype/strain,
legacy mouse labels, notes, and surgery history. Source files remain read-only
after import.

## What it tracks

- Active, inactive, and on-order cages
- One automatically generated ID per mouse, such as `A-1839`
- Background lineage tracking for mice created together
- DOB and automatically calculated age
- Sex, genotype, assigned mouse user, free-form notes, and optional legacy mouse labels
- Cage tags and tag filtering
- Cage filtering by mouse user, room, and status, with configurable list sorting
- Stock/unused, single-mouse, and breeding-pair cage views
- Split/move workflows and breeding-pair-only litter weaning
- Configurable room aliases for Regular Cycle, Reverse Cycle, and Breeding Core
- Simple surgery records: date, time, operator, and type (maximum four per mouse)

There are no individual accounts. Everyone who passes the shared login sees and
edits the same database, and every application route is gated by that session.
Successful logins remain valid for 30 days, including across application restarts.

An optional read-only integration API can resolve one exact mouse identifier for
Brain3D/camera3d. It uses a separate bearer token, never accepts the browser login
cookie, and remains behind the same loopback/SSH boundary. It does not mutate colony data.

`Stock mice` means active mice still housed in a non-breeding cage with more
than one active mouse. Breeding pairs can be assigned manually or derived from
the installation's configured breeding-room rule.

## Start

Double-click `run.command`, or run:

```bash
./run.command
```

The first start creates `data/mouseline.db`. If seed files are configured, it
imports them when that database is empty. The application process always binds
to `127.0.0.1`; network access should go through a reverse proxy.

## Review an AOPS cage-card export

Open **Update from CSV** and upload the NU Personal Page / AOPS Cage Cards CSV to compare it with
the current local records. Analysis only stages a review: it does not mutate the
colony. The entire CSV must pass strict header, row, status, count, date, and
duplicate-ID validation before any differences are shown; one malformed record
rejects the whole upload.

Choose **Preview updates**, review the proposed changes, then choose
**Apply all updates** or apply individual changes. The most recent review stays
available when you return to the page, including its saved application results.

The review can propose missing cages, append-only mouse records, an
`on_order` cage becoming active, and soft deactivation when AOPS marks a cage
inactive. Apply proposals individually or in bulk, or choose **Keep local** for
changes that should not be imported. Applying a proposal never overwrites
existing mouse metadata, mouse history, or locally maintained cage metadata.
Fewer rows in an export never delete local cages or mice, and a local record's
absence from AOPS is not treated as a deletion instruction.

The raw CSV and PI fields are not stored. Mouseline retains only the normalized
differences and audit metadata needed to review and record approved changes.

## Configuration

Copy `.env.example` to `.env` when changing the port, database location, seed
files, trusted proxy hosts, or URL prefix. Set the shared answer only in the
private `.env`; the tracked example intentionally contains a non-working placeholder.

For a reverse proxy mounted at `/colony`, set:

```dotenv
MOUSELINE_ROOT_PATH=/colony
MOUSELINE_ALLOWED_HOSTS=127.0.0.1,localhost,colony.example.test
MOUSELINE_LOGIN_ANSWER=replace-with-private-answer
```

The proxy should strip `/colony` before forwarding to the loopback application.
The shared login is not a substitute for network access control, so expose the
application only on a trusted network.

## Brain3D/camera3d subject lookup

Set a distinct random token (at least 32 characters) in the private `.env`:

```dotenv
MOUSELINE_INTEGRATION_TOKEN=<random-secret>
```

Keep the service bound to loopback. From a workstation, forward it over SSH (the
deployed instance currently uses port `3004`):

```bash
ssh -N -L 13004:127.0.0.1:3004 hhw9l84
```

Then resolve an exact generated or legacy mouse ID with
`GET http://127.0.0.1:13004/api/v1/animals/resolve?identifier=...` and an
`Authorization: Bearer ...` header. The response contains a canonical public ID,
minimal colony metadata, and a SHA-256 receipt suitable for storing with a plan.

## Data safety

- `data/*` is excluded from Git.
- Cage counts are calculated from active mouse records.
- The source files stay read-only; the database is seeded only when it is empty.
- Normal use marks records inactive rather than deleting them.
- Back up `data/mouseline.db` before upgrades or migration.

## Variables

Open **Variables** to manage genotype, mouse user, surgery type, surgery operator,
and room choices. Existing values populate the initial catalog. The mouse, cage,
and surgery forms use these shared dropdowns.

Renaming an option also updates its existing records, including inactive mice.
Deleting an option removes it from new choices while preserving saved values.
Room aliases and breeding-room
classification are retained when a room is renamed.

## Cage card photos

Use **Upload cage card** at the top of **Cages** to read a JPEG, PNG, or HEIC photo
and open its matching cage. The cage page also accepts photos and fills mouse
edit drafts. Review the detected cage, choose each target mouse, then use
**Apply to mice** and **Save all mice**. Missing fields leave existing values alone.
Photo genotype text is shown separately from the configured genotype choices.

The server requires the `photo` extra and the OCR model files in
`MOUSELINE_OCR_MODELS` (default `/opt/senzailab/backend/runtime/colony-ocr-models`).
Recognition uses CPU inference and does not send photos to an external service.

Set `MOUSELINE_PHOTO_DIRECTORY` to the permanent archive directory (default
`data/photos`). Production uses `/mnt/senzailab/Shared/Apps/Colony/IMGS`.
Each valid upload preserves its original bytes and an oriented JPEG preview,
even when OCR fails. Photos are grouped by cage database ID; uploads without a
matching or explicitly selected cage stay in `unassigned`. Re-uploading the same
photo to the same cage does not add a duplicate. The archive has no automatic cleanup.
Cage pages show a collapsed **Show pictures** section only when photos exist;
previews and original downloads require the same login as the rest of the app.
Each photo keeps its OCR results, including mouse IDs, sex, date of birth and
genotype, beside the preview. These results describe the photo and remain
separate from subsequent changes to mouse records.
