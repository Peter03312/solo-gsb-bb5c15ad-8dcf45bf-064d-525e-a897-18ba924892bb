"""业务逻辑。所有函数都在调用方持有的事务（db.tx）内执行。"""

import json
from datetime import datetime, timezone

from fastapi import HTTPException


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_allergens(values) -> list:
    """去空白、去重、排序，保证集合比较与存储稳定。"""
    return sorted({v.strip() for v in values if v and v.strip()})


# ---------------------------------------------------------------- 原料

def create_ingredient(conn, payload):
    if conn.execute("SELECT 1 FROM ingredients WHERE id=?", (payload.id,)).fetchone():
        raise HTTPException(409, f"ingredient {payload.id!r} already exists")
    allergens = normalize_allergens(payload.allergens)
    ts = utcnow()
    conn.execute(
        "INSERT INTO ingredients(id, name, current_version, created_at) VALUES (?,?,1,?)",
        (payload.id, payload.name, ts),
    )
    conn.execute(
        "INSERT INTO ingredient_versions(ingredient_id, version, allergens, created_at)"
        " VALUES (?,?,?,?)",
        (payload.id, 1, json.dumps(allergens), ts),
    )
    return {"id": payload.id, "name": payload.name, "version": 1, "allergens": allergens}


def correct_ingredient(conn, ingredient_id, payload):
    row = conn.execute("SELECT * FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"ingredient {ingredient_id!r} not found")
    current = row["current_version"]
    if payload.expected_version != current:
        # 旧版本拒绝：不写入任何内容，历史版本保留
        raise HTTPException(
            409,
            f"version conflict: expected_version={payload.expected_version}, "
            f"but current version of {ingredient_id!r} is {current}; "
            f"correction rejected, history preserved",
        )
    new_version = current + 1
    allergens = normalize_allergens(payload.allergens)
    conn.execute(
        "INSERT INTO ingredient_versions(ingredient_id, version, allergens, created_at)"
        " VALUES (?,?,?,?)",
        (ingredient_id, new_version, json.dumps(allergens), utcnow()),
    )
    conn.execute(
        "UPDATE ingredients SET current_version=? WHERE id=?",
        (new_version, ingredient_id),
    )
    return {
        "id": ingredient_id,
        "name": row["name"],
        "version": new_version,
        "allergens": allergens,
    }


def get_ingredient(conn, ingredient_id):
    row = conn.execute(
        "SELECT i.id, i.name, i.current_version, v.allergens, i.created_at"
        " FROM ingredients i"
        " JOIN ingredient_versions v"
        "   ON v.ingredient_id = i.id AND v.version = i.current_version"
        " WHERE i.id=?",
        (ingredient_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"ingredient {ingredient_id!r} not found")
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["current_version"],
        "allergens": json.loads(row["allergens"]),
        "created_at": row["created_at"],
    }


def list_ingredients(conn):
    rows = conn.execute(
        "SELECT i.id, i.name, i.current_version, v.allergens"
        " FROM ingredients i"
        " JOIN ingredient_versions v"
        "   ON v.ingredient_id = i.id AND v.version = i.current_version"
        " ORDER BY i.id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "version": r["current_version"],
            "allergens": json.loads(r["allergens"]),
        }
        for r in rows
    ]


