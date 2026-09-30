import os
import tempfile

# 必须在导入 app 之前设置，使测试使用独立数据库文件
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    conn = db.get_conn()
    for table in ("replace_previews", "finalizations", "batch_sources", "batches",
                  "ingredient_versions", "ingredients"):
        conn.execute(f"DELETE FROM {table}")
    yield


# ---------------------------------------------------------------- 原料版本

def test_ingredient_correction_versioning():
    r = client.post("/ingredients", json={
        "id": "ING-1", "name": "全脂奶粉", "allergens": ["milk"]})
    assert r.status_code == 201
    assert r.json()["version"] == 1

    # 期望版本不符 -> 409，历史保留
    r = client.post("/ingredients/ING-1/corrections", json={
        "allergens": ["milk", "lactose"], "expected_version": 5})
    assert r.status_code == 409

    r = client.post("/ingredients/ING-1/corrections", json={
        "allergens": ["milk", "lactose"], "expected_version": 1})
    assert r.status_code == 201
    assert r.json()["version"] == 2

    r = client.get("/ingredients/ING-1/versions")
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["allergens"] == ["milk"]  # 旧版本仍在

    # 基于过期版本的并发更正被拒绝
    r = client.post("/ingredients/ING-1/corrections", json={
        "allergens": ["milk"], "expected_version": 1})
    assert r.status_code == 409


def test_ingredient_not_found():
    assert client.get("/ingredients/NOPE").status_code == 404
    assert client.post("/ingredients/NOPE/corrections", json={
        "allergens": [], "expected_version": 1}).status_code == 404


# ---------------------------------------------------------------- 批次来源校验

