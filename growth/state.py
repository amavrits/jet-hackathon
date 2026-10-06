"""GraphState and Recommendation Pydantic models.

Specialist agents ask the LLM for `RecommendationDraft`s (no id, no numbers).
The graph turns each draft into a `Recommendation`, and `agents/impact.py`
fills in `impact` with heuristic figures. The LLM never produces impact numbers.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["reviews", "menu", "pricing", "promo_ads"]
Confidence = Literal["low", "med", "high"]
EvidenceKind = Literal["review", "order_stat", "menu_item", "competitor", "ad_campaign", "listing"]


class EvidenceRef(BaseModel):
    """Pointer to real rows backing a recommendation."""

    kind: EvidenceKind
    ref: str = Field(description="Row id (review_id, menu_item_id, ...) or the name of the query that produced a stat.")
    detail: str = Field(description="Short human-readable fact, for example 'avg rating 2.1 over 14 reviews'.")


class PatchOp(BaseModel):
    """One RFC 6902 JSON Patch operation on the listing document."""

    op: Literal["add", "remove", "replace"]
    path: str = Field(description="JSON pointer into the listing, for example /menu/3/price_eur.")
    value: Any = None


class Impact(BaseModel):
    """Projected weekly effect. Filled by agents/impact.py, never by the LLM."""

    orders_per_week: float = 0.0
    gmv_eur_per_week: float = 0.0
    jet_revenue_eur_per_week: float = 0.0
    ad_spend_eur: float = 0.0
    formula: str = Field(default="", description="The heuristic used, shown in the UI.")


class RecommendationDraft(BaseModel):
    """What a specialist agent asks the LLM to return."""

    title: str = Field(description="Imperative, one line, for example 'Add photos to your 3 bestsellers'.")
    rationale: str = Field(description="Why this helps orders, basket value, conversion or ad efficiency.")
    evidence: list[EvidenceRef] = Field(min_length=1, description="At least one reference to real data.")
    patch: list[PatchOp] | None = Field(default=None, description="JSON patch on the listing, or null if advice-only.")
    confidence: Confidence


class Recommendation(RecommendationDraft):
    id: str
    agent: AgentName
    impact: Impact | None = None

    @classmethod
    def from_draft(cls, draft: RecommendationDraft, agent: AgentName, n: int) -> "Recommendation":
        return cls(id=f"{agent}-{n}", agent=agent, **draft.model_dump())


class AppliedChange(BaseModel):
    """One approved patch written to the listing. Mirrors a `change_log` row."""

    change_id: str
    recommendation_id: str
    patch: list[PatchOp]
    applied_at: datetime


class Summary(BaseModel):
    approved: int = 0
    rejected: int = 0
    total: Impact = Field(default_factory=Impact)
    scaled_restaurants: int = 0
    scaled_jet_revenue_eur_per_week: float = 0.0
    text: str = ""


def merge_by_agent(
    left: dict[str, list[Recommendation]], right: dict[str, list[Recommendation]]
) -> dict[str, list[Recommendation]]:
    """Reducer: the four specialists run in parallel and each writes its own key."""
    return {**left, **right}


class GraphState(BaseModel):
    restaurant_id: str
    # Output of load_context: restaurant, listing, menu, sales, reviews, competitors, ads.
    context: dict[str, Any] = Field(default_factory=dict)
    recommendations: Annotated[dict[str, list[Recommendation]], merge_by_agent] = Field(default_factory=dict)
    # Flat list after impact + rank, best first. This is what the owner sees.
    ranked: list[Recommendation] = Field(default_factory=list)
    # recommendation id -> approved? Missing ids are undecided.
    approvals: dict[str, bool] = Field(default_factory=dict)
    applied: list[AppliedChange] = Field(default_factory=list)
    summary: Summary | None = None

    def all_recommendations(self) -> list[Recommendation]:
        return [r for recs in self.recommendations.values() for r in recs]
