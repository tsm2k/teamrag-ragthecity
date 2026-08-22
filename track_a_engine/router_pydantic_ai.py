"""
An alternative to SqlLane in router.py, built on PydanticAI instead of a
hand-rolled retry loop. Same guardrails (check_sql / check_rows from
router.py), same golden-query-first design — just a different mechanism for
getting the model to retry on a rejected proposal.

MEASURED: PydanticAI's default structured-output mode asks the model to
return its answer as a tool call. granite3.1-dense:8b over Ollama's
OpenAI-compatible endpoint does not do this reliably — it kept replying with
a JSON *string* instead of an actual tool call, so every attempt hit
"Plain text responses are not permitted, please include your response in a
tool call" and the run failed after exhausting retries. Switching to
PromptedOutput (ask the model to emit JSON in plain text, parse it directly,
no tool call involved) fixed it immediately. If you swap in a model that
does support tool calling well, ToolOutput (the default) is worth
retrying — it is generally more reliable when it works.

Run standalone:
    python -m team.track_a_engine.router_pydantic_ai "which neighborhood files the most 311 requests?"
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, PromptedOutput, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from team.track_a_engine.router import (
    MODEL,
    SCHEMA_CARD,
    Answer,
    _connect,
    _format_rows,
    _strip_fence,
    _tables_in,
    check_rows,
    check_sql,
    coverage_note,
)

OLLAMA_BASE_URL = "http://localhost:11434/v1"


class SQLProposal(BaseModel):
    sql: str


@dataclass
class SqlDeps:
    con: object
    question: str


def make_agent(model_name: str = MODEL) -> Agent:
    model = OpenAIChatModel(model_name, provider=OllamaProvider(base_url=OLLAMA_BASE_URL))
    return Agent(
        model,
        output_type=PromptedOutput(SQLProposal),
        deps_type=SqlDeps,
        retries=3,
        system_prompt="You write DuckDB SQL. Return only the query, no prose.",
    )


class PydanticAISqlLane:
    """Same job as router.SqlLane, same guardrails, PydanticAI drives the retries."""

    name = "sql_pydantic_ai"

    def __init__(self, con):
        self.con = con
        self.agent = make_agent()
        self.coverage = coverage_note(con)

    def answer(self, question: str) -> Answer | None:
        @self.agent.output_validator
        def validate(ctx: RunContext[SqlDeps], proposal: SQLProposal) -> SQLProposal:
            sql = _strip_fence(proposal.sql)
            problems = check_sql(sql, ctx.deps.question)
            if problems:
                raise ModelRetry("; ".join(problems))
            try:
                rows = ctx.deps.con.sql(sql).fetchall()
                cols = [d[0] for d in ctx.deps.con.sql(sql).description]
            except Exception as exc:
                raise ModelRetry(f"That query failed to execute: {exc}")
            problems = check_rows(rows, cols, ctx.deps.question)
            if problems:
                raise ModelRetry("; ".join(problems))
            return SQLProposal(sql=sql)

        prompt = f"Schema:\n{SCHEMA_CARD}\n\n{self.coverage}\n\nQuestion: {question}"
        try:
            result = self.agent.run_sync(prompt, deps=SqlDeps(con=self.con, question=question))
        except Exception as exc:
            return Answer(
                text="I could not produce a query I trust for that question. "
                     f"PydanticAI gave up after its retry budget: {exc}",
                lane=self.name, abstained=True)

        sql = result.output.sql
        rows = self.con.sql(sql).fetchall()
        cols = [d[0] for d in self.con.sql(sql).description]

        if not rows:
            return Answer(
                text="The data does not contain an answer to that. The query ran "
                     "successfully and returned no rows.",
                lane=self.name, sql=sql, abstained=True)

        table = _format_rows(cols, rows)
        text = (f"{table}\n\nComputed directly over the Boston open data — "
                f"validated by PydanticAI's ModelRetry loop, same guardrails as the "
                f"hand-rolled SqlLane.")
        return Answer(text=text, lane=self.name, sql=sql, rows=rows,
                      citations=[f"Analyze Boston via DuckDB ({', '.join(_tables_in(sql))})"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Same SQL lane as router.py, driven by PydanticAI.")
    ap.add_argument("question", nargs="*")
    args = ap.parse_args()
    q = " ".join(args.question).strip()
    if not q:
        raise SystemExit('Usage: python -m team.track_a_engine.router_pydantic_ai "your question"')

    con = _connect()
    lane = PydanticAISqlLane(con)
    print(f"\nQ: {q}")
    ans = lane.answer(q)
    print(f"-- lane: {ans.lane}{'  (ABSTAINED)' if ans.abstained else ''} " + "-" * 30)
    print(ans.render())


if __name__ == "__main__":
    main()
