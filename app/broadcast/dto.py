"""Broadcast 层跨模块传输对象（DTO）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator, model_validator


def _strip_nonempty(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


class PlanContact(BaseModel):
    """Plan 联系人：主键使用 conv_local，display_name 仅用于展示。"""

    conv_local: str = ""
    display_name: str

    @field_validator("conv_local")
    @classmethod
    def _conv_local(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        return _strip_nonempty(value, "display_name")


class ContactRowDTO(BaseModel):
    """联系人表行 DTO。"""

    conv_local: str
    display_name: str
    remote: str = ""
    category: str = ""
    labels: list[str] = []
    # 客户/群"本身"的原始名（客户微信名 / 群成员拼接名 / 群原名），
    # 与 display_name（我们打的备注/群自定义名）区分开，供 GUI 展示"id"列。
    peer_name: str = ""

    @field_validator("conv_local", "display_name", "remote", "category", "peer_name")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return str(value).strip()

    @model_validator(mode="after")
    def _check_required(self) -> "ContactRowDTO":
        self.conv_local = _strip_nonempty(self.conv_local, "conv_local")
        self.display_name = _strip_nonempty(self.display_name, "display_name")
        self.labels = [str(x).strip() for x in self.labels if str(x).strip()]
        return self


class AssetRowDTO(BaseModel):
    """素材表行 DTO。"""

    asset_type: str
    item_id: str
    name: str
    tags: list[str] = []
    path: str = ""
    thumb_path: str = ""
    extra: dict[str, Any] = {}

    @field_validator("asset_type", "item_id", "name", "path", "thumb_path")
    @classmethod
    def _strip(cls, value: str) -> str:
        return str(value).strip()

    @model_validator(mode="after")
    def _check_required(self) -> "AssetRowDTO":
        self.asset_type = _strip_nonempty(self.asset_type, "asset_type")
        self.item_id = _strip_nonempty(self.item_id, "item_id")
        self.name = _strip_nonempty(self.name, "name")
        self.tags = [str(x).strip() for x in self.tags if str(x).strip()]
        return self


__all__ = ["PlanContact", "ContactRowDTO", "AssetRowDTO"]
