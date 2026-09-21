from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import settings
from .mail import LocalTransport, build_transport
from .models import DocType


def _llm():
    if settings.llm_backend != "ollama":
        return None
    from .llm import OllamaParser

    p = OllamaParser(settings.ollama_url, settings.ollama_model)
    if not p.available():
        logging.getLogger(__name__).warning("Ollama model %s not reachable at %s; running with rules only", settings.ollama_model, settings.ollama_url)
        return None
    return p


def cmd_fetch(args):
    from .bundler import bundle
    from .uarb import fetch_once

    doc_type = DocType(args.doc_type)
    result = asyncio.run(fetch_once(settings.uarb_url, settings.data_dir / "downloads", args.matter, doc_type, limit=args.limit, headless=settings.headless, max_total_bytes=settings.max_total_bytes, max_file_bytes=settings.attachment_budget_bytes))
    out = settings.data_dir / "zips"
    z = bundle(result.downloaded, out, f"{args.matter} {doc_type.value}", budget_bytes=settings.attachment_budget_bytes)
    print(json.dumps(result.metadata.model_dump(mode="json"), indent=2, default=str))
    print(f"listed {len(result.listed)}, downloaded {len(result.downloaded)} in {result.duration_s:.0f}s")
    for part in z.parts:
        print(f"zip: {part.path} ({part.size / 1_048_576:.1f} MB, {len(part.members)} files)")
    for f in z.too_large:
        print(f"too large to email: {f.row.doc_id} {f.size / 1_048_576:.1f} MB")
    for s in result.skipped:
        print(f"skipped: {s}")


def cmd_ask(args):
    """Drop a request into the local mailbox and handle it right away, no email account needed."""
    from .agent import Agent

    transport = LocalTransport(settings.data_dir / "mailbox", address="agent@local")
    transport.drop(args.sender, args.subject or "", args.text)
    agent = Agent(settings, transport, llm=_llm())
    n = asyncio.run(agent.run_once())
    sent = transport.sent()[-1] if transport.sent() else None
    print(f"handled {n} request(s)")
    if sent:
        print(f"\nSubject: {sent['subject']}\nAttachments: {sent['attachments']}\n\n{sent['body']}")


def cmd_run(args):
    from .agent import Agent

    transport = build_transport(settings)
    agent = Agent(settings, transport, llm=_llm())
    asyncio.run(agent.run_forever(stop_after=args.stop_after))


def cmd_parse(args):
    from .request_parser import parse_request

    req = parse_request(args.text, args.subject or "", llm=_llm())
    print(req.model_dump_json(indent=2))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="uarb-agent", description="Email agent for the Nova Scotia UARB public documents database")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="fetch one matter/tab to a zip without any email")
    p.add_argument("matter")
    p.add_argument("doc_type", choices=[d.value for d in DocType])
    p.add_argument("--limit", type=int, default=settings.max_docs)
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("ask", help="simulate an email through the local mailbox and print the reply")
    p.add_argument("text")
    p.add_argument("--subject", default="")
    p.add_argument("--sender", default="you@example.com")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("run", help="poll the configured mailbox and answer requests")
    p.add_argument("--stop-after", type=int, default=None)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("parse", help="show how a request would be interpreted")
    p.add_argument("text")
    p.add_argument("--subject", default="")
    p.set_defaults(fn=cmd_parse)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
