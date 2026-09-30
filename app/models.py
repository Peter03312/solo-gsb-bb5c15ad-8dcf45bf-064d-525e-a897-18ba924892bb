from typing import List, Literal

from pydantic import BaseModel, Field


class IngredientCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    allergens: List[str] = []


class CorrectionCreate(BaseModel):
    """原料更正：必须携带期望版本，旧版本将被拒绝，历史版本保留。"""

    allergens: List[str]
    expected_version: int = Field(ge=1)


class SourceRef(BaseModel):
    type: Literal["ingredient", "batch"]
    id: str = Field(min_length=1)


class BatchCreate(BaseModel):
    id: str = Field(min_length=1)
    product: str = Field(min_length=1)
    sources: List[SourceRef]


class SourcesUpdate(BaseModel):
    sources: List[SourceRef]


class ReplacementSpec(BaseModel):
    """单个可替换的直接来源位置：seq 为现有直接来源序号，candidates 为候选来源。"""

    seq: int = Field(ge=0)
    candidates: List[SourceRef]


class ReplacePreviewRequest(BaseModel):
    """替换预演请求：禁止出现的过敏原 + 各位置候选来源（未列出的位置保持不变）。"""

    forbidden_allergens: List[str] = []
    replacements: List[ReplacementSpec] = []


class ReplaceAdoptRequest(BaseModel):
    """采纳请求：必须携带预演返回的 preview_token（采纳依据）。"""

    preview_token: str = Field(min_length=1)
