"""按需仓库持久化：固定目录、原始对象、索引代次和保留关系共用短事务。

网络/解析不持有SQLite写锁；有占用/待发布结果的对象不回收。
会话与用量数据库不在此模块中修改。恢复不凭租约时间猜测任务已结束。
"""

import hashlib
import importlib.metadata
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from repoagent.repositories.github import GitHubError, verify_blob
from repoagent.repositories.text import (
    DECODER_VERSION,
    SUPPORTED_DECODERS,
    DecodedText,
    decode_text,
    profile,
)
from repoagent.snapshots.frontend import LANGUAGES, parse_frontend
from repoagent.snapshots.java import parse_java
from repoagent.snapshots.repository import (
    CONFIG_NAMES,
    EXCLUDED_DIRS,
    EXTENSIONS,
    Limits,
    safe_relative,
)
from repoagent.storage import guard_database


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def parser_versions():
    return {
        name: importlib.metadata.version(name)
        for name in (
            "tree-sitter",
            "tree-sitter-java",
            "tree-sitter-typescript",
            "tree-sitter-javascript",
        )
    } | {"index_policy": "n3-index-v1"}


def file_policy(entry):
    path = Path(entry["path"])
    if entry["type"] == "tree":
        return "directory"
    if entry["mode"] not in {"100644", "100755"}:
        return "links_or_submodules"
    if set(path.parts) & EXCLUDED_DIRS:
        return "excluded_directory"
    if path.suffix not in EXTENSIONS and path.name not in CONFIG_NAMES:
        return "unsupported_file"
    if entry["size"] > Limits().max_file_bytes:
        return "file_too_large"
    return "allowed"