def list_ingredient_versions(conn, ingredient_id):
    if not conn.execute("SELECT 1 FROM ingredients WHERE id=?", (ingredient_id,)).fetchone():
        raise HTTPException(404, f"ingredient {ingredient_id!r} not found")
    rows = conn.execute(
        "SELECT version, allergens, created_at FROM ingredient_versions"
        " WHERE ingredient_id=? ORDER BY version",
        (ingredient_id,),
    ).fetchall()
    return {
        "id": ingredient_id,
        "versions": [
            {
                "version": r["version"],
                "allergens": json.loads(r["allergens"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 批次

def _reachable(conn, start_ids, target) -> bool:
    """从 start_ids 沿批次来源边出发能否到达 target（用于循环检测）。"""
    stack, seen = list(start_ids), set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        rows = conn.execute(
            "SELECT source_id FROM batch_sources"
            " WHERE batch_id=? AND source_type='batch'",
            (cur,),
        ).fetchall()
        stack.extend(r["source_id"] for r in rows)
    return False


def _validate_sources(conn, batch_id, sources):
    if not sources:
        raise HTTPException(422, "batch sources must not be empty")
    for s in sources:
        if s.type == "ingredient":
            if not conn.execute(
                "SELECT 1 FROM ingredients WHERE id=?", (s.id,)
            ).fetchone():
                raise HTTPException(422, f"unknown ingredient source: {s.id!r}")
        else:
            if s.id == batch_id:
                raise HTTPException(
                    422, f"circular source: batch {batch_id!r} references itself"
                )
            if not conn.execute("SELECT 1 FROM batches WHERE id=?", (s.id,)).fetchone():
                raise HTTPException(422, f"unknown batch source: {s.id!r}")
    batch_refs = [s.id for s in sources if s.type == "batch"]
    if _reachable(conn, batch_refs, batch_id):
        raise HTTPException(
            422, f"circular source reference detected for batch {batch_id!r}"
        )


def _batch_view(conn, row):
    sources = conn.execute(
        "SELECT source_type, source_id FROM batch_sources WHERE batch_id=? ORDER BY seq",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "product": row["product"],
        "status": row["status"],
        "created_at": row["created_at"],
        "sources": [{"type": s["source_type"], "id": s["source_id"]} for s in sources],
    }


def create_batch(conn, payload):
    if conn.execute("SELECT 1 FROM batches WHERE id=?", (payload.id,)).fetchone():
        raise HTTPException(409, f"batch {payload.id!r} already exists")
    _validate_sources(conn, payload.id, payload.sources)
    conn.execute(
        "INSERT INTO batches(id, product, status, created_at) VALUES (?,?, 'draft', ?)",
        (payload.id, payload.product, utcnow()),
    )
    for seq, s in enumerate(payload.sources):
        conn.execute(
            "INSERT INTO batch_sources(batch_id, seq, source_type, source_id)"
            " VALUES (?,?,?,?)",
            (payload.id, seq, s.type, s.id),
        )
    row = conn.execute("SELECT * FROM batches WHERE id=?", (payload.id,)).fetchone()
    return _batch_view(conn, row)


def get_batch(conn, batch_id):
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"batch {batch_id!r} not found")
    return _batch_view(conn, row)


def update_sources(conn, batch_id, payload):
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"batch {batch_id!r} not found")
    if row["status"] == "finalized":
        raise HTTPException(
            409, f"batch {batch_id!r} is finalized; sources are immutable"
        )
    _validate_sources(conn, batch_id, payload.sources)
    conn.execute("DELETE FROM batch_sources WHERE batch_id=?", (batch_id,))
    for seq, s in enumerate(payload.sources):
        conn.execute(
            "INSERT INTO batch_sources(batch_id, seq, source_type, source_id)"
            " VALUES (?,?,?,?)",
            (batch_id, seq, s.type, s.id),
        )
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    return _batch_view(conn, row)


# ---------------------------------------------------------------- 声明计算

def compute_declaration(conn, batch_id):
    """按全部可达原料的当前版本计算过敏原并集、来源路径与来源版本。

    重复来源只计一次：过敏原集合天然去重，路径按字符串去重，
    source_versions 中每个原料只记录一次。
    """
    paths = {}      # allergen -> set(path)
    versions = {}   # ingredient_id -> version

    def visit(bid, trail):
        rows = conn.execute(
            "SELECT source_type, source_id FROM batch_sources"
            " WHERE batch_id=? ORDER BY seq",
            (bid,),
        ).fetchall()
        for row in rows:
            sid = row["source_id"]
            if row["source_type"] == "ingredient":
                ing = conn.execute(
                    "SELECT v.version, v.allergens FROM ingredients i"
                    " JOIN ingredient_versions v"
                    "   ON v.ingredient_id = i.id AND v.version = i.current_version"
                    " WHERE i.id=?",
                    (sid,),
                ).fetchone()
                if ing is None:  # 写入时已校验，防御性检查
                    raise HTTPException(500, f"unknown ingredient source: {sid!r}")
                versions[sid] = ing["version"]
                path = " -> ".join(trail + [sid])
                for a in json.loads(ing["allergens"]):
                    paths.setdefault(a, set()).add(path)
            else:
                if sid in trail:  # 写入时已做循环校验，防御性检查
                    raise HTTPException(500, f"cycle detected at batch {sid!r}")
                visit(sid, trail + [sid])

    visit(batch_id, [batch_id])
    return {
        "allergens": sorted(paths),
        "paths": {a: sorted(ps) for a, ps in sorted(paths.items())},
        "source_versions": dict(sorted(versions.items())),
    }


def finalize_batch(conn, batch_id):
    """定版：在同一事务内原子保存声明集合、来源版本和路径。"""
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"batch {batch_id!r} not found")
    if row["status"] == "finalized":
        raise HTTPException(409, f"batch {batch_id!r} is already finalized")
    decl = compute_declaration(conn, batch_id)
    ts = utcnow()
    conn.execute(
        "INSERT INTO finalizations(batch_id, allergens, source_versions, paths,"
        " finalized_at) VALUES (?,?,?,?,?)",
        (
            batch_id,
            json.dumps(decl["allergens"]),
            json.dumps(decl["source_versions"]),
            json.dumps(decl["paths"]),
            ts,
        ),
    )
    conn.execute("UPDATE batches SET status='finalized' WHERE id=?", (batch_id,))
    return {
        "batch_id": batch_id,
        "status": "finalized",
        "finalized_at": ts,
        "declaration": decl,
    }


def get_declaration(conn, batch_id):
    """未定版：返回现算声明。已定版：返回定版快照 + 现算集合 + 差异。

    上游更正后，现算集合与定版集合不同时标记为 pending_review；
    下游批次同样按当前证据（原料当前版本）重算。
    """
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"batch {batch_id!r} not found")
    current = compute_declaration(conn, batch_id)
    if row["status"] == "draft":
        return {"batch_id": batch_id, "status": "draft", "declaration": current}

    fin = conn.execute(
        "SELECT * FROM finalizations WHERE batch_id=?", (batch_id,)
    ).fetchone()
    finalized_allergens = json.loads(fin["allergens"])
    added = sorted(set(current["allergens"]) - set(finalized_allergens))
    removed = sorted(set(finalized_allergens) - set(current["allergens"]))
    return {
        "batch_id": batch_id,
        "status": "finalized",
        "review_status": "pending_review" if (added or removed) else "in_sync",
        "finalized": {
            "finalized_at": fin["finalized_at"],
            "allergens": finalized_allergens,
            "source_versions": json.loads(fin["source_versions"]),
            "paths": json.loads(fin["paths"]),
        },
        "current": current,
        "diff": {"added": added, "removed": removed},
    }
