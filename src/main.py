"""Command line entrypoint. Default path is offline and does not need an API key."""

from __future__ import annotations

import argparse
import json
import sys

from src import __version__
from src.audit import audit_pack
from src.config import PackError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="One User, One Agent. Mock mode is offline and does not need an API key.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="recompute pack counts and known data traps")
    audit.add_argument("--pack", default="./one_user_one_agent_pack", help="path to the data pack")
    audit.add_argument("--out", default=None, help="optional path for the audit JSON")

    replay = commands.add_parser("replay", help="run decide → simulate → judge → impact in mock mode")
    replay.add_argument("--pack", default="./one_user_one_agent_pack")
    replay.add_argument("--out", default="./sample_run")
    replay.add_argument("--model", default="mock", choices=("mock", "openai"))
    replay.add_argument("--reset", action="store_true")
    replay.add_argument("--seed", default="oua-practice")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            report = audit_pack(args.pack)
            text = json.dumps(report, indent=2, ensure_ascii=False)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.write("\n")
            print(text)
            return 0
        if args.command == "replay":
            from src.pipeline import run_pipeline

            summary = run_pipeline(
                args.pack,
                args.out,
                model=args.model,
                reset=args.reset or True,
                seed=args.seed,
            )
            print(json.dumps(summary, indent=2, default=str))
            if summary.get("holdout_actions", 0) != 0:
                print("error: holdout users received actions", file=sys.stderr)
                return 1
            return 0
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"error: unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
