"""Claude 抽取结果的 Pydantic 模型(同时也是 messages.parse 的 JSON schema)。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ModelPrice(BaseModel):
    """单个模型的按量价格, 单位: 每百万 tokens。"""

    model: str = Field(..., description="模型名, 保留页面原文, 如 GPT-5.2 / qwen3-max")
    input_per_1m: Optional[float] = Field(
        None, description="输入价格(每百万 tokens), 页面原币种; 没有则 null")
    output_per_1m: Optional[float] = Field(
        None, description="输出价格(每百万 tokens), 页面原币种; 没有则 null")
    cached_input_per_1m: Optional[float] = Field(
        None, description="缓存命中的输入价格(每百万 tokens); 没有则 null")
    currency: Optional[str] = Field(
        None, description="该价格使用的币种: USD / CNY / EUR")
    note: Optional[str] = Field(
        None, description="备注: 限时折扣、免费额度、档位、模型类型(如 embedding)等")


class PricingPage(BaseModel):
    """一个厂商价格页的完整抽取结果。"""

    currency: Optional[str] = Field(
        None, description="该页价格的主要币种: USD / CNY / EUR")
    models: List[ModelPrice] = Field(default_factory=list, description="模型价格列表")
    promotions: Optional[str] = Field(
        None, description="页面上明显的促销/活动文字整段, 没有则 null")
    page_has_pricing: bool = Field(
        True, description="页面是否包含可解析的 API 价格表; JS 空壳/报错页为 false")


class NewsEntry(BaseModel):
    """一条官方公告。"""

    date: Optional[str] = Field(None, description="页面标注的日期原文, 如 2026-08-20")
    title: str = Field(..., description="公告标题原文")
    url: Optional[str] = Field(None, description="该条目在页面文本中的链接, 没有则 null")
    summary: Optional[str] = Field(None, description="不超过 60 字的中文一句话摘要")


class NewsPage(BaseModel):
    """一个厂商公告页的抽取结果。"""

    entries: List[NewsEntry] = Field(
        default_factory=list, description="最近公告, 最多 12 条")
