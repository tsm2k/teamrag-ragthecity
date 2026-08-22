"""Reusable before/after harness: score ANY answer function on Lab 0's questions.

WHY this exists. Track A's top rubric anchor ("RAG Quality & Grounding", 4 pts)
demands you are *measurably* better than naive RAG and can show the numbers.
`make lab0-score` gives you one number for one pipeline — the naive baseline
hard-wired at `lab0_millbrook/judge.py:91`. That is a *before* with no way to
produce an *after*: you cannot point it at your engine, and it prints to stdout
and forgets, so at 2 PM on Saturday you are comparing a fresh run against a
number someone remembers from last night.

This harness fixes both. It takes a pluggable `answer_fn(question) -> str`, so
the same scorer runs against the naive baseline today and your engine tomorrow,
and it writes every run to JSON so two runs can be diffed question by question.
It deliberately REUSES `lab0_millbrook.judge` for `judge_one`, `POINTS` and
`band` — if the harness re-implemented the grading, your "after" would be
measured on a different ruler than your "before" and the delta would be fiction.

Run:
    # score the naive baseline (the "before")
    .venv/bin/python -m team.track_a_engine.eval_harness --label naive-baseline

    # score your engine (the "after") — any module:callable taking a question
    .venv/bin/python -m team.track_a_engine.eval_harness \
        --answer-fn team.track_a_engine.my_engine:answer --label hybrid-v2

    # show the delta that wins the rubric point
    .venv/bin/python -m team.track_a_engine.eval_harness \
        --compare eval_runs/naive-baseline.json eval_runs/hybrid-v2.json
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lab0_millbrook import judge, naive_rag

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = REPO_ROOT / "lab0_boston" / "questions.json"
RESULTS_DIR = REPO_ROOT / "eval_runs"

# An answer function is the ONLY thing a team has to supply. Anything that maps
# a question string to an answer string qualifies: the naive baseline, a router,
# a hybrid retriever, even a hard-coded stub while you are wiring things up.
AnswerFn = Callable[[str], str]


def naive_answer_fn(docs_dir: Path = naive_rag.DOCS_DIR,
                    collection: str = naive_rag.COLLECTION) -> AnswerFn:
    """The baseline as an AnswerFn — this is the 'before' half of the delta.

    Closes over corpus dir + collection so you can score two different corpora
    (or two different chunking strategies) without touching the judge.
    """
    def answer(question: str) -> str:
        chunks = naive_rag.retrieve(question, docs_dir=docs_dir, collection=collection)
        return naive_rag.generate(question, chunks) or "(no answer)"
    return answer


def load_answer_fn(spec: str) -> AnswerFn:
    """Resolve "package.module:callable" into an AnswerFn.

    If the target is a zero-argument factory that returns a callable (the
    `naive_answer_fn` shape), we call it once and use what it returns — that
    lets an engine do expensive setup (load an index, warm a model) exactly
    once instead of per question.
    """
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--answer-fn must look like module:callable, got {spec!r}")
    target = getattr(importlib.import_module(module_name), attr)
    # Decide by signature, not by try/except: a factory that raises TypeError
    # internally would otherwise be silently mistaken for an answer function.
    required = [p for p in inspect.signature(target).parameters.values()
                if p.default is p.empty and p.kind in
                (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if required:
        return target  # takes the question directly
    built = target()  # zero-arg factory: do expensive setup once, not per question
    if not callable(built):
        raise SystemExit(f"{spec} took no arguments and did not return a callable")
    return built


def score_run(answer_fn: AnswerFn, questions: list[dict], llm, label: str,
              questions_file: Path = QUESTIONS, quiet: bool = False) -> dict:
    """Answer + judge every question. Returns a JSON-serializable run record.

    We keep the ANSWER TEXT in the record, which `make lab0-score` throws away.
    That is what makes two runs diffable: a verdict flip tells you the score
    moved, but only the answer text tells you *why* it moved, and "why" is what
    a judge asks you in the demo.
    """
    started = time.time()
    records: list[dict] = []
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        answer = answer_fn(q["question"]) or "(no answer)"
        verdict = judge.judge_one(llm, q, answer)
        records.append({
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "answer": answer,
            "verdict": verdict,
            "points": judge.POINTS[verdict],
            "seconds": round(time.time() - t0, 2),
        })
        if not quiet:
            print(f"  [{i:2d}/{len(questions)}] {q['id']:>2} {verdict:<17} {q['question'][:52]}")

    total = sum(r["points"] for r in records)
    pct = 100.0 * total / len(records) if records else 0.0
    return {
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "questions_file": str(questions_file),
        "n_questions": len(records),
        "total_points": round(total, 2),
        "pct": round(pct, 2),
        "band": judge.band(pct),
        "wall_clock_seconds": round(time.time() - started, 1),
        "fabricated": [r["id"] for r in records if r["verdict"] == "fabricated"],
        "results": records,
    }


def by_category(run: dict) -> dict[str, dict]:
    """Per-category rollup — the table that tells you which failure to kill first."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in run["results"]:
        buckets[r["category"]].append(r)
    return {
        cat: {
            "points": round(sum(r["points"] for r in rs), 2),
            "max": len(rs),
            "pct": round(100.0 * sum(r["points"] for r in rs) / len(rs), 1),
            "ids": [r["id"] for r in rs],
        }
        for cat, rs in sorted(buckets.items())
    }


