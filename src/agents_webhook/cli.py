"""Command line entrypoint: migrate, ingest, api and worker subcommands."""

import argparse
import sys
from collections.abc import Sequence

from agents_webhook import __version__

SUBCOMMANDS = {
    "migrate": "apply database migrations",
    "ingest": "run the webhook ingest listener",
    "api": "run the HTTP API",
    "worker": "run the queue worker",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-webhook", description="Agents webhook service")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    print(f"agents-webhook {args.command}: not wired", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