def test_batch_source_validation():
    client.post("/ingredients", json={
        "id": "ING-P", "name": "花生酱", "allergens": ["peanut"]})

    # 空来源
    r = client.post("/batches", json={"id": "B-E", "product": "x", "sources": []})
    assert r.status_code == 422

    # 未知来源
    r = client.post("/batches", json={
        "id": "B-U", "product": "x",
        "sources": [{"type": "ingredient", "id": "GHOST"}]})
    assert r.status_code == 422

    # 自引用
    r = client.post("/batches", json={
        "id": "B-S", "product": "x",
        "sources": [{"type": "batch", "id": "B-S"}]})
    assert r.status_code == 422

    # 间接循环：B-1 -> ING-P，B-2 -> B-1，再把 B-1 指向 B-2
    client.post("/batches", json={
        "id": "B-1", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-P"}]})
    client.post("/batches", json={
        "id": "B-2", "product": "x",
        "sources": [{"type": "batch", "id": "B-1"}]})
    r = client.put("/batches/B-1/sources", json={
        "sources": [{"type": "batch", "id": "B-2"}]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 声明计算

def _setup_graph():
    client.post("/ingredients", json={
        "id": "ING-A", "name": "奶粉", "allergens": ["milk"]})
    client.post("/ingredients", json={
        "id": "ING-B", "name": "花生粉", "allergens": ["peanut", "milk"]})
    client.post("/batches", json={
        "id": "B-BASE", "product": "基料",
        "sources": [{"type": "ingredient", "id": "ING-A"},
                    {"type": "ingredient", "id": "ING-B"}]})
    client.post("/batches", json={
        "id": "B-TOP", "product": "成品",
        "sources": [{"type": "batch", "id": "B-BASE"},
                    {"type": "ingredient", "id": "ING-A"}]})


def test_declaration_union_paths_dedup():
    _setup_graph()
    r = client.get("/batches/B-TOP/declaration")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "draft"
    decl = body["declaration"]
    # 并集
    assert decl["allergens"] == ["milk", "peanut"]
    # 重复来源只计一次：ING-A 经两条路径到达，路径各自唯一
    assert decl["paths"]["milk"] == [
        "B-TOP -> B-BASE -> ING-A",
        "B-TOP -> B-BASE -> ING-B",
        "B-TOP -> ING-A",
    ]
    assert decl["paths"]["peanut"] == ["B-TOP -> B-BASE -> ING-B"]
    assert decl["source_versions"] == {"ING-A": 1, "ING-B": 1}


def test_finalize_then_upstream_correction_and_downstream_recompute():
    _setup_graph()
    # 定版
    r = client.post("/batches/B-TOP/finalize")
    assert r.status_code == 200
    assert r.json()["declaration"]["allergens"] == ["milk", "peanut"]

    # 定版后来源不可改
    r = client.put("/batches/B-TOP/sources", json={
        "sources": [{"type": "ingredient", "id": "ING-A"}]})
    assert r.status_code == 409
    # 重复定版
    assert client.post("/batches/B-TOP/finalize").status_code == 409

    # 下游批次（未定版）引用已定版批次
    client.post("/batches", json={
        "id": "B-DOWN", "product": "礼盒",
        "sources": [{"type": "batch", "id": "B-TOP"}]})

    # 上游更正：ING-B 增加 sesame、去掉 milk
    r = client.post("/ingredients/ING-B/corrections", json={
        "allergens": ["peanut", "sesame"], "expected_version": 1})
    assert r.status_code == 201

    # 已定版批次：同时给出定版快照、现算集合与差异，标记待复核
    r = client.get("/batches/B-TOP/declaration")
    body = r.json()
    assert body["status"] == "finalized"
    assert body["review_status"] == "pending_review"
    assert body["finalized"]["allergens"] == ["milk", "peanut"]
    assert body["current"]["allergens"] == ["milk", "peanut", "sesame"]
    assert body["diff"] == {"added": ["sesame"], "removed": []}
    assert body["current"]["source_versions"] == {"ING-A": 1, "ING-B": 2}

    # 下游批次按当前证据重算
    r = client.get("/batches/B-DOWN/declaration")
    assert r.json()["declaration"]["allergens"] == ["milk", "peanut", "sesame"]

    # 移除 milk 后再次查询：新增与消失同时出现
    client.post("/ingredients/ING-A/corrections", json={
        "allergens": [], "expected_version": 1})
    r = client.get("/batches/B-TOP/declaration")
    body = r.json()
    assert body["current"]["allergens"] == ["peanut", "sesame"]
    assert body["diff"] == {"added": ["sesame"], "removed": ["milk"]}
    assert body["review_status"] == "pending_review"


def test_finalized_batch_in_sync_when_no_change():
    _setup_graph()
    client.post("/batches/B-TOP/finalize")
    r = client.get("/batches/B-TOP/declaration")
    body = r.json()
    assert body["review_status"] == "in_sync"
    assert body["diff"] == {"added": [], "removed": []}


# ------------------------------------------------- 替换预演 / 采纳

def _make_simple_graph():
    """B-T: [ING-A(milk), ING-B(peanut,milk)]；另建一批干净候选原料。"""
    client.post("/ingredients", json={
        "id": "ING-A", "name": "奶粉", "allergens": ["milk"]})
    client.post("/ingredients", json={
        "id": "ING-B", "name": "花生粉", "allergens": ["peanut", "milk"]})
    client.post("/ingredients", json={
        "id": "ING-C", "name": "米粉", "allergens": []})
    client.post("/batches", json={
        "id": "B-T", "product": "饼干",
        "sources": [{"type": "ingredient", "id": "ING-A"},
                    {"type": "ingredient", "id": "ING-B"}]})


def _preview(batch_id, payload):
    return client.post(f"/batches/{batch_id}/replace/preview", json=payload)


def test_preview_zero_replacement_when_already_compliant():
    _make_simple_graph()
    # 原配方不含 sesame：零替换即可，批次不发生变化
    r = _preview("B-T", {
        "forbidden_allergens": ["sesame"],
        "replacements": [{"seq": 0, "candidates": [
            {"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is True
    assert body["replacement_count"] == 0
    assert body["replaced_positions"] == []
    assert body["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-B"}]
    assert body["declaration"]["allergens"] == ["milk", "peanut"]
    assert "preview_token" in body["basis"]
    # 预演不改动批次
    assert client.get("/batches/B-T").json()["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-B"}]


def test_preview_min_positions_single_replace():
    _make_simple_graph()
    # peanut 仅由位置 1（ING-B）贡献：换位置 1 一处即可，替换数 1；
    # 验证不会因为同时给出位置 0 的候选而多换。
    r = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]},
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is True
    assert body["replacement_count"] == 1
    assert body["replaced_positions"] == [1]
    assert body["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-C"}]
    assert body["declaration"]["allergens"] == ["milk"]

    # milk 由两个位置共同贡献：必须两处都换（贪心逐位置会失败），替换数 2
    r = _preview("B-T", {
        "forbidden_allergens": ["milk"],
        "replacements": [
            {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]},
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    body = r.json()
    assert body["feasible"] is True
    assert body["replacement_count"] == 2
    assert body["declaration"]["allergens"] == []


def test_preview_no_feasible_combination():
    _make_simple_graph()
    # 唯一候选 ING-C 本身干净，但把 seq1 换成 ING-C 后 seq0 的 ING-A 仍有 milk
    r = _preview("B-T", {
        "forbidden_allergens": ["milk", "peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is False
    assert "token" not in body
    # 批次未被改动
    assert len(client.get("/batches/B-T").json()["sources"]) == 2


def test_preview_tie_break_lexicographic():
    # 同一位置两个均可解决问题的候选：ING-Z 与 ING-C，取 ID 字典序首个
    client.post("/ingredients", json={
        "id": "ING-A", "name": "奶粉", "allergens": ["milk"]})
    client.post("/ingredients", json={
        "id": "ING-C", "name": "米粉", "allergens": []})
    client.post("/ingredients", json={
        "id": "ING-Z", "name": "淀粉", "allergens": []})
    client.post("/batches", json={
        "id": "B-X", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-A"}]})
    r = _preview("B-X", {
        "forbidden_allergens": ["milk"],
        "replacements": [{"seq": 0, "candidates": [
            {"type": "ingredient", "id": "ING-Z"},
            {"type": "ingredient", "id": "ING-C"}]}]})
    body = r.json()
    assert body["feasible"] is True
    assert body["sources"][0] == {"type": "ingredient", "id": "ING-C"}


def test_preview_tie_break_type_then_id():
    # 并列时候选类型优先（batch < ingredient），同类型再按 ID
    client.post("/ingredients", json={
        "id": "ING-A", "name": "奶粉", "allergens": ["milk"]})
    client.post("/ingredients", json={
        "id": "ING-C", "name": "米粉", "allergens": []})
    client.post("/batches", json={
        "id": "B-SUB", "product": "无奶基料",
        "sources": [{"type": "ingredient", "id": "ING-C"}]})
    client.post("/batches", json={
        "id": "B-X", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-A"}]})
    r = _preview("B-X", {
        "forbidden_allergens": ["milk"],
        "replacements": [{"seq": 0, "candidates": [
            {"type": "ingredient", "id": "ING-C"},
            {"type": "batch", "id": "B-SUB"}]}]})
    body = r.json()
    assert body["feasible"] is True
    assert body["sources"][0] == {"type": "batch", "id": "B-SUB"}


def test_preview_validation_errors():
    _make_simple_graph()
    # 空替换指令
    assert _preview("B-T", {"forbidden_allergens": ["milk"]}).status_code == 422
    # 重复序号
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]},
        {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 422
    # 非法序号
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 9, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 422
    # 空候选
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": []}]})
    assert r.status_code == 422
    # 未知候选来源（原料 / 批次）
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "ingredient", "id": "GHOST"}]}]})
    assert r.status_code == 422
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "batch", "id": "GHOST"}]}]})
    assert r.status_code == 422


def test_preview_candidate_introducing_cycle_rejected():
    client.post("/ingredients", json={
        "id": "ING-A", "name": "米粉", "allergens": []})
    client.post("/batches", json={
        "id": "B-1", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-A"}]})
    client.post("/batches", json={
        "id": "B-2", "product": "x",
        "sources": [{"type": "batch", "id": "B-1"}]})
    # B-1 想把来源换成 B-2：B-2 -> B-1 会成环
    r = _preview("B-1", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "batch", "id": "B-2"}]}]})
    assert r.status_code == 422
    # 自引用候选同样拒绝
    r = _preview("B-1", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "batch", "id": "B-1"}]}]})
    assert r.status_code == 422


