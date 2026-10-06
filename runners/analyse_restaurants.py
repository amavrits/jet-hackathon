"""Run and store an analysis for every partner restaurant (or one), with optimiser and Jev selections.

Run:  python -m runners.analyse_restaurants [--restaurant r_spice_route]

Writes to the `analyses` and `analysis_recommendations` tables of the project database. Nothing is
applied to any listing: the graph stops at the owner-approval pause.
"""

from __future__ import annotations

import argparse
import logging

from growth import analyses
from growth.data import queries as q
from growth.graph import build_graph


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", help="one partner restaurant id; default: all partners")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    con = q.connect()
    q.create_schema(con)  # adds the analysis tables to databases built before they existed
    graph = build_graph()
    rids = [args.restaurant] if args.restaurant else [r["restaurant_id"] for r in q.list_restaurants(con)]

    print(f"{'restaurant':<18} {'cards':>5} {'optimiser':>9} {'jev':>4} {'agree':>5}  jev picks")
    for rid in rids:
        a = analyses.latest(con, rid) if analyses.analyse(con, graph, rid) else None
        picks = [r["variant_label"] or r["title"] for r in a["recommendations"] if r["jev_selected"]]
        print(
            f"{rid:<18} {a['n_recommendations']:>5} {a['n_optimizer_selected']:>9} {a['n_jev_selected']:>4} "
            f"{a['n_agree']:>5}  {'; '.join(picks)[:90]}"
        )
    con.close()


if __name__ == "__main__":
    main()
