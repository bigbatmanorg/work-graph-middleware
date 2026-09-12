from __future__ import annotations

import argparse
import json
from pathlib import Path

from work_graph_middleware.persistence import SQLiteStore
from work_graph_middleware.presentation.projection import project


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect durable WorkGraph state")
    parser.add_argument("database", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--after-seq", type=int, default=0)
    args = parser.parse_args()
    store = SQLiteStore(args.database)
    if args.events:
        print(
            json.dumps(
                [item.to_dict() for item in store.events(args.run_id, after_seq=args.after_seq)],
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(json.dumps(project(store.load(args.run_id)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
