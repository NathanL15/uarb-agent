"""Score the request parser on evals/parser_cases.json.

Runs rules alone and, if Ollama is reachable, rules + LLM fallback, and writes a
markdown table so the two can be compared case by case.

    python -m evals.parser_eval            # rules only
    python -m evals.parser_eval --llm      # also rules + Ollama
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uarb_agent.config import settings  # noqa: E402
from uarb_agent.models import ParseStatus  # noqa: E402
from uarb_agent.request_parser import parse_request  # noqa: E402

HERE = Path(__file__).parent
CASES = json.loads((HERE / "parser_cases.json").read_text(encoding="utf-8"))


def score(case: dict, llm) -> dict:
    t0 = time.perf_counter()
    req = parse_request(case["text"], case.get("subject", ""), llm=llm)
    dt = time.perf_counter() - t0
    want_status = case.get("status", "ok")
    checks = {
        "status": req.status.value == want_status,
        "matter": (req.matter == case["matter"]) if want_status != "ambiguous_matter" else req.matter == case["matter"],
        "doc_types": [d.value for d in req.doc_types] == case["doc_types"],
        "limit": req.limit == case.get("limit", 10),
    }
    if want_status == "no_matter":
        checks["matter"] = req.matter is None
    return {"id": case["id"], "pass": all(checks.values()), "checks": checks, "got": {"matter": req.matter, "doc_types": [d.value for d in req.doc_types], "status": req.status.value, "limit": req.limit, "source": req.source}, "seconds": round(dt, 3), "needs_llm": case.get("needs_llm", False)}


def run(llm=None) -> list[dict]:
    return [score(c, llm) for c in CASES]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="also run with the Ollama fallback")
    args = ap.parse_args()

    runs = {"rules": run(None)}
    if args.llm:
        from uarb_agent.llm import OllamaParser

        p = OllamaParser(settings.ollama_url, settings.ollama_model)
        if p.available():
            runs["rules+ollama"] = run(p)
        else:
            print(f"Ollama model {settings.ollama_model} not available at {settings.ollama_url}; skipping", file=sys.stderr)

    lines = [f"# Parser eval ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)", ""]
    for name, results in runs.items():
        passed = sum(r["pass"] for r in results)
        rule_only = [r for r in results if not r["needs_llm"]]
        llm_cases = [r for r in results if r["needs_llm"]]
        lines.append(f"## {name}: {passed}/{len(results)} passed")
        lines.append(f"- cases the rules should handle: {sum(r['pass'] for r in rule_only)}/{len(rule_only)}")
        lines.append(f"- cases that need language understanding: {sum(r['pass'] for r in llm_cases)}/{len(llm_cases)}")
        lines.append(f"- median time per case: {sorted(r['seconds'] for r in results)[len(results) // 2]:.3f}s")
        lines.append("")
        lines.append("| case | pass | matter | doc types | status | via | s |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in results:
            g = r["got"]
            lines.append(f"| {r['id']} | {'yes' if r['pass'] else 'NO'} | {g['matter']} | {', '.join(g['doc_types']) or '-'} | {g['status']} | {g['source']} | {r['seconds']:.2f} |")
        lines.append("")
    report = "\n".join(lines)
    print(report)
    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (out / f"parser-{stamp}.md").write_text(report, encoding="utf-8")
    (out / f"parser-{stamp}.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")
    failed = [r["id"] for r in runs["rules"] if not r["pass"] and not r["needs_llm"]]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
