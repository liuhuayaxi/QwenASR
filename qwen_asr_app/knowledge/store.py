"""SQLite WAL storage, FTS char ngrams, NumPy vector search, and transactional revision switching."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..lazy import lazy_module

np = lazy_module("numpy")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    category TEXT UNIQUE NOT NULL,
    folder_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    category TEXT NOT NULL,
    asset_id TEXT,
    lesson_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_type TEXT NOT NULL,
    active_revision TEXT NOT NULL,
    file_path TEXT,
    file_mtime REAL,
    file_size INTEGER,
    stat_hash TEXT,
    open_url TEXT NOT NULL,
    lineage TEXT,
    needs_vectors INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_category ON sources(category);
CREATE INDEX IF NOT EXISTS idx_sources_course ON sources(course_id);


CREATE TABLE IF NOT EXISTS source_revisions (
    revision_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_contract TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    citation_id TEXT UNIQUE NOT NULL,
    source_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    course_id TEXT NOT NULL,
    category TEXT NOT NULL,
    lesson_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    text TEXT NOT NULL,
    heading TEXT,
    page INTEGER,
    start REAL,
    end REAL,
    segment_ids TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_contract TEXT NOT NULL,
    open_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_category ON chunks(category);
CREATE INDEX IF NOT EXISTS idx_chunks_active ON chunks(is_active);

CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT PRIMARY KEY,
    embedding_contract TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS knowledge_jobs (
    job_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    stage TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    ngrams,
    category UNINDEXED,
    tokenize="unicode61"
);
"""


def make_citation_id(chunk_id: str) -> str:
    """Generate safe K + hex citation ID."""
    return "K" + hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()[:12]


def to_char_ngrams(text: str) -> str:
    """Generate unigram and bigram tokens for CJK and keep alphanumeric words."""
    if not text:
        return ""
    tokens: list[str] = []
    # Identify words and CJK characters
    words = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_.+-]+", text.lower())
    for w in words:
        cjk_chars = [c for c in w if "\u4e00" <= c <= "\u9fff"]
        if cjk_chars:
            tokens.extend(cjk_chars)
            for i in range(len(cjk_chars) - 1):
                tokens.append(cjk_chars[i] + cjk_chars[i + 1])
        else:
            tokens.append(w)
    return " ".join(tokens)


class KnowledgeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
            if "asset_id" not in cols:
                conn.execute("ALTER TABLE sources ADD COLUMN asset_id TEXT")
            if "needs_vectors" not in cols:
                conn.execute("ALTER TABLE sources ADD COLUMN needs_vectors INTEGER NOT NULL DEFAULT 0")
            if "error" not in cols:
                conn.execute("ALTER TABLE sources ADD COLUMN error TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_asset ON sources(asset_id)")
            conn.execute('CREATE TABLE IF NOT EXISTS knowledge_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            version=conn.execute("SELECT value FROM knowledge_meta WHERE key='lexical_version'").fetchone()
            if not version or version['value']!='zh-en-v2':
                conn.execute('DELETE FROM chunks_fts')
                rows=conn.execute('SELECT chunk_id,text,category FROM chunks WHERE is_active=1').fetchall()
                conn.executemany('INSERT INTO chunks_fts (chunk_id,content,ngrams,category) VALUES (?,?,?,?)',
                                 [(r['chunk_id'],r['text'],to_char_ngrams(r['text']),r['category']) for r in rows])
                conn.execute("INSERT OR REPLACE INTO knowledge_meta VALUES ('lexical_version','zh-en-v2')")

    # ---------------- Course management ----------------

    def get_course(self, category: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM courses WHERE category = ?", (category,)).fetchone()
            return dict(row) if row else None

    def get_course_by_id(self, course_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM courses WHERE course_id = ?", (course_id,)).fetchone()
            return dict(row) if row else None

    def upsert_course(self, course_id: str, category: str, folder_path: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            existing_by_id = conn.execute("SELECT category FROM courses WHERE course_id = ?", (course_id,)).fetchone()
            if existing_by_id:
                old_category = existing_by_id["category"]
                if old_category != category:
                    conn.execute(
                        "UPDATE courses SET category = ?, folder_path = ?, updated_at = ? WHERE course_id = ?",
                        (category, folder_path, now, course_id),
                    )
                    conn.execute(
                        "UPDATE sources SET category = ?, updated_at = ? WHERE course_id = ?",
                        (category, now, course_id),
                    )
                    conn.execute(
                        "UPDATE chunks SET category = ? WHERE course_id = ?",
                        (category, course_id),
                    )
                    conn.execute(
                        "UPDATE chunks_fts SET category = ? WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE course_id = ?)",
                        (category, course_id),
                    )
                    conn.execute(
                        "UPDATE knowledge_jobs SET category = ? WHERE category = ?",
                        (category, old_category),
                    )
                else:
                    conn.execute(
                        "UPDATE courses SET folder_path = ?, updated_at = ? WHERE course_id = ?",
                        (folder_path, now, course_id),
                    )
                return

            existing_by_cat = conn.execute("SELECT course_id FROM courses WHERE category = ?", (category,)).fetchone()
            if existing_by_cat:
                conn.execute(
                    "UPDATE courses SET course_id = ?, folder_path = ?, updated_at = ? WHERE category = ?",
                    (course_id, folder_path, now, category),
                )
                return

            conn.execute(
                """
                INSERT INTO courses (course_id, category, folder_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (course_id, category, folder_path, now, now),
            )

    # ---------------- Source & Revisions ----------------

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
            return dict(row) if row else None

    def list_sources(self, category: str, active_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM sources WHERE category = ? AND status = 'active'",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sources WHERE category = ?", (category,)).fetchall()
            return [dict(r) for r in rows]

    def tombstone_source(self, source_id: str) -> None:
        """Tombstone a missing or deleted source and deactivate its chunks."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "UPDATE sources SET status = 'tombstoned', updated_at = ? WHERE source_id = ?",
                (now, source_id),
            )
            # Deactivate chunks in chunks table
            conn.execute(
                "UPDATE chunks SET is_active = 0 WHERE source_id = ?",
                (source_id,),
            )
            # Remove from FTS
            conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id = ?)",
                (source_id,),
            )

    def restore_source(self, source_id: str, revision: str) -> None:
        """Restore a tombstoned source when its file is restored on disk."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "UPDATE sources SET status = 'active', updated_at = ? WHERE source_id = ?",
                (now, source_id),
            )
            conn.execute(
                "UPDATE chunks SET is_active = 1 WHERE source_id = ? AND revision = ?",
                (source_id, revision),
            )
            # Re-insert into FTS
            active_chunks = conn.execute(
                "SELECT chunk_id, text, category FROM chunks WHERE source_id = ? AND revision = ? AND is_active = 1",
                (source_id, revision),
            ).fetchall()
            for c in active_chunks:
                conn.execute(
                    "INSERT INTO chunks_fts (chunk_id, content, ngrams, category) VALUES (?, ?, ?, ?)",
                    (c["chunk_id"], c["text"], to_char_ngrams(c["text"]), c["category"]),
                )

    def mark_source_stale_and_hide_chunks(self, source_id: str, reason: str = "file_stat_changed") -> None:
        """Immediately deactivate chunks and remove from FTS when underlying file changes before reindex."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "UPDATE sources SET status = 'stale', error = ?, updated_at = ? WHERE source_id = ?",
                (reason, now, source_id),
            )
            conn.execute(
                "UPDATE chunks SET is_active = 0 WHERE source_id = ?",
                (source_id,),
            )
            conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id = ?)",
                (source_id,),
            )

    def record_source_failure(
        self,
        source_id: str,
        course_id: str,
        category: str,
        title: str,
        kind: str,
        file_path: str | None,
        error_msg: str,
        asset_id: str | None = None,
    ) -> None:
        """Record explicit parse failure so files are not silently discarded."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (
                    source_id, course_id, category, asset_id, title, kind, origin,
                    source_type, active_revision, file_path, status, error,
                    open_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'original', 'failed_parse', '', ?, 'failed', ?, '', ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    status='failed',
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (source_id, course_id, category, asset_id, title, kind, file_path, error_msg, now, now),
            )
            conn.execute("UPDATE chunks SET is_active = 0 WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id = ?)", (source_id,))

    def cached_vectors(self, hashes, contract):
        key=json.dumps(contract, sort_keys=True) if isinstance(contract,dict) else str(contract or '')
        found={}
        with self._connect() as conn:
            for start in range(0,len(hashes),500):
                batch=hashes[start:start+500]
                if not batch:
                    continue
                marks=','.join('?' for _ in batch)
                rows=conn.execute(f'SELECT c.content_hash,cv.vector,cv.dimension FROM chunks c JOIN chunk_vectors cv USING(chunk_id) WHERE c.content_hash IN ({marks}) AND cv.embedding_contract=?', [*batch,key]).fetchall()
                for row in rows:
                    values=np.frombuffer(row['vector'],dtype=np.float32)
                    if len(values)==row['dimension']:
                        found[row['content_hash']]=values.tolist()
        return found

    # ---------------- Transactional Old/New Switch ----------------

    def commit_source_revision(
        self,
        source_meta: dict[str, Any],
        revision: str,
        content_hash: str,
        parser_version: str,
        chunker_version: str,
        embedding_contract: dict[str, Any] | str,
        chunks_data: list[dict[str, Any]],
        vectors: list[list[float]] | None,
    ) -> None:
        """Transactionally replace active chunks of a source only after embedding succeeds.
        If fail, leaves stale hidden and rolls back."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        source_id = source_meta["source_id"]
        contract_str = (
            json.dumps(embedding_contract, sort_keys=True)
            if isinstance(embedding_contract, dict)
            else str(embedding_contract or "")
        )

        needs_vectors = 1 if vectors is None or any(v is None for v in vectors) else 0
        if vectors is None and (not contract_str or contract_str == "{}"):
            contract_str = "lexical-only"

        with self._connect() as conn:
            cursor = conn.cursor()
            try:
                # 1. Upsert source record
                cursor.execute(
                    """
                    INSERT INTO sources (
                        source_id, course_id, category, asset_id, lesson_id, title, kind,
                        origin, source_type, active_revision, file_path, file_mtime,
                        file_size, stat_hash, open_url, lineage, needs_vectors, error, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        course_id=excluded.course_id,
                        category=excluded.category,
                        asset_id=excluded.asset_id,
                        lesson_id=excluded.lesson_id,
                        title=excluded.title,
                        kind=excluded.kind,
                        origin=excluded.origin,
                        source_type=excluded.source_type,
                        active_revision=excluded.active_revision,
                        file_path=excluded.file_path,
                        file_mtime=excluded.file_mtime,
                        file_size=excluded.file_size,
                        stat_hash=excluded.stat_hash,
                        open_url=excluded.open_url,
                        lineage=excluded.lineage,
                        needs_vectors=excluded.needs_vectors,
                        error=NULL,
                        status='active',
                        updated_at=excluded.updated_at
                    """,
                    (
                        source_id,
                        source_meta["course_id"],
                        source_meta["category"],
                        source_meta.get("asset_id"),
                        source_meta.get("lesson_id"),
                        source_meta["title"],
                        source_meta["kind"],
                        source_meta["origin"],
                        source_meta["source_type"],
                        revision,
                        source_meta.get("file_path"),
                        source_meta.get("file_mtime"),
                        source_meta.get("file_size"),
                        source_meta.get("stat_hash"),
                        source_meta["open_url"],
                        json.dumps(source_meta.get("lineage") or {}),
                        needs_vectors,
                        now,
                        now,
                    ),
                )

                # 2. Record source revision
                rev_id = f"{source_id}:{revision}"
                cursor.execute(
                    """
                    INSERT INTO source_revisions (
                        revision_id, source_id, revision, content_hash, parser_version,
                        chunker_version, embedding_contract, is_active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(revision_id) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        embedding_contract=excluded.embedding_contract,
                        is_active=1
                    """,
                    (rev_id, source_id, revision, content_hash, parser_version, chunker_version, contract_str, now),
                )

                # 3. Deactivate old chunks for this source
                cursor.execute("UPDATE chunks SET is_active = 0 WHERE source_id = ?", (source_id,))
                cursor.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id = ?)",
                    (source_id,),
                )

                # 4. Insert new chunks and vectors
                for idx, c in enumerate(chunks_data):
                    chunk_id = c["chunk_id"]
                    citation_id = c.get("citation_id") or make_citation_id(chunk_id)
                    seg_ids_json = json.dumps(c.get("segment_ids") or [])

                    cursor.execute(
                        """
                        INSERT INTO chunks (
                            chunk_id, citation_id, source_id, revision, content_hash,
                            chunk_index, course_id, category, lesson_id, title,
                            kind, origin, text, heading, page, start, end,
                            segment_ids, parser_version, chunker_version, embedding_contract,
                            open_url, is_active, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(chunk_id) DO UPDATE SET
                            citation_id=excluded.citation_id,
                            revision=excluded.revision,
                            content_hash=excluded.content_hash,
                            chunk_index=excluded.chunk_index,
                            title=excluded.title,
                            kind=excluded.kind,
                            origin=excluded.origin,
                            text=excluded.text,
                            heading=excluded.heading,
                            page=excluded.page,
                            start=excluded.start,
                            end=excluded.end,
                            segment_ids=excluded.segment_ids,
                            parser_version=excluded.parser_version,
                            chunker_version=excluded.chunker_version,
                            embedding_contract=excluded.embedding_contract,
                            open_url=excluded.open_url,
                            is_active=1
                        """,
                        (
                            chunk_id,
                            citation_id,
                            source_id,
                            revision,
                            c["content_hash"],
                            idx,
                            source_meta["course_id"],
                            source_meta["category"],
                            source_meta.get("lesson_id"),
                            source_meta["title"],
                            source_meta["kind"],
                            source_meta["origin"],
                            c["text"],
                            c.get("heading"),
                            c.get("page"),
                            c.get("start"),
                            c.get("end"),
                            seg_ids_json,
                            parser_version,
                            chunker_version,
                            contract_str,
                            c.get("open_url") or source_meta["open_url"],
                            now,
                        ),
                    )

                    # Insert into FTS
                    cursor.execute(
                        "INSERT INTO chunks_fts (chunk_id, content, ngrams, category) VALUES (?, ?, ?, ?)",
                        (chunk_id, c["text"], to_char_ngrams(c["text"]), source_meta["category"]),
                    )

                    # Insert vector if present
                    if vectors is not None and idx < len(vectors) and vectors[idx] is not None:
                        v_arr = np.array(vectors[idx], dtype=np.float32)
                        v_blob = v_arr.tobytes()
                        cursor.execute(
                            """
                            INSERT INTO chunk_vectors (chunk_id, embedding_contract, dimension, vector)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(chunk_id) DO UPDATE SET
                                embedding_contract=excluded.embedding_contract,
                                dimension=excluded.dimension,
                                vector=excluded.vector
                            """,
                            (chunk_id, contract_str, len(v_arr), v_blob),
                        )

                # Search uses active generations only. Keep historical text and
                # evidence metadata, avoiding copies of large obsolete vectors.
                cursor.execute('DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id=? AND is_active=0)',(source_id,))
                cursor.execute('UPDATE sources SET error=? WHERE source_id=?',(source_meta.get('error'),source_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ---------------- Retrieval & Search ----------------

    def search_lexical(
        self,
        category: str,
        query: str,
        limit: int = 20,
        lesson_id: str | None = None,
        source_types: list[str] | None = None,
        include_derived: bool = False,
    ) -> list[dict[str, Any]]:
        """Search chunks using FTS5 char ngrams BM25 ranking."""
        tokens=[]
        for run in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_.+-]+',query.lower()):
            if re.search(r'[\u4e00-\u9fff]',run):
                tokens.extend(run[i:i+2] for i in range(len(run)-1))
                if len(run)==1:
                    tokens.append(run)
            else:
                tokens.append(run)
        stop={'这个','那个','什么','老师','上次','刚才','这里','怎么','为何','哪些','怎样','这次','那段','多少','如何','的是','了吗'}
        tokens=list(dict.fromkeys(t for t in tokens if t not in stop))[:64]
        if not tokens:
            return []
        match_query = 'ngrams : (' + ' OR '.join('"'+t.replace('"','""')+'"' for t in tokens) + ')'

        sql = """
            SELECT c.*, s.asset_id, bm25(chunks_fts) as fts_rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            JOIN sources s ON s.source_id = c.source_id
            WHERE chunks_fts MATCH ?
              AND c.category = ?
              AND c.is_active = 1
              AND s.status = 'active'
        """
        params: list[Any] = [match_query, category]

        if lesson_id:
            sql += " AND c.lesson_id = ?"
            params.append(lesson_id)

        if not include_derived:
            sql += " AND (c.origin != 'ai_derived' OR s.source_type = 'recording_refined')"

        if source_types:
            placeholders = ",".join("?" for _ in source_types)
            sql += f" AND s.source_type IN ({placeholders})"
            params.extend(source_types)

        sql += " ORDER BY fts_rank ASC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def search_vectors(
        self,
        category: str,
        query_vector: list[float],
        embedding_contract: dict[str, Any] | str,
        limit: int = 20,
        lesson_id: str | None = None,
        source_types: list[str] | None = None,
        include_derived: bool = False,
    ) -> list[dict[str, Any]]:
        """NumPy exact cosine similarity vector search."""
        contract_str = (
            json.dumps(embedding_contract, sort_keys=True)
            if isinstance(embedding_contract, dict)
            else str(embedding_contract or "")
        )

        sql = """
            SELECT c.*, s.asset_id, cv.vector, cv.dimension
            FROM chunks c
            JOIN chunk_vectors cv ON cv.chunk_id = c.chunk_id
            JOIN sources s ON s.source_id = c.source_id
            WHERE c.category = ?
              AND c.is_active = 1
              AND s.status = 'active'
              AND cv.embedding_contract = ?
        """
        params: list[Any] = [category, contract_str]

        if lesson_id:
            sql += " AND c.lesson_id = ?"
            params.append(lesson_id)

        if not include_derived:
            sql += " AND (c.origin != 'ai_derived' OR s.source_type = 'recording_refined')"

        if source_types:
            placeholders = ",".join("?" for _ in source_types)
            sql += f" AND s.source_type IN ({placeholders})"
            params.extend(source_types)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return []

        q_arr = np.array(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q_arr)
        if q_norm < 1e-9:
            return []
        q_arr = q_arr / q_norm

        vectors_list = []
        valid_rows = []
        for r in rows:
            dim = r["dimension"]
            v_arr = np.frombuffer(r["vector"], dtype=np.float32)
            if len(v_arr) == dim and len(v_arr) == len(q_arr):
                v_norm = np.linalg.norm(v_arr)
                if v_norm > 1e-9:
                    vectors_list.append(v_arr / v_norm)
                    valid_rows.append(r)

        if not vectors_list:
            return []

        matrix = np.vstack(vectors_list)
        sims = np.dot(matrix, q_arr)

        top_indices = np.argsort(-sims)[:limit]
        results = []
        for idx in top_indices:
            if float(sims[idx]) < 0.35:
                continue
            row_dict = dict(valid_rows[idx])
            row_dict.pop("vector", None)
            row_dict["vector_score"] = float(sims[idx])
            results.append(row_dict)

        return results

    def get_neighbor_context(self, source_id: str, revision: str, chunk_index: int) -> dict[str, Any]:
        """Fetch immediate predecessor and successor chunks for context expansion."""
        with self._connect() as conn:
            prev_row = conn.execute(
                "SELECT chunk_id, text, heading, page, start, end FROM chunks WHERE source_id = ? AND revision = ? AND chunk_index = ?",
                (source_id, revision, chunk_index - 1),
            ).fetchone()
            next_row = conn.execute(
                "SELECT chunk_id, text, heading, page, start, end FROM chunks WHERE source_id = ? AND revision = ? AND chunk_index = ?",
                (source_id, revision, chunk_index + 1),
            ).fetchone()

            return {
                "prev": dict(prev_row) if prev_row else None,
                "next": dict(next_row) if next_row else None,
            }

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
            return dict(row) if row else None

    # ---------------- Job queue ----------------

    def create_job(self, job_id: str, category: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_jobs (job_id, category, status, progress, stage, message, created_at, updated_at)
                VALUES (?, ?, 'queued', 0.0, 'queued', '等待处理', ?, ?)
                """,
                (job_id, category, now, now),
            )

    def update_job(
        self,
        job_id: str,
        status: str,
        progress: float | None = None,
        stage: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]

        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if stage is not None:
            updates.append("stage = ?")
            params.append(stage)
        if message is not None:
            updates.append("message = ?")
            params.append(message)
        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(job_id)
        sql = f"UPDATE knowledge_jobs SET {', '.join(updates)} WHERE job_id = ?"
        with self._connect() as conn:
            conn.execute(sql, params)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_active_job_for_category(self, category: str) -> dict[str, Any] | None:
        """Find active queued or running job for category to coalesce."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_jobs WHERE category = ? AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
                (category,),
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def recover_interrupted_jobs(self) -> list[str]:
        """Recover running, queued, or interrupted jobs after restart, resetting them to queued."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        recovered = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM knowledge_jobs WHERE status IN ('running', 'queued', 'interrupted') ORDER BY created_at ASC"
            ).fetchall()
            for r in rows:
                jid = r["job_id"]
                conn.execute(
                    "UPDATE knowledge_jobs SET status = 'queued', message = '服务重启，任务恢复排队', updated_at = ? WHERE job_id = ?",
                    (now, jid),
                )
                recovered.append(jid)
        return recovered
