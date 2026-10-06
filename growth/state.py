"""GraphState and Recommendation Pydantic models.

Specialist agents ask the LLM for `RecommendationDraft`s (no id, no numbers).
The graph turns each draft into a `Recommendation`, and `agents/impact.py`
fills in `impact` with heuristic figures. The LLM never produces impact numbers.
"""

import operator
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
    value: float | None = Field(default=None, description="The number behind the fact, when there is one.")


class PatchOp(BaseModel):
    """One RFC 6902 JSON Patch operation on the listing document."""

    op: Literal["add", "remove", "replace"]
    path: str = Field(description="JSON pointer into the listing, for example /menu/<menu_item_id>/price_eur.")
    value: Any = None


class Impact(BaseModel):
    """Projected weekly effect. Filled by agents/impact.py, never by the LLM."""

    orders_per_week: float = 0.0
    gmv_eur_per_week: float = 0.0
    jet_revenue_eur_per_week: float = 0.0
    ad_spend_eur: float = 0.0
    # What the restaurant gives up per week: price revenue on existing sales, promo discounts,
    # extra ad spend. Negative = a saving. Shown next to JET's gain so the trade-off is visible.
    partner_cost_eur_per_week: float = 0.0
    formula: str = Field(default="", description="The heuristic used, shown in the UI.")


class RecommendationDraft(BaseModel):
    """What a specialist agent asks the LLM to return."""

    title: str = Field(description="Imperative, one line, for example 'Add photos to your 3 bestsellers'.")
    rationale: str = Field(description="Why this helps orders, basket value, conversion or ad efficiency.")
    evidence: list[EvidenceRef] = Field(min_length=1, description="At least one reference to real data.")
    patch: list[PatchOp] | None = Field(default=None, description="JSON patch on the listing, or null if advice-only.")
    confidence: Confidence


# Levers of the demand model, named exactly as the columns of `restaurant_weeks`.
Lever = Literal["photo_share", "description_share", "price_index", "promo_active", "ad_budget_eur", "ad_offpeak_share"]


class LeverChange(BaseModel):
    """How a recommendation moves one model lever. This is the contract with growth/ml:
    `predict_impact(restaurant_id, lever_changes)` values a recommendation from these alone.

    `before` is the restaurant's value in its latest `restaurant_weeks` row; `after` is the
    value once the recommendation is live. A partial-week effect (a promo in one slot) is
    expressed as a fraction of the week, e.g. promo_active 0 -> 0.12.
    """

    lever: Lever
    before: float
    after: float
    note: str = Field(default="", description="How `after` was derived, shown next to the impact.")


class Recommendation(RecommendationDraft):
    id: str
    agent: AgentName
    # Set by the agent, never by the LLM. impact.py dispatches on `kind` and reads `facts`.
    kind: str = "advice"
    action: str = Field(default="", description="What the owner (or JET) should do.")
    facts: dict[str, float] = Field(default_factory=dict, description="Numeric inputs for impact.py.")
    # Empty when the recommendation has no lever in the demand model (dish complaints, delivery,
    # hiding a dish): impact.py then keeps its heuristic for it.
    lever_changes: list[LeverChange] = Field(default_factory=list)
    # Recommendations sharing a variant_group are alternatives (e.g. 5/10/15% price cut):
    # choose at most one. None = a standalone recommendation.
    variant_group: str | None = None
    variant_label: str = ""
    impact: Impact | None = None

    def patch_ops(self) -> list[dict[str, Any]]:
        """The patch as plain RFC 6902 dicts, ready for jsonpatch."""
        return [op.model_dump() for op in self.patch or []]

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
    # recommendation id -> the owner's reason for rejecting it (feeds re-planning later).
    rejection_reasons: dict[str, str] = Field(default_factory=dict)
    # Who decided (shown in the app, kept for the audit trail).
    approver: str = ""
    applied: list[AppliedChange] = Field(default_factory=list)
    summary: Summary | None = None
    # Non-fatal problems (an agent or a patch failed). Appended to from parallel nodes.
    errors: Annotated[list[str], operator.add] = Field(default_factory=list)

    def all_recommendations(self) -> list[Recommendation]:
        return [r for recs in self.recommendations.values() for r in recs]
