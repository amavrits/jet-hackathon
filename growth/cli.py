"""Run the graph headless and print ranked recommendations. Stops at approval; writes nothing."""

import sys
import uuid

from growth.graph import build_graph
from growth.state import GraphState


def main(rid: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    for update in graph.stream(GraphState(restaurant_id=rid), config, stream_mode="updates"):
        for node in update:
            print(f"[{node}] done" if node != "__interrupt__" else "[human_approval] waiting")
    ranked = graph.get_state(config).values["ranked"]
    print(f"\n{len(ranked)} recommendation(s) for {rid}:\n")
    for r in ranked:
        i = r.impact
        print(f"- {r.title}  [{r.agent}, {r.confidence}]")
        print(
            f"  Δorders/wk {i.orders_per_week:+.1f}  ΔGMV/wk €{i.gmv_eur_per_week:+.2f}  "
            f"JET/wk €{i.jet_revenue_eur_per_week:+.2f}  partner cost/wk "
            f"€{i.partner_cost_eur_per_week:.2f}"
        )
    print("\nNothing applied: approval happens in the UI.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m growth.cli <restaurant_id>")
    main(sys.argv[1])
