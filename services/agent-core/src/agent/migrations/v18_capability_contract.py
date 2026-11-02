"""Schema 18 capability-contract migration.

Capability and route health state lives in the desktop settings authority, so
the Core database requires no table rewrite.  Keeping an explicit migration
function makes the version transition durable and gives future installations a
single place to add capability-scoped persistence if needed.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    # Deliberately a no-op: v18 changes the IPC vocabulary and settings-side
    # route state, not the conversation tables. The caller advances
    # schema_info in the same transaction as other migrations.
    del connection
