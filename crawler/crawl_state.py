from __future__ import annotations

from typing import Optional

from platform_db import connect, initialize_database


def current_document(url: str, workspace_id: str, db_path=None) -> Optional[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE workspace_id=? AND canonical_url=? AND is_current=1 ORDER BY document_version DESC LIMIT 1",
            (workspace_id, url),
        ).fetchone()
    return dict(row) if row else None


def document_by_hash(digest: str, workspace_id: str, db_path=None) -> Optional[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE workspace_id=? AND content_hash=? AND is_current=1 LIMIT 1",
            (workspace_id, digest),
        ).fetchone()
    return dict(row) if row else None
