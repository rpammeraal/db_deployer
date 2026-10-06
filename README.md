# db_deployer

Deploy database objects (roles, databases, schemas, tables, functions, procedures, views, indexes, data, privileges) to PostgreSQL from a directory of SQL files. Tracks changes between deployments via a local cache so unchanged objects are skipped, and can check a database against the repository and fix what drifted.

Every file in the repository is plain SQL plus comments: anything db_deployer deploys can also be run through `psql`.

## Installation

```
pip install db-deployer
```

Requires Python 3.10+ and a target PostgreSQL server (any recent version). No client library needs to be installed separately.

## Configuration

`db_deployer` reads all configuration from environment variables and `~/.pgpass`. No config file.

### Environment variables

| Variable | Purpose |
|---|---|
| `DB_DEPLOYER_REPO` | Path to the SQL repo containing `database/<name>/...` (required, or use `--repo`) |
| `DB_DEPLOYER_ENV` | Deployment environment, selects `data/<env>/` (or use `--env`) |
| `PGHOST` | PostgreSQL host (standard libpq env) |
| `PGPORT` | PostgreSQL port (default 5432) |
| `PGUSER` | Connecting user |
| `PGDATABASE` | Optional; if unset, iterate all directories under `<repo>/database/` |

The same `PG*` vars are read by `psql` and every other libpq-based tool, so you can share them across your workflow.

### Passwords via `~/.pgpass`

Passwords are never accepted on the command line or in environment variables. libpq consults `~/.pgpass` automatically. Format:

```
hostname:port:database:username:password
```

Example:

```
globe.local:5432:rock_dev:roy:xxxxxxxx
globe.local:5432:rock_dev:analytics_owner:yyyyyyyy
globe.local:5432:rock_dev:reports_owner:zzzzzzzz
```

The file must be mode 0600 or libpq will refuse to read it:

```
chmod 600 ~/.pgpass
```

Multiple entries let you switch identities for privilege application without embedding secrets in the tool.

### Encryption primitive

SQL files can use `___ENCRYPT_FIELD___(<column>,<SECTION>,<KEY>)` to expand into a `pgp_sym_encrypt(...)` expression at deploy time. The secret is read from an environment variable named `<SECTION>_<KEY>` (uppercased):

```sql
SELECT ___ENCRYPT_FIELD___(password_col,ENCRYPTION,MASTER_KEY) FROM users;
```

Expects `$ENCRYPTION_MASTER_KEY` in the environment.

## Repository layout

The SQL repo must follow this structure:

```
<DB_DEPLOYER_REPO>/
└── database/
    ├── postgres/                     ← roles and databases are created from here
    │   ├── role/
    │   └── database/
    ├── rock_dev/                     ← directory name = postgres database name
    │   ├── schema/
    │   │   └── analytics.sql
    │   ├── table/
    │   │   ├── users.sql
    │   │   └── orders.sql
    │   ├── function/
    │   ├── procedure/
    │   ├── view/
    │   ├── data/
    │   │   ├── dev/                  ← only with --env dev
    │   │   └── prod/                 ← only with --env prod
    │   ├── index/
    │   ├── privilege/
    │   └── post_deployment/
    └── another_db/
        └── ...
```

Object types are processed in this order: `role`, `database`, `schema`, `table`, `function`, `procedure`, `view`, `data`, `index`, `privilege`, `post_deployment`. The `postgres` database is always processed first, as that is where `CREATE ROLE` and `CREATE DATABASE` for the application databases live.

A file's contents must match its directory: a file in `table/` that does not define a table aborts the run. Functions and procedures may share a directory.

### Object directories

Each file in `schema/`, `table/`, `function/`, `procedure/`, `view/` and `index/` defines one object, and db_deployer works out what to do from the database's current state:

- A table that does not exist is created from the file. A table that exists is compared with the file, and columns are added or dropped to match. A `NOT NULL` column with a `DEFAULT` is added as nullable, existing rows are filled with the default, and the column is then made `NOT NULL`. A `NOT NULL` column without a default is added as-is, with a warning. Indexes defined in the table file are added, recreated or dropped to match.
- Functions, procedures and views are dropped and recreated. Whatever depends on them (views, materialized views, policies, triggers) is dropped first and recreated afterwards — but only if it is defined in the repository. Anything in the database that the repository does not know about aborts the run, which is the point: the repository is the source of truth.
- The same holds for dropping a column: its dependents are dropped and recreated from the repository, or the run aborts.

### Statement directories

Files in `data/`, `privilege/` and `post_deployment/` are not parsed for an object: anything goes, and the statements are run as they are. `data/` runs after views, `privilege/` after that, and `post_deployment/` last of all, in its own transaction. `CREATE POLICY` and `CREATE TRIGGER` statements in these files are preceded by the matching `DROP ... IF EXISTS`, so the files are re-runnable.

`data/` may contain one subdirectory per environment. `data/dev/` is deployed only with `--env dev` (or `DB_DEPLOYER_ENV=dev`); files directly in `data/` are deployed always. Without an environment set, db_deployer warns and deploys the common files only.

### Ordering within an object type

Within a single object type, db_deployer processes files alphabetically by default. Two optional per-directory files let you override or supplement that:

**`manifest.txt`** — pin an explicit order for named files.

Place a `manifest.txt` alongside SQL files in any object-type directory. List filenames one per line, in the order they should be processed. Files listed in the manifest are processed first in the given order; files not listed follow in alphabetical order.

```
# database/rock_dev/table/manifest.txt
users.sql
sessions.sql
orders.sql
```

**`dependency.txt`** — declare parent/child relationships and let db_deployer topologically sort.

Format is one dependency per line, `<child>:<parent>`. A file listed as `child` will not be processed until every `parent` it depends on has been processed. Multiple parents per child are declared on separate lines:

