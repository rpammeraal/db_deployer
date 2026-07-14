# db_deployer

Deploy database objects (schemas, tables, functions, views, indexes, roles, privileges) to PostgreSQL from a directory of SQL files. Tracks changes between deployments via a local cache so unchanged objects are skipped.

Packaged as a self-contained `.pyz` executable via [shiv](https://github.com/linkedin/shiv) — drop it on `$PATH` and it runs without a venv or a system-wide install.

## Requirements

- Python 3.10+
- PostgreSQL client library (`libpq`) available at build time
- Target PostgreSQL server (any recent version)

On macOS with Homebrew:

```
brew install libpq openssl@3
```

On Debian/Ubuntu:

```
sudo apt install libpq-dev
```

## Build and install

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

## Configuration

`db_deployer` reads all configuration from environment variables and `~/.pgpass`. No config file.

### Environment variables

| Variable | Purpose |
|---|---|
| `DB_DEPLOYER_REPO` | Path to the SQL repo containing `database/<name>/...` (required, or use `--repo`) |
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
    ├── rock_dev/                     ← directory name = postgres database name
    │   ├── schema/
    │   │   └── analytics.sql
    │   ├── table/
    │   │   ├── users.sql
    │   │   └── orders.sql
    │   ├── function/
    │   ├── view/
    │   ├── index/
    │   ├── role/
    │   └── privilege/
    └── another_db/
        └── ...
```

Supported object types: `schema`, `table`, `function`, `view`, `data`, `index`, `role`, `privilege`.

## Usage

```
export DB_DEPLOYER_REPO=/path/to/sql/repo
export PGHOST=globe.local
export PGUSER=roy
# ~/.pgpass provides passwords

db_deployer --db rock_dev --run              # deploy changed files to rock_dev
db_deployer --run                            # deploy to every db found under database/
db_deployer --db rock_dev --dev --run        # only files that have changed since last cache commit
db_deployer --db rock_dev --rebuild_cache    # mark everything as up-to-date without deploying
db_deployer --db rock_dev --verbose --run    # show SQL output during deployment
```

Without `--run`, the tool exits without doing anything — a safety measure to prevent accidental deploys.

### Options

| Option | Description |
|---|---|
| `--repo PATH` | Override `$DB_DEPLOYER_REPO` |
| `--db LIST` | Comma-separated list of database names to process |
| `--run` | Actually execute the deployment (required — otherwise dry) |
| `--dev` | Only deploy files that have changed since the last cache commit |
| `--rebuild_cache` | Mark all files as up-to-date without deploying anything |
| `--verbose` | Show SQL output during deployment |
| `-h`, `--help` | Show help |

## Development

Editable install into the build venv, so source edits are picked up without rebuilding the pyz:

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

To rebuild and reinstall after changes are ready to ship:

```
deactivate
make clean
make
sudo make install
```

## Makefile targets

| Target | What it does |
|---|---|
| `make` / `make build` | Build `db_deployer.pyz` |
| `make install` | Build and copy to `$PREFIX/bin/db_deployer` |
| `make uninstall` | Remove the installed binary |
| `make dev` | Editable install for live-edit development |
| `make check-deps` | Verify psycopg2 can be installed on this platform |
| `make clean` | Remove build artifacts (keeps the build venv) |
| `make distclean` | Also remove the build venv |

## Cross-platform builds

The resulting `.pyz` bundles a compiled psycopg2 linked against the local libpq. It runs on any machine with a compatible Python interpreter and libpq at the same path.

For a fleet with mixed macOS and Linux, build once per platform on a representative machine.

## Layout

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
            ├── db.py             # PostgreSQL connection wrapper
            ├── sqlfile.py        # parses individual .sql files
            ├── sqlpreprocessor.py # expands ___PRIMITIVES___
            ├── tablefield.py     # column definition helper
            └── util.py           # logging, misc helpers
```

## License

Copyright (C) 2026 Roy P. Ammeraal

db_deployer is free software, licensed under the GNU General Public License, version 2 (GPL-2.0-only). See the `LICENSE` file for the full text.

Running db_deployer against your own SQL repository does not place your SQL or database under the GPL — that's arm's-length use, not distribution of a derivative work. The GPL's copyleft obligations apply when db_deployer itself (modified or unmodified) is redistributed, or when other code links against `db_deployer.lib` modules.