class RepositoryStore:
    def __init__(self, directory, *, capacity=512 * 1024 * 1024, disk_limit=1024**3):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, exist_ok=True)
        for parent in (self.directory, *self.directory.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("Managed storage must not use links")
        self.path = self.directory / "repository.sqlite3"
        self.capacity, self.disk_limit = capacity, disk_limit
        self.fetch_lock = threading.RLock()
        guard_database(self.path, "repository-current-v1")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repository_meta(version INTEGER PRIMARY KEY CHECK(version=1));
                INSERT OR IGNORE INTO repository_meta VALUES(1);
                CREATE TABLE IF NOT EXISTS revisions(
                    id TEXT PRIMARY KEY, repository_id INTEGER NOT NULL, name TEXT NOT NULL,
                    commit_id TEXT NOT NULL, tree_id TEXT NOT NULL, catalog_digest TEXT NOT NULL,
                    ref TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS file_entries(
                    revision TEXT NOT NULL REFERENCES revisions(id), path TEXT NOT NULL,
                    entry TEXT NOT NULL, policy TEXT NOT NULL,
                    PRIMARY KEY(revision,path));
                CREATE TABLE IF NOT EXISTS content_objects(
                    digest TEXT PRIMARY KEY, git_blob TEXT NOT NULL, size INTEGER NOT NULL,
                    data BLOB NOT NULL, touched INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS content_git_blob ON content_objects(git_blob,size);
                CREATE TABLE IF NOT EXISTS materializations(
                    revision TEXT NOT NULL, path TEXT NOT NULL, digest TEXT REFERENCES content_objects(digest),
                    parse_state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', symbols TEXT NOT NULL,
                    PRIMARY KEY(revision,path), FOREIGN KEY(revision,path) REFERENCES file_entries(revision,path));
                CREATE TABLE IF NOT EXISTS parsed_versions(revision TEXT NOT NULL,path TEXT NOT NULL,version TEXT NOT NULL,PRIMARY KEY(revision,path));
                CREATE TABLE IF NOT EXISTS fetch_states(revision TEXT NOT NULL,path TEXT NOT NULL,state TEXT NOT NULL,error TEXT,PRIMARY KEY(revision,path));
                CREATE TABLE IF NOT EXISTS pins(
                    owner TEXT NOT NULL, revision TEXT NOT NULL, path TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('inflight','publication_pending','published','reading')),
                    PRIMARY KEY(owner,revision,path),
                    FOREIGN KEY(revision,path) REFERENCES materializations(revision,path));
                CREATE TABLE IF NOT EXISTS runs(
                    owner TEXT PRIMARY KEY, run_id TEXT UNIQUE, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations(owner TEXT PRIMARY KEY,size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS version_pins(owner TEXT NOT NULL,revision TEXT NOT NULL REFERENCES revisions(id),PRIMARY KEY(owner,revision));
                CREATE TABLE IF NOT EXISTS import_jobs(
                    id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS text_profiles(revision TEXT PRIMARY KEY REFERENCES revisions(id), profile TEXT NOT NULL, decoder TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS text_views(id TEXT PRIMARY KEY, raw_digest TEXT NOT NULL REFERENCES content_objects(digest), encoding TEXT NOT NULL, decoder TEXT NOT NULL, data BLOB NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS file_text_views(revision TEXT NOT NULL,path TEXT NOT NULL,view_id TEXT NOT NULL REFERENCES text_views(id),PRIMARY KEY(revision,path),FOREIGN KEY(revision,path) REFERENCES materializations(revision,path));
                CREATE TABLE IF NOT EXISTS storage_extensions(name TEXT PRIMARY KEY,version INTEGER NOT NULL);
                INSERT OR IGNORE INTO storage_extensions VALUES('source-text',1);
            """)
            if db.execute(
                "SELECT version FROM storage_extensions WHERE name='source-text'"
            ).fetchone() != (1,):
                raise ValueError("Unsupported text storage schema")
            if db.execute("select version from repository_meta").fetchall() != [(1,)]:
                raise ValueError("Unsupported repository storage schema")
            changed = db.execute(
                "SELECT DISTINCT m.revision FROM materializations m LEFT JOIN parsed_versions p ON p.revision=m.revision AND p.path=m.path WHERE m.parse_state IN ('parsed','text_only') AND (p.version IS NULL OR p.version!=?)",
                (encode(parser_versions()),),
            ).fetchall()
            db.execute(
                "UPDATE materializations SET parse_state='cached',reason='parser_changed',symbols='[]' WHERE parse_state IN ('parsed','text_only') AND NOT EXISTS(SELECT 1 FROM parsed_versions p WHERE p.revision=materializations.revision AND p.path=materializations.path AND p.version=?)",
                (encode(parser_versions()),),
            )
            db.executemany(
                "UPDATE revisions SET generation=generation+1 WHERE id=?", changed
            )

    @contextmanager
    def connect(self):
        if any(
            p.is_symlink() or p.is_junction()
            for p in (self.directory, *self.directory.parents)
        ):
            raise ValueError("Managed storage path changed to a link")
        if any(
            p.is_symlink() or p.is_junction()
            for p in self.directory.glob("repository.sqlite3*")
        ):
            raise ValueError("Linked repository database or journal rejected")
        if self.path.is_symlink() or self.path.is_junction():
            raise ValueError("Linked repository database rejected")
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("pragma foreign_keys=on")
            db.execute("pragma synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def register(
        self, meta, commit, tree, entries, ref, *, text_profile=None, text_decoder=None
    ):
        text_profile = profile(text_profile)
        text_decoder = text_decoder or DECODER_VERSION
        if text_decoder not in SUPPORTED_DECODERS:
            raise GitHubError("unsupported_decoder_version")
        self.guard_space(len(encode(entries).encode()) * 4)
        allowed = [e for e in entries if file_policy(e) == "allowed"]
        limits = Limits()
        if (
            len(allowed) > limits.max_readable_files
            or sum(e["size"] for e in allowed) > limits.max_total_bytes
        ):
            raise GitHubError("catalog_policy_limit")
        if (
            sum(Path(e["path"]).suffix == ".java" for e in allowed)
            > limits.max_java_files
            or sum(Path(e["path"]).suffix in {".java", ".ts", ".js"} for e in allowed)
            > limits.max_source_files
        ):
            raise GitHubError("catalog_policy_limit")
        catalog = hashlib.sha256(encode(entries).encode()).hexdigest()
        identity = {
            "schema": "github-current-v1",
            "text_profile": text_profile,
            "decoder": text_decoder,
            "repository_id": meta["id"],
            "commit": commit,
            "tree": tree,
            "catalog": catalog,
            "policy": "n3-files-v1",
        }
        allowed_paths = {e["path"] for e in allowed}
        if set(text_profile["overrides"]) - allowed_paths:
            raise GitHubError("file_outside_readable_catalog")
        version = hashlib.sha256(encode(identity).encode()).hexdigest()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO revisions(id,repository_id,name,commit_id,tree_id,catalog_digest,ref) VALUES(?,?,?,?,?,?,?)",
                (version, meta["id"], meta["full_name"], commit, tree, catalog, ref),
            )
            db.execute(
                "INSERT OR IGNORE INTO text_profiles(revision,profile,decoder) VALUES(?,?,?)",
                (version, encode(text_profile), text_decoder),
            )
            db.execute(
                "UPDATE revisions SET name=? WHERE repository_id=?",
                (meta["full_name"], meta["id"]),
            )
            db.executemany(
                "INSERT OR IGNORE INTO file_entries VALUES(?,?,?,?)",
                [(version, e["path"], encode(e), file_policy(e)) for e in entries],
            )
        return version

    def revision(self, version):
        with self.connect() as db:
            row = db.execute(
                "SELECT id,repository_id,name,commit_id,tree_id,catalog_digest,ref,generation FROM revisions WHERE id=?",
                (version,),
            ).fetchone()
        if row is None:
            raise GitHubError("unknown_source_version")
        return dict(
            zip(
                (
                    "source_version_id",
                    "repository_id",
                    "name",
                    "commit",
                    "tree",
                    "catalog_digest",
                    "ref",
                    "index_generation",
                ),
                row,
                strict=True,
            )
        )

    def versions(self):
        with self.connect() as db:
            ids = [
                r[0]
                for r in db.execute("SELECT id FROM revisions ORDER BY name,commit_id")
            ]
        return [self.revision(version) for version in ids]

    def entries(self, version):
        with self.connect() as db:
            return [
                json.loads(row[0])
                | {
                    "policy": row[1],
                    "parse_state": row[2] or "not_fetched",
                    "fetch_state": row[3]
                    or (
                        "cached"
                        if row[2] in {"cached", "parsed", "text_only"}
                        else row[2] or "not_fetched"
                    ),
                    "fetch_error": row[4],
                }
                for row in db.execute(
                    "SELECT e.entry,e.policy,m.parse_state,f.state,f.error FROM file_entries e LEFT JOIN materializations m ON e.revision=m.revision AND e.path=m.path LEFT JOIN fetch_states f ON f.revision=e.revision AND f.path=e.path WHERE e.revision=? ORDER BY e.path",
                    (version,),
                )
            ]

    def entry(self, version, path):
        safe_relative(path)
        with self.connect() as db:
            row = db.execute(
                "SELECT entry,policy FROM file_entries WHERE revision=? AND path=?",
                (version, path),
            ).fetchone()
        if row is None or row[1] != "allowed":
            raise GitHubError("file_outside_readable_catalog")
        return json.loads(row[0])

    def begin_run(self, owner, run_id):
        with self.connect() as db:
            prior = db.execute(
                "SELECT run_id FROM runs WHERE owner=?", (owner,)
            ).fetchone()
            if prior and prior != (run_id,):
                raise GitHubError("turn_run_conflict")
            db.execute(
                "INSERT OR IGNORE INTO runs VALUES(?,?,'inflight')", (owner, run_id)
            )

    def _gc(self, db, needed, exclude=None):
        used = db.execute(
            "SELECT (SELECT coalesce(sum(size),0) FROM content_objects)+(SELECT coalesce(sum(size),0) FROM text_views)"
        ).fetchone()[0]
        reserved = db.execute(
            "SELECT coalesce(sum(size),0) FROM reservations"
        ).fetchone()[0]
        reclaimed = 0
        rows = db.execute(
            """SELECT c.digest,c.size+coalesce((SELECT sum(v.size) FROM text_views v WHERE v.raw_digest=c.digest),0) FROM content_objects c WHERE c.digest!=coalesce(?, '') AND NOT EXISTS(
            SELECT 1 FROM materializations m JOIN pins p ON p.revision=m.revision AND p.path=m.path
            WHERE m.digest=c.digest) ORDER BY c.touched,c.digest""",
            (exclude,),
        ).fetchall()
        for digest, size in rows:
            if used + reserved + needed <= self.capacity:
                break
            versions = db.execute(
                "SELECT DISTINCT revision FROM materializations WHERE digest=?",
                (digest,),
            ).fetchall()
            db.execute(
                "DELETE FROM parsed_versions WHERE (revision,path) IN (SELECT revision,path FROM materializations WHERE digest=?)",
                (digest,),
            )
            db.execute(
                "UPDATE fetch_states SET state='reclaimed',error=NULL WHERE (revision,path) IN (SELECT revision,path FROM materializations WHERE digest=?)",
                (digest,),
            )
            db.execute(
                "UPDATE materializations SET digest=NULL,parse_state='reclaimed',reason='',symbols='[]' WHERE digest=?",
                (digest,),
            )
            db.execute(
                "DELETE FROM file_text_views WHERE view_id IN (SELECT id FROM text_views WHERE raw_digest=?)",
                (digest,),
            )
            db.execute("DELETE FROM text_views WHERE raw_digest=?", (digest,))
            db.execute("DELETE FROM content_objects WHERE digest=?", (digest,))
            db.executemany(
                "UPDATE revisions SET generation=generation+1 WHERE id=?", versions
            )
            reclaimed += size
            used -= size
        if used + reserved + needed > self.capacity:
            raise GitHubError("cache_full_with_protected_content")
        return reclaimed

    def guard_space(self, size):
        physical = sum(
            p.stat().st_size for p in self.directory.iterdir() if p.is_file()
        )
        if physical + size * 4 + 65536 > self.disk_limit:
            raise GitHubError("repository_disk_limit")

    def reserve(self, owner, size):
        self.guard_space(size)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM reservations WHERE owner=?", (owner,))
            self._gc(db, size)
            db.execute("INSERT INTO reservations VALUES(?,?)", (owner, size))

    def release_reservation(self, owner):
        with self.connect() as db:
            db.execute("DELETE FROM reservations WHERE owner=?", (owner,))

    def cached(self, version, path, owner=None, *, reuse=False, pin_state="inflight"):
        entry = self.entry(version, path)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT c.digest,c.data FROM materializations m JOIN content_objects c ON c.digest=m.digest WHERE m.revision=? AND m.path=?",
                (version, path),
            ).fetchone()
            if row is None and reuse:
                row = db.execute(
                    "SELECT digest,data FROM content_objects WHERE git_blob=? AND size=?",
                    (entry["sha"], entry["size"]),
                ).fetchone()
                if row:
                    # 跨版本复用只发布正文；解析在锁外完成后单独登记。
                    db.execute(
                        "INSERT OR REPLACE INTO materializations VALUES(?,?,?,'cached','','[]')",
                        (version, path, row[0]),
                    )
                    db.execute(
                        "UPDATE revisions SET generation=generation+1 WHERE id=?",
                        (version,),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO fetch_states VALUES(?,?,'cached',NULL)",
                        (version, path),
                    )
            if row is None:
                return None
            if (
                hashlib.sha256(row[1]).hexdigest() != row[0]
                or verify_blob(row[1], entry) != row[0]
            ):
                raise GitHubError("cached_object_integrity_failed")
            if owner:
                db.execute(
                    "INSERT OR IGNORE INTO pins VALUES(?,?,?,?)",
                    (owner, version, path, pin_state),
                )
            db.execute(
                "UPDATE content_objects SET touched=touched+1 WHERE digest=?", (row[0],)
            )
            return bytes(row[1])

    def put(self, version, path, data, owner):
        entry = self.entry(version, path)
        digest = verify_blob(data, entry)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute(
                "SELECT size FROM reservations WHERE owner=?", (owner,)
            ).fetchone()
            if reservation != (len(data),):
                raise GitHubError("cache_reservation_mismatch")
            db.execute(
                "INSERT OR IGNORE INTO content_objects(digest,git_blob,size,data) VALUES(?,?,?,?)",
                (digest, entry["sha"], len(data), data),
            )
            db.execute(
                "INSERT OR REPLACE INTO materializations VALUES(?,?,?,'cached','','[]')",
                (version, path, digest),
            )
            db.execute(
                "INSERT OR IGNORE INTO pins VALUES(?,?,?,'inflight')",
                (owner, version, path),
            )
            db.execute("DELETE FROM reservations WHERE owner=?", (owner,))
            db.execute(
                "UPDATE revisions SET generation=generation+1 WHERE id=?", (version,)
            )
            db.execute(
                "INSERT OR REPLACE INTO fetch_states VALUES(?,?,'cached',NULL)",
                (version, path),
            )
        return data

    def index(self, version, path, data):
        decoded = self.text_view(version, path, data)
        parse_input = decoded.utf8
        suffix, state, symbols, reason = (
            Path(path).suffix,
            "text_only",
            [],
            "text_only_language",
        )
        if suffix in {".java", ".ts", ".js"}:
            result = (
                parse_java(parse_input, path)
                if suffix == ".java"
                else parse_frontend(parse_input, path, LANGUAGES[suffix])
            )
            if result.parse_status == "parsed":
                state, symbols, reason = (
                    "parsed",
                    [s.model_dump() for s in result.symbols],
                    "",
                )
            else:
                reason = "syntax_error"
        with self.connect() as db:
            row = db.execute(
                "SELECT c.digest FROM materializations m JOIN content_objects c ON c.digest=m.digest WHERE m.revision=? AND m.path=?",
                (version, path),
            ).fetchone()
            if row is None or row[0] != hashlib.sha256(data).hexdigest():
                raise GitHubError("index_content_changed")
            db.execute(
                "UPDATE materializations SET parse_state=?,reason=?,symbols=? WHERE revision=? AND path=?",
                (state, reason, encode(symbols), version, path),
            )
            db.execute(
                "INSERT OR REPLACE INTO parsed_versions VALUES(?,?,?)",
                (version, path, encode(parser_versions())),
            )
            db.execute(
                "UPDATE revisions SET generation=generation+1 WHERE id=?", (version,)
            )

    def mark_fetch(self, version, path, state, error=None):
        self.entry(version, path)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO fetch_states VALUES(?,?,?,?)",
                (version, path, state, error),
            )

    def text_profile(self, version):
        with self.connect() as db:
            row = db.execute(
                "SELECT profile FROM text_profiles WHERE revision=?", (version,)
            ).fetchone()
        if row is None:
            raise GitHubError("missing_text_profile")
        return profile(json.loads(row[0]))

    def text_decoder(self, version):
        with self.connect() as db:
            row = db.execute(
                "SELECT decoder FROM text_profiles WHERE revision=?", (version,)
            ).fetchone()
        if row is None or row[0] != DECODER_VERSION:
            raise GitHubError("unsupported_decoder_version")
        return row[0]

    def reinterpret(self, version, overrides):
        revision = self.revision(version)
        selected = self.text_profile(version)
        selected["overrides"].update(overrides)
        with self.connect() as db:
            entries = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT entry FROM file_entries WHERE revision=? ORDER BY path",
                    (version,),
                )
            ]
        # Catalog order is identity-bearing: registration always uses its verified path order.
        return self.register(
            {"id": revision["repository_id"], "full_name": revision["name"]},
            revision["commit"],
            revision["tree"],
            entries,
            revision["ref"],
            text_profile=selected,
        )

    def release_failed_file(self, owner, version, path, code):
        with self.connect() as db:
            db.execute(
                "UPDATE materializations SET parse_state='decode_failed',reason=?,symbols='[]' WHERE revision=? AND path=?",
                (code, version, path),
            )
            db.execute(
                "DELETE FROM pins WHERE owner=? AND revision=? AND path=? AND state='inflight'",
                (owner, version, path),
            )
            db.execute(
                "DELETE FROM parsed_versions WHERE revision=? AND path=?",
                (version, path),
            )

    def text_view(self, version, path, raw):
        digest = verify_blob(raw, self.entry(version, path))
        selected = self.text_profile(version)
        with self.connect() as db:
            fixed_decoder = db.execute(
                "SELECT decoder FROM text_profiles WHERE revision=?", (version,)
            ).fetchone()[0]
            row = db.execute(
                "SELECT v.id,v.raw_digest,v.encoding,v.decoder,v.data,v.digest,v.size FROM file_text_views f JOIN text_views v ON f.view_id=v.id WHERE f.revision=? AND f.path=?",
                (version, path),
            ).fetchone()
        if row:
            identifier, raw_digest, encoding, decoder, data, text_digest, size = row
            expected = hashlib.sha256(
                encode([digest, selected, encoding, decoder, text_digest]).encode()
            ).hexdigest()
            if (
                raw_digest != digest
                or decoder != fixed_decoder
                or identifier != expected
                or len(data) != size
                or hashlib.sha256(data).hexdigest() != text_digest
            ):
                raise GitHubError("text_view_integrity_failed")
            try:
                return DecodedText(data.decode("utf-8", errors="strict"), encoding)
            except UnicodeError:
                raise GitHubError("text_view_integrity_failed") from None
        if fixed_decoder not in SUPPORTED_DECODERS:
            raise GitHubError("unsupported_decoder_version")
        decoded = decode_text(
            raw, selected["overrides"].get(path, "auto"), decoder=fixed_decoder
        )
        identifier = hashlib.sha256(
            encode(
                [digest, selected, decoded.encoding, fixed_decoder, decoded.digest]
            ).encode()
        ).hexdigest()
        self.guard_space(len(decoded.utf8))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT digest FROM materializations WHERE revision=? AND path=?",
                (version, path),
            ).fetchone() != (digest,):
                raise GitHubError("index_content_changed")
            if not db.execute(
                "SELECT 1 FROM text_views WHERE id=?", (identifier,)
            ).fetchone():
                self._gc(db, len(decoded.utf8), exclude=digest)
                db.execute(
                    "INSERT INTO text_views VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        digest,
                        decoded.encoding,
                        fixed_decoder,
                        decoded.utf8,
                        decoded.digest,
                        len(decoded.utf8),
                    ),
                )
            db.execute(
                "INSERT OR IGNORE INTO file_text_views VALUES(?,?,?)",
                (version, path, identifier),
            )
        return decoded

    def indexed(self, version):
        with self.connect() as db:
            rows = db.execute(
                "SELECT m.path,m.parse_state,m.symbols FROM materializations m JOIN parsed_versions p ON p.revision=m.revision AND p.path=m.path WHERE m.revision=? AND p.version=? AND m.parse_state IN ('parsed','text_only') ORDER BY m.path",
                (version, encode(parser_versions())),
            ).fetchall()
        return [(path, state, json.loads(symbols)) for path, state, symbols in rows]

    def publication_pending(self, owner):
        with self.connect() as db:
            db.execute(
                "UPDATE pins SET state='publication_pending' WHERE owner=?", (owner,)
            )
            db.execute(
                "UPDATE runs SET state='publication_pending' WHERE owner=?", (owner,)
            )

    def keep_version(self, owner, version):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO version_pins VALUES(?,?)", (owner, version)
            )

    def finish_run(self, owner, *, published):
        with self.connect() as db:
            if published:
                db.execute("UPDATE pins SET state='published' WHERE owner=?", (owner,))
            else:
                db.execute("DELETE FROM pins WHERE owner=?", (owner,))
            db.execute("DELETE FROM reservations WHERE owner=?", (owner,))
            db.execute(
                "UPDATE runs SET state=? WHERE owner=?",
                ("published" if published else "finished", owner),
            )

    def remove_version(self, version):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM pins WHERE revision=? UNION ALL SELECT 1 FROM version_pins WHERE revision=? LIMIT 1",
                (version, version),
            ).fetchone():
                raise GitHubError("source_version_in_use")
            db.execute("DELETE FROM file_text_views WHERE revision=?", (version,))
            db.execute("DELETE FROM text_profiles WHERE revision=?", (version,))
            db.execute("DELETE FROM materializations WHERE revision=?", (version,))
            db.execute("DELETE FROM parsed_versions WHERE revision=?", (version,))
            db.execute("DELETE FROM fetch_states WHERE revision=?", (version,))
            db.execute("DELETE FROM file_entries WHERE revision=?", (version,))
            db.execute("DELETE FROM revisions WHERE id=?", (version,))

    def stats(self):
        with self.connect() as db:
            used = db.execute(
                "SELECT (SELECT coalesce(sum(size),0) FROM content_objects)+(SELECT coalesce(sum(size),0) FROM text_views)"
            ).fetchone()[0]
            protected = db.execute("""SELECT coalesce(sum(c.size+coalesce((SELECT sum(v.size) FROM text_views v WHERE v.raw_digest=c.digest),0)),0) FROM content_objects c WHERE EXISTS(
                SELECT 1 FROM materializations m JOIN pins p ON p.revision=m.revision AND p.path=m.path WHERE m.digest=c.digest)""").fetchone()[
                0
            ]
            reserved = db.execute(
                "SELECT coalesce(sum(size),0) FROM reservations"
            ).fetchone()[0]
        return {
            "content_bytes": used,
            "protected_bytes": protected,
            "reclaimable_bytes": used - protected,
            "reserved_bytes": reserved,
            "capacity_bytes": self.capacity,
            "physical_bytes": sum(
                p.stat().st_size for p in self.directory.iterdir() if p.is_file()
            ),
            "disk_limit_bytes": self.disk_limit,
        }