```
# database/offsite/table/dependency.txt
core.file.sql:core.peer.sql
core.remote_peer.sql:core.peer.sql
core.remote_file.sql:core.file.sql
core.remote_file.sql:core.remote_peer.sql
```

Dependencies are resolved before deploy. If a parent file has changed and is redeployed, all its transitive children are also marked as changed and redeployed, even if their own contents are unchanged — this keeps foreign keys and view definitions consistent when their referents change.

Use `manifest.txt` for stable, explicit ordering (e.g. schemas that must exist before tables). Use `dependency.txt` for expressing real referential relationships that should trigger cascaded redeploys.

## Usage

```
export DB_DEPLOYER_REPO=/path/to/sql/repo
export DB_DEPLOYER_ENV=dev
export PGHOST=globe.local
export PGUSER=roy
# ~/.pgpass provides passwords

db_deployer --db rock_dev --run              # deploy changed files to rock_dev
db_deployer --run                            # deploy to every db found under database/
db_deployer --run --dry-run                  # show the change script, execute nothing
db_deployer --db rock_dev --dev --run        # only files that have changed since last cache commit
db_deployer --db rock_dev --rebuild_cache    # mark everything as up-to-date without deploying
db_deployer --db rock_dev --verbose --run    # show SQL output during deployment
```

Without `--run`, the tool exits without doing anything — a safety measure to prevent accidental deploys.

The change script of every deployment is written to `/tmp/deploy.<database>.<timestamp>.sql`.

### Checking a database against the repository

```
db_deployer --check                          # report differences, exit code 1 if any
db_deployer --fix --dry-run                  # show the script that would resolve them
db_deployer --fix                            # resolve them, in one transaction
```

`--check` compares every table (columns, types, nullability, defaults, indexes, constraints), the `index/` directory, every view and materialized view, every function and procedure, and the policies and triggers defined in `data/` and `post_deployment/` with the database. Each object is created in the `tmp` schema and compared through the catalog, so quoting, casing and whitespace differences do not register. Other statements in `data/` and `post_deployment/` are not checked, since they can be anything.

Objects in the database that have no file in the repository at all are not reported; strays within a known object (a column, index, constraint, policy or trigger) are, and `--fix` drops them.

### Options

| Option | Description |
|---|---|
| `--repo PATH` | Override `$DB_DEPLOYER_REPO` |
| `--env NAME` | Deployment environment, selects `data/NAME/`; overrides `$DB_DEPLOYER_ENV` |
| `--db LIST` | Comma-separated list of database names to process |
| `--run` | Actually execute the deployment (required — otherwise dry) |
| `--check` | Compare the database with the repository and report the differences |
| `--fix` | Resolve the differences found by `--check` (implies `--check`) |
| `--dry-run` | With `--run` or `--fix`: print what would be executed, execute nothing |
| `--dev` | Only deploy files that have changed since the last cache commit |
| `--rebuild_cache` | Mark all files as up-to-date without deploying anything |
| `--verbose` | Show SQL output during deployment |
| `-h`, `--help` | Show help |

## Development

Editable install into a venv, so source edits are picked up without reinstalling:

```
make dev
source .venv/bin/activate
db_deployer --help
```

Or invoke without activating the venv:

```
.venv/bin/db_deployer --help
.venv/bin/python -m db_deployer --help
```

### Self-contained executable

As an alternative to `pip install`, db_deployer can be packaged as a self-contained `.pyz` executable via [shiv](https://github.com/linkedin/shiv) — drop it on `$PATH` and it runs without a venv or a system-wide install:

```
make               # builds ./db_deployer.pyz
sudo make install  # copies to /usr/local/bin/db_deployer
```

Override the install prefix if you prefer somewhere else:

```
make install PREFIX=~/.local
```

Use a specific Python interpreter:

```
make PYTHON=python3.12
```

The `.pyz` is pinned to the Python interpreter the build venv was created from, and bundles compiled extensions for that interpreter. Rebuild it (`make distclean install`) after that Python is upgraded. For a fleet with mixed macOS and Linux, build once per platform on a representative machine.

| Target | What it does |
|---|---|
| `make` / `make build` | Build `db_deployer.pyz` |
| `make install` | Build and copy to `$PREFIX/bin/db_deployer` |
| `make uninstall` | Remove the installed binary |
| `make dev` | Editable install for live-edit development |
| `make check-deps` | Verify psycopg2 can be installed on this platform |
| `make clean` | Remove build artifacts (keeps the build venv) |
| `make distclean` | Also remove the build venv |

### Layout

```
db_deployer/
├── LICENSE
├── Makefile
├── pyproject.toml
├── README.md
└── src/
    └── db_deployer/
        ├── __init__.py
        ├── __main__.py       # entry point for `python -m db_deployer`
        ├── cli.py            # main() and top-level deployment logic
        └── lib/
            ├── __init__.py
            ├── cache.py          # tracks file hashes/timestamps between runs
            ├── constants.py      # env var names, filenames
            ├── db.py             # PostgreSQL connection wrapper and catalog queries
            ├── sqlfile.py        # parses individual .sql files
            ├── sqlpreprocessor.py # expands ___PRIMITIVES___
            ├── tablefield.py     # column definition helper
            └── util.py           # logging, misc helpers
```

## License

Copyright (C) 2026 Roy P. Ammeraal

db_deployer is free software, licensed under the GNU General Public License, version 2 (GPL-2.0-only). See the `LICENSE` file for the full text.

Running db_deployer against your own SQL repository does not place your SQL or database under the GPL — that's arm's-length use, not distribution of a derivative work. The GPL's copyleft obligations apply when db_deployer itself (modified or unmodified) is redistributed, or when other code links against `db_deployer.lib` modules.
