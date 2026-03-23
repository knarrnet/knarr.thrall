# tests/test_knowledge.py
import os, json, tempfile, hashlib
import pytest
from concurrent.futures import ThreadPoolExecutor
from nacl.signing import SigningKey

def _make_pack(files=None, recipe=None, signing_key=None):
    """Helper: build a valid signed knowledge pack."""
    if files is None:
        files = {"rules.md": "# Rules\n\nRule one.\n\n## Section Two\nDetails."}
    if recipe is None:
        recipe = {}
    if signing_key is None:
        signing_key = SigningKey.generate()
    canonical = json.dumps({"files": files, "recipe": recipe},
                           sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()
    sig = signing_key.sign(content_hash.encode()).signature
    return {
        "domain": "test",
        "version": "1.0.0",
        "description": "Test pack",
        "files": files,
        "recipe": recipe,
        "metadata": {
            "author": "abc123",
            "author_pubkey": signing_key.verify_key.encode().hex(),
            "sha256": content_hash,
            "signature": sig.hex(),
        }
    }

def test_validate_valid_pack():
    from knowledge import KnowledgeManager
    km = KnowledgeManager.__new__(KnowledgeManager)
    km._max_pack_bytes = 5_242_880
    pack = _make_pack()
    # Should not raise
    km._validate_pack(pack)

def test_validate_bad_hash():
    from knowledge import KnowledgeManager
    km = KnowledgeManager.__new__(KnowledgeManager)
    km._max_pack_bytes = 5_242_880
    pack = _make_pack()
    pack["metadata"]["sha256"] = "wrong"
    with pytest.raises(ValueError, match="integrity"):
        km._validate_pack(pack)

def test_validate_bad_signature():
    from knowledge import KnowledgeManager
    km = KnowledgeManager.__new__(KnowledgeManager)
    km._max_pack_bytes = 5_242_880
    pack = _make_pack()
    pack["metadata"]["signature"] = "00" * 64
    with pytest.raises(ValueError, match="signature"):
        km._validate_pack(pack)

def test_chunk_markdown():
    from knowledge import chunk_markdown
    # Each section must be >= 100 chars to avoid being merged
    section_a_body = "This is a substantial paragraph about section A. " * 3
    section_b_body = "This is a substantial paragraph about section B. " * 3
    md = (
        "# Title\n\nIntro paragraph with enough content to be meaningful.\n\n"
        f"## Section A\n\n{section_a_body}\n\n"
        f"## Section B\n\n{section_b_body}"
    )
    chunks = chunk_markdown(md, "test.md", "testdomain")
    assert len(chunks) >= 2
    assert all("chunk_text" in c for c in chunks)
    assert all("source_file" in c for c in chunks)

def test_chunk_merges_small_sections():
    from knowledge import chunk_markdown
    md = "## A\n\nTiny.\n\n## B\n\nAlso tiny."
    chunks = chunk_markdown(md, "test.md", "testdomain")
    # Both sections < 100 chars, should be merged
    assert len(chunks) == 1

def test_pack_size_limit():
    from knowledge import KnowledgeManager
    km = KnowledgeManager.__new__(KnowledgeManager)
    km._max_pack_bytes = 100
    big_files = {"huge.md": "x" * 200}
    pack = _make_pack(files=big_files)
    with pytest.raises(ValueError, match="exceeds max_pack_bytes"):
        km._validate_pack(pack)


# ── Ingest / Query / Remove tests ──

def test_ingest_accepts_valid_pack():
    """Full ingest flow with FTS5 fallback (no sqlite-vec in test)."""
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(
            db=db, backend=None, plugin_dir=d,
            config={"trust_level": "none", "max_domains": 10})
        pack = _make_pack(files={
            "rules.md": "# Rules\n\n## Section One\nContent one.\n\n## Section Two\nContent two."
        })
        result = km.ingest(pack, sender_node_id="abc123")
        assert result["status"] == "accepted"
        assert result["files"] == 1
        # Wait for background thread
        km._executor.shutdown(wait=True)
        # Check metadata
        meta = km.get_domain_status("test")
        assert meta is not None
        assert meta["ingestion_status"] == "ready"
        assert meta["chunk_count"] >= 1
        # Close DB before cleanup
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_ingest_idempotent_same_version():
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(db=db, backend=None, plugin_dir=d, config={})
        pack = _make_pack()
        km.ingest(pack)
        km._executor.shutdown(wait=True)
        # Re-create executor since shutdown kills it
        km._executor = ThreadPoolExecutor(max_workers=1)
        result = km.ingest(pack)
        assert result["status"] == "exists"
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_ingest_rejects_lower_version():
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(db=db, backend=None, plugin_dir=d, config={})
        pack_v2 = _make_pack()
        pack_v2["version"] = "2.0.0"
        km.ingest(pack_v2)
        km._executor.shutdown(wait=True)
        # Re-create executor since shutdown kills it
        km._executor = ThreadPoolExecutor(max_workers=1)
        pack_v1 = _make_pack()
        pack_v1["version"] = "1.0.0"
        result = km.ingest(pack_v1)
        assert result["status"] == "newer_exists"
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_query_knowledge_fts5_fallback():
    """Query using FTS5 when sqlite-vec is unavailable."""
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(db=db, backend=None, plugin_dir=d, config={})
        pack = _make_pack(files={
            "rules.md": "## Game Rules\n\nPlayers roll dice and score points.\n\n## Strategy\n\nAlways bet on high numbers."
        })
        km.ingest(pack)
        km._executor.shutdown(wait=True)
        result = km.query_knowledge("test", "dice rules")
        assert "dice" in result.lower() or "rules" in result.lower()
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_query_knowledge_empty_domain():
    """Query nonexistent domain returns empty string."""
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(db=db, backend=None, plugin_dir=d, config={})
        result = km.query_knowledge("nonexistent", "query")
        assert result == ""
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_remove_domain():
    from db import ThrallDB
    from knowledge import KnowledgeManager
    d = tempfile.mkdtemp()
    try:
        db = ThrallDB(os.path.join(d, "test.db"))
        km = KnowledgeManager(db=db, backend=None, plugin_dir=d, config={})
        km.ingest(_make_pack())
        km._executor.shutdown(wait=True)
        assert km.get_domain_status("test") is not None
        km.remove_domain("test")
        assert km.get_domain_status("test") is None
        db._conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