def print_report(run: dict) -> None:
    cats = by_category(run)
    print(f"\nPER-CATEGORY RESULTS — {run['label']}")
    # Sort weakest first: the prep instruction is "pick which failure you'll
    # kill first", and alphabetical order buries the answer to that question.
    for cat, c in sorted(cats.items(), key=lambda kv: (kv[1]["pct"], kv[0])):
        bar = "#" * int(round(c["pct"] / 10)) or "-"
        print(f"  {cat:<40} {c['points']:>4.1f} / {c['max']:<3} {c['pct']:>5.1f}%  {bar}")
    print(f"\nTOTAL: {run['total_points']:.1f} / {run['n_questions']}  ->  "
          f"{run['pct']:.0f}%   BAND: {run['band']}")
    print(f"Wall clock: {run['wall_clock_seconds']:.1f}s "
          f"({run['wall_clock_seconds'] / max(run['n_questions'], 1):.1f}s per question)")
    if run["fabricated"]:
        print(f"\nCRITICAL FAILURE: fabricated answers on {', '.join(run['fabricated'])}")
        print("Per the rubric, fabricating information not in the documents is an")
        print("automatic critical failure — fix this before chasing points elsewhere.")


def print_delta(before: dict, after: dict) -> None:
    """The slide. Overall delta, per-category delta, and every verdict flip."""
    b_cats, a_cats = by_category(before), by_category(after)
    print(f"\nDELTA: {before['label']}  ->  {after['label']}")
    print(f"  {'category':<40} {'before':>8} {'after':>8} {'delta':>8}")
    for cat in sorted(set(b_cats) | set(a_cats)):
        b = b_cats.get(cat, {"pct": 0.0})["pct"]
        a = a_cats.get(cat, {"pct": 0.0})["pct"]
        print(f"  {cat:<40} {b:>7.1f}% {a:>7.1f}% {a - b:>+7.1f}")
    print(f"  {'OVERALL':<40} {before['pct']:>7.1f}% {after['pct']:>7.1f}% "
          f"{after['pct'] - before['pct']:>+7.1f}")
    print(f"  {'band':<40} {before['band']:>8} -> {after['band']}")

    b_by_id = {r["id"]: r for r in before["results"]}
    flips = [(r, b_by_id[r["id"]]) for r in after["results"]
             if r["id"] in b_by_id and b_by_id[r["id"]]["verdict"] != r["verdict"]]
    print(f"\nVERDICT FLIPS ({len(flips)}):")
    for a, b in flips:
        # Three-way, not two: fabricated -> wrong scores the same 0.0 but is a
        # real win, because the rubric treats fabrication as a critical failure.
        if a["points"] > b["points"]:
            tag = "GAIN"
        elif a["points"] < b["points"]:
            tag = "LOSS"
        else:
            tag = "EVEN"
        note = "  (fabrication fixed)" if b["verdict"] == "fabricated" != a["verdict"] else ""
        note = "  (NEW FABRICATION)" if a["verdict"] == "fabricated" else note
        print(f"  [{tag}] {a['id']:>2} {a['category']:<38} {b['verdict']} -> {a['verdict']}{note}")
    if not flips:
        print("  (none — the two runs scored every question identically)")


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answer-fn", default="team.track_a_engine.eval_harness:naive_answer_fn",
                        help="module:callable taking a question, or a factory returning one")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--label", default="run", help="name this run — becomes the JSON filename")
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--quiet", action="store_true", help="suppress the per-question stream")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"),
                        help="skip scoring; just diff two saved run JSONs")
    args = parser.parse_args()

    if args.compare:
        print_delta(_load(args.compare[0]), _load(args.compare[1]))
        return

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model=naive_rag.MODEL, temperature=0)
        llm.invoke("Reply with: ok")  # fail fast — never 15 minutes in
    except Exception as exc:
        raise SystemExit(f"[!] Ollama unreachable ({type(exc).__name__}): {exc}\n"
                         f"[!] Start it ('ollama serve') and pull: ollama pull {naive_rag.MODEL}")

    run = score_run(load_answer_fn(args.answer_fn), questions, llm, args.label,
                    questions_file=args.questions, quiet=args.quiet)
    print_report(run)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.label}.json"
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved -> {out}")
    print(f"Compare later with:  .venv/bin/python -m team.track_a_engine.eval_harness "
          f"--compare {out} eval_runs/<your-next-run>.json")


if __name__ == "__main__":
    main()
