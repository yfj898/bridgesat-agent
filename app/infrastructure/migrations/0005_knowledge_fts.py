"""0005: knowledge_fts.

Governed retrieval index: a derived FTS5 index over published content
(questions and lessons) plus an audit log of what was indexed and when.
The content registry (0002) remains authoritative; this index is rebuilt
from published packs only.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            content_id UNINDEXED,
            version UNINDEXED,
            content_type UNINDEXED,
            target_skill UNINDEXED,
            target_subskill UNINDEXED,
            audience UNINDEXED,
            license_id UNINDEXED,
            license_name UNINDEXED,
            source_id UNINDEXED,
            review_status UNINDEXED,
            body,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS knowledge_index_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            indexed_at TEXT NOT NULL,
            pack_id TEXT NOT NULL,
            pack_version TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            lesson_count INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
