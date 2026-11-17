#!/usr/bin/env python3
"""
Apply db/migrations/NNN_*.sql in order, each once, recording them in ops.schema_migration.

Usage:  uv run --with 'psycopg[binary]' python db/migrate.py
Reads DATABASE_URL from the environment or from .env in the repo root.
"""
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def database_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("DATABASE_URL is not set (environment or .env)")


def main():
    files = sorted((ROOT / "db" / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute("create schema if not exists ops")
        conn.execute("""create table if not exists ops.schema_migration (
                          filename text primary key, applied_at timestamptz not null default now())""")
        done = {r[0] for r in conn.execute("select filename from ops.schema_migration")}
        pending = [f for f in files if f.name not in done]
        if not pending:
            print("Database is up to date.")
            return
        for f in pending:
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("insert into ops.schema_migration (filename) values (%s)", (f.name,))
            print(f"applied {f.name}")


if __name__ == "__main__":
    main()
