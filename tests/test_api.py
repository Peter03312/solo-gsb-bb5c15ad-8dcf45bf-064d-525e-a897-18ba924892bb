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
    for table in ("finalizations", "batch_sources", "batches",
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
