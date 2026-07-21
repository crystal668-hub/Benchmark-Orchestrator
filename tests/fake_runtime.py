from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path


cancelled = False


def handle_term(_signum: int, _frame: object) -> None:
    global cancelled
    cancelled = True


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def result(group_id: str, record_id: str, score: float = 0.75) -> dict[str, object]:
    return {
        "schema_version": 3,
        "group_id": group_id,
        "record_id": record_id,
        "dataset": "verifier_grounded_rdkit",
        "subset": "verifier_grounded_rdkit",
        "eval_kind": "verifier_grounded",
        "evaluation": {
            "primary_metric": "verifier_score",
            "score": score,
            "normalized_score": score,
            "passed": None,
            "details": {},
        },
        "run_lifecycle_status": "completed",
        "protocol_completion_status": "completed",
        "protocol_acceptance_status": None,
        "answer_availability": "native_final",
        "answer_reliability": "native",
        "evaluable": True,
        "scored": True,
        "recovery_mode": "none",
        "degraded_execution": False,
        "execution_error_kind": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    pairs = [tuple(item.split(":", 1)) for item in args.pairs.split(",")]
    signal.signal(signal.SIGTERM, handle_term)
    write_json(
        output / "progress/state.json",
        {"status": "running", "total": len(pairs), "completed": 0, "groups": {}},
    )
    completed: list[dict[str, object]] = []
    for group_id, record_id in pairs:
        checkpoint = (
            output / "per-record" / group_id / f"{record_id.replace('_', '-')}.json"
        )
        if args.merge and checkpoint.exists():
            completed.append(json.loads(checkpoint.read_text(encoding="utf-8")))
            continue
        deadline = time.monotonic() + args.delay
        while time.monotonic() < deadline:
            if cancelled:
                return 143
            time.sleep(0.02)
        if cancelled:
            return 143
        payload = result(group_id, record_id)
        write_json(checkpoint, payload)
        completed.append(payload)
        write_json(
            output / "progress/state.json",
            {
                "status": "running",
                "total": len(pairs),
                "completed": len(completed),
                "groups": {},
            },
        )
    write_json(
        output / "results.json",
        {
            "schema_version": 3,
            "records": len({pair[1] for pair in pairs}),
            "results": completed,
            "groups": [],
            "summary": {},
        },
    )
    write_json(
        output / "runtime-manifest.json",
        {"schema_version": 1, "run_groups": sorted({pair[0] for pair in pairs})},
    )
    write_json(
        output / "progress/state.json",
        {
            "status": "completed",
            "total": len(pairs),
            "completed": len(pairs),
            "groups": {},
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
