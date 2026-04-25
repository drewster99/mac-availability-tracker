"""Produce an HTML heatmap from the latest availability snapshots in the local db."""
from __future__ import annotations

import argparse
import logging

from mac_availability import viz


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument("--out", default="data/heatmap.html")
    parser.add_argument("--title", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = viz.render_heatmap(args.db, out_path=args.out, title=args.title)
    print(f"Heatmap written to: {out}")


if __name__ == "__main__":
    main()
