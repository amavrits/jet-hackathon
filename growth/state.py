"""Shared Pydantic models: what agents emit and what the graph carries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["reviews", "menu", "pricing", "promo_ads"]
Confidence = Literal["low", "med", "high"]


class Evidence(BaseModel):
    """A pointer to a real row or a computed stat. No evidence, no recommendation."""

    kind: Literal["review", "menu_item", "order_stat", "campaign", "competitor_item"]
    ref: str  # row id (review_id, menu_item_id, ...) or stat name
    note: str = ""  # short human-readable detail, e.g. the review quote
    value: float | None = None  # for stats


class Impact(BaseModel):
    """Weekly projected effect. Filled by agents/impact.py, never by an LLM."""

    orders_per_week: float
    gmv_eur_per_week: float
    jet_revenue_eur_per_week: float
    ad_spend_eur: float = 0.0
    formula: str  # shown in the UI


class Recommendation(BaseModel):
    id: str
    agent: AgentName
    kind: str  # e.g. "dish_quality", "delivery_quality" - impact.py dispatches on this
    title: str
    rationale: str
    action: str  # what the owner (or JET) should do
    evidence: list[Evidence] = Field(min_length=1)
    facts: dict[str, float] = Field(default_factory=dict)  # numeric inputs impact.py needs
    patch: list[dict[str, Any]] | None = None  # JSON Patch on the listing; None = advice-only
    impact: Impact | None = None
    confidence: Confidence
