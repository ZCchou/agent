"""Managed primary generation models. The caller owns the transaction."""
import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS local_models (
        id TEXT PRIMARY KEY, provider_profile_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'generation', format TEXT NOT NULL DEFAULT 'gguf',
        architecture TEXT NOT NULL, quantization TEXT NOT NULL, context_length INTEGER NOT NULL,
        max_context_length INTEGER NOT NULL, size_bytes INTEGER NOT NULL, digest TEXT NOT NULL UNIQUE,
        template_digest TEXT NOT NULL, adapter_digest TEXT NOT NULL, device TEXT NOT NULL DEFAULT 'cpu',
        gpu_layers INTEGER NOT NULL DEFAULT -1, threads INTEGER NOT NULL, status TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
        source TEXT, license TEXT, error TEXT, files_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS local_models_one_default
        ON local_models(is_default) WHERE is_default=1""")
    connection.execute("""CREATE TABLE IF NOT EXISTS local_model_jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, model_id TEXT, status TEXT NOT NULL
        CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
        progress REAL NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS local_model_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
