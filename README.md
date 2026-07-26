# Mouse Colony Management GUI

A simple, no-login mouse colony tracker. It listens on loopback by default at
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
- Sex, genotype, free-form notes, and optional legacy mouse labels
- Cage tags and tag filtering
- Stock/unused, single-mouse, and breeding-pair cage views
- Split/move workflows and breeding-pair-only litter weaning
- Configurable room aliases for Regular Cycle, Reverse Cycle, and Breeding Core
- Simple surgery records: date, time, operator, and type (maximum four per mouse)

There are no accounts or logins. Everyone using this local page sees and edits
the same database.

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

## Configuration

Copy `.env.example` to `.env` when changing the port, database location, seed
files, trusted proxy hosts, or URL prefix.

For a reverse proxy mounted at `/colony`, set:

```dotenv
MOUSELINE_ROOT_PATH=/colony
MOUSELINE_ALLOWED_HOSTS=127.0.0.1,localhost,colony.example.test
```

The proxy should strip `/colony` before forwarding to the loopback application.
Because there are no user accounts, expose it only on a trusted network.

## Data safety

- `data/*` is excluded from Git.
- Cage counts are calculated from active mouse records.
- The source files stay read-only; the database is seeded only when it is empty.
- Normal use marks records inactive rather than deleting them.
- Back up `data/mouseline.db` before upgrades or migration.
