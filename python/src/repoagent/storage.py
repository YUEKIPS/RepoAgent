"""当前数据契约的入口保护；只初始化空库，不升级或解释旧库。"""

import sqlite3
from contextlib import closing
from pathlib import Path

DATA_VERSION = "repoagent-pool-20261108"


def guard_database(path, kind):
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        raise ValueError("Linked database rejected")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            if "product_meta" not in tables or db.execute(
                "SELECT version,kind FROM product_meta"
            ).fetchall() != [(DATA_VERSION, kind)]:
                raise ValueError("Unsupported data version; use its frozen release")
        else:
            db.execute("CREATE TABLE product_meta(version TEXT NOT NULL,kind TEXT NOT NULL)")
            db.execute("INSERT INTO product_meta VALUES(?,?)", (DATA_VERSION, kind))
