"""Pydantic models for graph state and the Recommendation contract."""

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["reviews", "menu", "pricing", "promo_ads"]
Confidence = Literal["low", "med", "high"]


class Evidence(BaseModel):
    """A pointer to real rows backing a recommendation."""

    kind: Literal["review", "order_stat", "competitor", "menu_item", "ad_campaign"]
    ref: str  # e.g. "reviews.id=42" or "orders: item_id=7, last 8 weeks"
    detail: str
    row_id: int | None = None  # checked against the database before a rec is surfaced


class Impact(BaseModel):
    """Weekly impact computed by impact.py, never by an LLM."""

    orders_per_week: float
    gmv_eur_per_week: float
    jet_revenue_eur_per_week: float
    ad_spend_eur: float = 0.0
    partner_cost_eur_per_week: float = 0.0
    formula: str


class Recommendation(BaseModel):
    id: str
    agent: AgentName
    title: str
    rationale: str
    evidence: list[Evidence] = Field(min_length=1)
    patch: list[dict[str, Any]] | None = None  # RFC 6902 JSON patch on the listing
    impact_basis: dict[str, Any]  # inputs for impact.py; "kind" selects the heuristic
    impact: Impact | None = None
    confidence: Confidence


class Restaurant(BaseModel):
    id: str
    name: str
    cuisine: str
    city: str
    delivery_model: Literal["marketplace", "jet_delivery"]
    commission_rate: float


class MenuItemStats(BaseModel):
    item_id: int
    name: str
    category: str
    price_eur: float
    description: str
    has_photo: bool
    orders_per_week: float
    gmv_eur_per_week: float


class Review(BaseModel):
    id: int
    rating: int
    text: str
    item_id: int | None
    ts: str


class SlotStat(BaseModel):
    weekday: int  # 0 = Monday
    slot: str
    orders_per_week: float


class CategoryPrice(BaseModel):
    category: str
    own_median_eur: float
    competitor_median_eur: float
    competitor_count: int


class AdCampaign(BaseModel):
    id: int
    weekly_budget_eur: float
    weeks: int
    attributed_orders: int
    active: bool


class Kpis(BaseModel):
    orders_per_week: float
    gmv_eur_per_week: float
    avg_basket_eur: float
    avg_rating: float
    review_count: int


class Context(BaseModel):
    restaurant: Restaurant
    kpis: Kpis
    peer_median_orders_per_week: float
    menu: list[MenuItemStats]
    reviews: list[Review]
    slots: list[SlotStat]
    category_prices: list[CategoryPrice]
    campaigns: list[AdCampaign]
    listing: dict[str, Any]


class Summary(BaseModel):
    approved: int
    rejected: int
    applied_changes: int
    orders_per_week: float
    gmv_eur_per_week: float
    jet_revenue_eur_per_week: float
    ad_spend_eur: float
    partner_cost_eur_per_week: float


class GraphState(BaseModel):
    restaurant_id: str
    context: Context | None = None
    recommendations: Annotated[list[Recommendation], operator.add] = []
    scored: list[Recommendation] = []
    ranked: list[Recommendation] = []
    decisions: dict[str, bool] = {}
    approver: str = ""
    applied_change_ids: list[int] = []
    summary: Summary | None = None
