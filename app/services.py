"""业务逻辑。所有函数都在调用方持有的事务（db.tx）内执行。"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from itertools import combinations

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

def _direct_sources(conn, batch_id):
    """读取批次当前的有序直接来源，返回 [(type, id), ...]。"""
    rows = conn.execute(
        "SELECT source_type, source_id FROM batch_sources"
        " WHERE batch_id=? ORDER BY seq",
        (batch_id,),
    ).fetchall()
    return [(r["source_type"], r["source_id"]) for r in rows]


def compute_declaration(conn, batch_id):
    """按全部可达原料的当前版本计算过敏原并集、来源路径与来源版本。

    重复来源只计一次：过敏原集合天然去重，路径按字符串去重，
    source_versions 中每个原料只记录一次。
    """
    return _compute(conn, batch_id)["declaration"]


def _compute(conn, batch_id, override_sources=None):
    """声明计算内核。

    override_sources：[(type, id), ...]，提供时以其作为 batch_id 的直接来源
    （替换预演使用，不读库、不写库）；其余批次仍读库中的现有来源。

    返回 declaration（allergens / paths / source_versions）以及计算过程中
    实际读取的证据 edges（[(batch_id, seq, type, id), ...]）。
    """
    paths = {}      # allergen -> set(path)
    versions = {}   # ingredient_id -> version
    edges = []      # 计算所依赖的来源关系（含目标批次与每个进入的子批次）
    entered = set()  # 已展开过的批次，重复进入只展开一次（边仍按实际遍历记录）

    def visit(bid, trail, sources):
        if bid in entered:
            return
        entered.add(bid)
        for seq, (stype, sid) in enumerate(sources):
            edges.append((bid, seq, stype, sid))
            if stype == "ingredient":
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
                if sid in trail:  # 写入/预演时已做循环校验，防御性检查
                    raise HTTPException(500, f"cycle detected at batch {sid!r}")
                visit(sid, trail + [sid], _direct_sources(conn, sid))

    root_sources = (
        list(override_sources)
        if override_sources is not None
        else _direct_sources(conn, batch_id)
    )
    visit(batch_id, [batch_id], root_sources)
    declaration = {
        "allergens": sorted(paths),
        "paths": {a: sorted(ps) for a, ps in sorted(paths.items())},
        "source_versions": dict(sorted(versions.items())),
    }
    return {"declaration": declaration, "edges": edges}


def _plan_fingerprint(conn, batch_id, sources):
    """对某一组直接来源计算声明所依赖证据的指纹（SHA-256）。

    覆盖：目标批次状态、目标批次替换前直接来源（预演快照基准）、计算路径上
    每个批次的全部来源边，以及全部可达原料的当前版本。任意参与预演的来源
    关系或原料版本变化都会令指纹改变，从而使过期依据失效。
    """
    batch_row = conn.execute(
        "SELECT status FROM batches WHERE id=?", (batch_id,)
    ).fetchone()
    result = _compute(conn, batch_id, override_sources=sources)
    payload = {
        "batch_id": batch_id,
        "status": batch_row["status"] if batch_row else None,
        "current_sources": _direct_sources(conn, batch_id),
        "edges": result["edges"],
        "source_versions": result["declaration"]["source_versions"],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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


# ---------------------------------------------------------------- 替换预演 / 采纳

def _load_draft_batch(conn, batch_id):
    """读取批次并校验其存在且未定版。"""
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"batch {batch_id!r} not found")
    if row["status"] == "finalized":
        raise HTTPException(
            409, f"batch {batch_id!r} is finalized; replacement preview is not allowed"
        )
    return row


def _validate_replacement_specs(conn, batch_id, current, replacements):
    """校验替换指令并返回 {seq: 去重排序后的候选 (type, id) 列表}。

    明确报错（422）：空替换指令、重复序号、非法序号（越界）、空候选、
    候选来源不存在、候选会引入循环（自引用或可达回目标批次）。
    """
    if not replacements:
        raise HTTPException(422, "replacements must not be empty")
    seqs = [spec.seq for spec in replacements]
    if len(seqs) != len(set(seqs)):
        dupes = sorted({s for s in seqs if seqs.count(s) > 1})
        raise HTTPException(422, f"duplicate source positions in replacements: {dupes}")

    current_by_seq = {i: ref for i, ref in enumerate(current)}
    specs = {}
    for spec in replacements:
        if spec.seq not in current_by_seq:
            raise HTTPException(
                422,
                f"invalid source seq {spec.seq}: batch {batch_id!r} has"
                f" {len(current)} direct sources (valid seq: 0..{len(current) - 1})",
            )
        if not spec.candidates:
            raise HTTPException(422, f"candidates for seq {spec.seq} must not be empty")
        # 候选去重（按类型+ID）并排序，保证并列时的字典序确定
        uniq = sorted({(c.type, c.id) for c in spec.candidates})
        for ctype, cid in uniq:
            if ctype == "ingredient":
                if not conn.execute(
                    "SELECT 1 FROM ingredients WHERE id=?", (cid,)
                ).fetchone():
                    raise HTTPException(
                        422, f"unknown ingredient candidate at seq {spec.seq}: {cid!r}"
                    )
            else:
                if cid == batch_id:
                    raise HTTPException(
                        422,
                        f"candidate at seq {spec.seq} introduces a cycle:"
                        f" batch {batch_id!r} would reference itself",
                    )
                if not conn.execute(
                    "SELECT 1 FROM batches WHERE id=?", (cid,)
                ).fetchone():
                    raise HTTPException(
                        422, f"unknown batch candidate at seq {spec.seq}: {cid!r}"
                    )
                # 候选批次沿其现有来源能否回到目标批次？此性质只依赖未被修改的
                # 子图，与其他位置的替换选择无关，故可独立判定。
                if _reachable(conn, [cid], batch_id):
                    raise HTTPException(
                        422,
                        f"candidate batch {cid!r} at seq {spec.seq} introduces a"
                        f" circular reference back to batch {batch_id!r}",
                    )
        specs[spec.seq] = uniq
    return specs


def _preview_plan_body(batch_id, sources, decl, changed_seqs, forbidden):
    """构造预演/采纳成功响应中的方案主体。"""
    return {
        "batch_id": batch_id,
        "replaced_positions": changed_seqs,
        "replacement_count": len(changed_seqs),
        "forbidden_allergens": forbidden,
        "sources": [{"type": t, "id": i} for t, i in sources],
        "declaration": decl,
    }


def preview_replacement(conn, batch_id, payload):
    """替换预演：不改动批次，寻找替换位置数最少的可行方案。

    可行性：候选来源存在、不引入循环（已在前置校验中硬性拒绝非法候选），
    且替换后的过敏原声明不含任何禁止过敏原。
    并列时按 (来源序号, 候选类型, 候选 ID) 字典序选首个；
    原配方已合格（无需替换）时返回零替换方案。
    """
    _load_draft_batch(conn, batch_id)
    forbidden = set(normalize_allergens(payload.forbidden_allergens))
    current = _direct_sources(conn, batch_id)

    if not forbidden:
        # 无禁止过敏原时，零替换恒为最优且唯一选择（候选不会改善任何约束），
        # 无需校验替换指令。
        specs = {}
    else:
        # 携带禁过敏原却不给任何替换位置，等同于空候选，明确报错
        if not payload.replacements:
            raise HTTPException(422, "replacements must not be empty")
        specs = _validate_replacement_specs(
            conn, batch_id, current, payload.replacements
        )

    # 每个可选位置：候选与现状相同的选项剔除（它等价于"不替换"），
    # 真正的替换选项按 (类型, ID) 排序。
    choices = {}
    for seq, cands in specs.items():
        opts = [c for c in cands if c != current[seq]]
        if opts:
            choices[seq] = opts

    best = None  # (key, sources, decl)；key 为同层方案的字典序比较键
    ordered_seqs = sorted(choices)
    target_snapshot = list(current)

    def consider(sources, changed):
        """计算给定来源的声明，可行则按字典序竞争本层最优方案。"""
        nonlocal best
        decl = _compute(conn, batch_id, override_sources=sources)["declaration"]
        if forbidden.intersection(decl["allergens"]):
            return
        key = tuple((seq, sources[seq][0], sources[seq][1]) for seq in changed)
        if best is None or key < best[0]:
            best = (key, list(sources), decl)

    # 零替换（原配方）恒参与竞争；已合格时零位置方案即全局最优
    consider(target_snapshot, [])
    if best is not None:
        picked = best
    else:
        # 按替换位置数 k=1..n 逐层枚举；第一个出现可行方案的层即最小替换数，
        # 该层内以 (序号, 候选类型, 候选 ID) 字典序取首，无需进入更大的 k。
        def walk(combo, i, sources, changed):
            if i == len(combo):
                consider(sources, changed)
                return
            seq = combo[i]
            for ref in choices[seq]:  # 已按 (类型, ID) 排序
                nxt = list(sources)
                nxt[seq] = ref
                walk(combo, i + 1, nxt, changed + [seq])

        picked = None
        for k in range(1, len(ordered_seqs) + 1):
            for combo in combinations(ordered_seqs, k):
                walk(combo, 0, target_snapshot, [])
            if best is not None:
                picked = best
                break

    if picked is None:
        return {
            "batch_id": batch_id,
            "feasible": False,
            "forbidden_allergens": sorted(forbidden),
            "message": "no feasible replacement combination avoids all forbidden"
                       " allergens",
        }

    _, sources, decl = picked
    changed_seqs = [
        seq for seq in range(len(sources)) if sources[seq] != target_snapshot[seq]
    ]
    forbidden_sorted = sorted(forbidden)
    basis = {
        "batch_id": batch_id,
        "forbidden_allergens": forbidden_sorted,
        "fingerprint": _plan_fingerprint(conn, batch_id, sources),
    }
    body = _preview_plan_body(
        batch_id, sources, decl, changed_seqs, forbidden_sorted
    )
    token = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO replace_previews(token, batch_id, basis, plan, created_at)"
        " VALUES (?,?,?,?,?)",
        (
            token,
            batch_id,
            json.dumps(basis, ensure_ascii=False),
            json.dumps(body, ensure_ascii=False),
            utcnow(),
        ),
    )
    return {
        "feasible": True,
        **body,
        "basis": {"preview_token": token, **basis},
    }


def adopt_replacement(conn, batch_id, payload):
    """采纳预演方案：携依据在同一事务内确认证据未变化后整体替换直接来源。

    过期（目标批次状态变化、直接来源变化、参与预演的任一来源边或原料版本
    变化）一律 409 拒绝，批次保持不变。依据一次性有效。
    """
    _load_draft_batch(conn, batch_id)
    row = conn.execute(
        "SELECT * FROM replace_previews WHERE token=?", (payload.preview_token,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown or expired preview_token")
    if row["batch_id"] != batch_id:
        raise HTTPException(
            422,
            f"preview_token belongs to batch {row['batch_id']!r},"
            f" not {batch_id!r}",
        )

    basis = json.loads(row["basis"])
    plan = json.loads(row["plan"])
    sources = [(s["type"], s["id"]) for s in plan["sources"]]

    # 同一事务内重新计算指纹：状态/来源关系/原料版本任一变化都会失配
    if _plan_fingerprint(conn, batch_id, sources) != basis["fingerprint"]:
        raise HTTPException(
            409,
            "preview basis is stale: the batch, its source relations or ingredient"
            " versions changed since the preview; adoption rejected, batch unchanged",
        )

    # 证据未变化：整体替换直接来源（删除旧边 -> 写入新边）
    conn.execute("DELETE FROM batch_sources WHERE batch_id=?", (batch_id,))
    for seq, (stype, sid) in enumerate(sources):
        conn.execute(
            "INSERT INTO batch_sources(batch_id, seq, source_type, source_id)"
            " VALUES (?,?,?,?)",
            (batch_id, seq, stype, sid),
        )
    # 依据一次性消费
    conn.execute("DELETE FROM replace_previews WHERE token=?", (payload.preview_token,))

    current_view = _batch_view(
        conn,
        conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone(),
    )
    return {
        "adopted": True,
        "basis": {"preview_token": payload.preview_token, **basis},
        **plan,
        "batch": current_view,
    }
