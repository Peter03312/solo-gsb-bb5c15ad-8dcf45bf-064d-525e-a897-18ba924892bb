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