def test_preview_unknown_and_finalized_batch():
    _make_simple_graph()
    assert _preview("NOPE", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).status_code == 404
    client.post("/batches/B-T/finalize")
    r = _preview("B-T", {"forbidden_allergens": ["milk"], "replacements": [
        {"seq": 0, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]})
    assert r.status_code == 409


def test_adopt_happy_path_replaces_sources():
    _make_simple_graph()
    body = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()
    assert body["feasible"] is True
    token = body["basis"]["preview_token"]

    r = client.post("/batches/B-T/replace/adopt", json={"preview_token": token})
    assert r.status_code == 200
    adopted = r.json()
    assert adopted["adopted"] is True
    assert adopted["replaced_positions"] == [1]
    assert adopted["batch"]["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-C"}]
    # 声明接口与采纳结果一致（接口兼容）
    decl = client.get("/batches/B-T/declaration").json()["declaration"]
    assert decl["allergens"] == ["milk"]
    assert decl["source_versions"] == {"ING-A": 1, "ING-C": 1}
    # 依据一次性消费
    r = client.post("/batches/B-T/replace/adopt", json={"preview_token": token})
    assert r.status_code == 404


def test_adopt_zero_replacement_leaves_batch_intact():
    _make_simple_graph()
    token = _preview("B-T", {
        "forbidden_allergens": ["sesame"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()["basis"]["preview_token"]
    r = client.post("/batches/B-T/replace/adopt", json={"preview_token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["replacement_count"] == 0
    assert body["batch"]["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-B"}]


def test_adopt_stale_after_target_source_change_rejected():
    _make_simple_graph()
    token = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()["basis"]["preview_token"]

    # 预演后目标批次直接来源发生变化 -> 依据过期
    client.put("/batches/B-T/sources", json={"sources": [
        {"type": "ingredient", "id": "ING-C"}]})
    r = client.post("/batches/B-T/replace/adopt", json={"preview_token": token})
    assert r.status_code == 409
    # 批次保持为变化后的现状（采纳未写入任何内容）
    assert client.get("/batches/B-T").json()["sources"] == [
        {"type": "ingredient", "id": "ING-C"}]


def test_adopt_stale_after_ingredient_correction_rejected():
    _make_simple_graph()
    token = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()["basis"]["preview_token"]

    # 预演后参与预演的原料版本变化（ING-A 去 milk，版本 1 -> 2）
    r = client.post("/ingredients/ING-A/corrections", json={
        "allergens": [], "expected_version": 1})
    assert r.status_code == 201
    r = client.post("/batches/B-T/replace/adopt", json={"preview_token": token})
    assert r.status_code == 409
    # 批次不变
    assert client.get("/batches/B-T").json()["sources"] == [
        {"type": "ingredient", "id": "ING-A"},
        {"type": "ingredient", "id": "ING-B"}]


def test_adopt_stale_after_intermediate_batch_edge_change_rejected():
    # 预演方案经过子批次 B-SUB；预演后修改 B-SUB 的来源边应令依据过期
    client.post("/ingredients", json={
        "id": "ING-A", "name": "奶粉", "allergens": ["milk"]})
    client.post("/ingredients", json={
        "id": "ING-C", "name": "米粉", "allergens": []})
    client.post("/ingredients", json={
        "id": "ING-D", "name": "淀粉", "allergens": []})
    client.post("/batches", json={
        "id": "B-SUB", "product": "基料",
        "sources": [{"type": "ingredient", "id": "ING-C"}]})
    client.post("/batches", json={
        "id": "B-X", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-A"}]})
    token = _preview("B-X", {
        "forbidden_allergens": ["milk"],
        "replacements": [
            {"seq": 0, "candidates": [{"type": "batch", "id": "B-SUB"}]}]}
    ).json()["basis"]["preview_token"]

    # 子批次来源边变化（声明过敏原恰好仍为空，仍必须判为过期）
    client.put("/batches/B-SUB/sources", json={"sources": [
        {"type": "ingredient", "id": "ING-D"}]})
    r = client.post("/batches/B-X/replace/adopt", json={"preview_token": token})
    assert r.status_code == 409
    # B-X 未被替换
    assert client.get("/batches/B-X").json()["sources"] == [
        {"type": "ingredient", "id": "ING-A"}]


def test_adopt_finalized_and_token_mismatch():
    _make_simple_graph()
    token = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()["basis"]["preview_token"]

    # 未知依据
    assert client.post("/batches/B-T/replace/adopt",
                       json={"preview_token": "nope"}).status_code == 404

    # 定版后不得采纳
    client.post("/batches/B-T/finalize")
    assert client.post("/batches/B-T/replace/adopt",
                       json={"preview_token": token}).status_code == 409

    # 定版批次保持既有快照接口兼容
    body = client.get("/batches/B-T/declaration").json()
    assert body["status"] == "finalized"
    assert body["finalized"]["allergens"] == ["milk", "peanut"]


def test_adopt_token_owned_by_other_batch():
    _make_simple_graph()
    client.post("/batches", json={
        "id": "B-OTHER", "product": "x",
        "sources": [{"type": "ingredient", "id": "ING-C"}]})
    token = _preview("B-T", {
        "forbidden_allergens": ["peanut"],
        "replacements": [
            {"seq": 1, "candidates": [{"type": "ingredient", "id": "ING-C"}]}]}
    ).json()["basis"]["preview_token"]
    r = client.post("/batches/B-OTHER/replace/adopt",
                    json={"preview_token": token})
    assert r.status_code == 422
    # 两个批次均未改动
    assert client.get("/batches/B-OTHER").json()["sources"] == [
        {"type": "ingredient", "id": "ING-C"}]
