from fastapi import FastAPI

from . import db, services
from .models import (
    BatchCreate,
    CorrectionCreate,
    IngredientCreate,
    SourcesUpdate,
)

db.get_conn()  # 启动即完成建库建表初始化

app = FastAPI(
    title="Batch Allergen Traceability",
    description="食品加工厂批次过敏原声明追溯服务",
    version="1.0.0",
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 原料

@app.post("/ingredients", status_code=201)
def create_ingredient(payload: IngredientCreate):
    """登记原料，初始版本为 1。"""
    with db.tx() as conn:
        return services.create_ingredient(conn, payload)


@app.get("/ingredients")
def list_ingredients():
    with db.tx(immediate=False) as conn:
        return services.list_ingredients(conn)


@app.get("/ingredients/{ingredient_id}")
def get_ingredient(ingredient_id: str):
    with db.tx(immediate=False) as conn:
        return services.get_ingredient(conn, ingredient_id)


@app.get("/ingredients/{ingredient_id}/versions")
def list_ingredient_versions(ingredient_id: str):
    """完整修订历史（含被更正替代的旧版本）。"""
    with db.tx(immediate=False) as conn:
        return services.list_ingredient_versions(conn, ingredient_id)


@app.post("/ingredients/{ingredient_id}/corrections", status_code=201)
def correct_ingredient(ingredient_id: str, payload: CorrectionCreate):
    """更正过敏原集合。expected_version 与当前版本不符时返回 409，历史保留。"""
    with db.tx() as conn:
        return services.correct_ingredient(conn, ingredient_id, payload)


# ---------------------------------------------------------------- 批次

@app.post("/batches", status_code=201)
def create_batch(payload: BatchCreate):
    """创建未定版批次。空来源、未知来源、循环引用均返回 422。"""
    with db.tx() as conn:
        return services.create_batch(conn, payload)


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str):
    with db.tx(immediate=False) as conn:
        return services.get_batch(conn, batch_id)


@app.put("/batches/{batch_id}/sources")
def update_sources(batch_id: str, payload: SourcesUpdate):
    """整体替换来源（仅未定版批次允许）。"""
    with db.tx() as conn:
        return services.update_sources(conn, batch_id, payload)


@app.get("/batches/{batch_id}/declaration")
def get_declaration(batch_id: str):
    """未定版：现算声明。已定版：定版快照 + 现算集合 + 差异 + 复核状态。"""
    with db.tx(immediate=False) as conn:
        return services.get_declaration(conn, batch_id)


@app.post("/batches/{batch_id}/finalize")
def finalize_batch(batch_id: str):
    """定版：原子保存声明集合、来源版本和路径，之后来源与声明不可改。"""
    with db.tx() as conn:
        return services.finalize_batch(conn, batch_id)
