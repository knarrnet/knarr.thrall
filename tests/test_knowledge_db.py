# tests/test_knowledge_db.py
import os, tempfile, time
from db import ThrallDB

def test_knowledge_tables_created():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        db = ThrallDB(os.path.join(d, "test.db"))
        try:
            rows = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='thrall_knowledge'"
            ).fetchall()
            assert len(rows) == 1
            rows = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='thrall_knowledge_meta'"
            ).fetchall()
            assert len(rows) == 1
            rows = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='thrall_knowledge_fts'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            db.close()

def test_knowledge_meta_upsert():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        db = ThrallDB(os.path.join(d, "test.db"))
        try:
            db._conn.execute("""
                INSERT INTO thrall_knowledge_meta
                    (domain, version, description, author_node, author_pubkey,
                     acquired_at, file_count, chunk_count, ingestion_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("casino", "1.0.0", "test", "node1", "pk1",
                  "2026-03-23T00:00:00Z", 3, 30, "pending"))
            db._conn.commit()
            row = db._conn.execute(
                "SELECT * FROM thrall_knowledge_meta WHERE domain='casino'"
            ).fetchone()
            assert row["version"] == "1.0.0"
            assert row["ingestion_status"] == "pending"
        finally:
            db.close()
