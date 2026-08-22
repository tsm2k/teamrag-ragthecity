"""The router — the piece that makes this an engine instead of a pipeline.

THE ARGUMENT
------------
Four of Lab 0's nine challenge categories are questions vector search
*structurally* cannot answer, no matter how good the embeddings get:

  counting     — no chunk contains a count; the answer is a GROUP BY
  temporal     — no row states a duration; the answer is date arithmetic
  multi-source — both figures retrieve well; neither one "wins"
  graph        — the path spans document genres; similarity can't chain edges

A naive baseline sends all nine categories down one retrieval path, so it fails
those four by construction. The fix is not a better retriever. The fix is to
*classify the question first* and send it somewhere that can actually answer it.

So the top-level object here is a Router, not a Retriever. Lanes register
themselves; the router picks one, and — critically — abstains out loud when the
chosen lane comes back empty rather than letting the LLM improvise.

WHY ABSTENTION IS CODE AND NOT A PROMPT
---------------------------------------
The top rubric anchor reads "says 'I don't know' instead of guessing." Prompt
wording does not reliably produce that from an 8B model. A zero-row SQL result
does, every time. See `Answer.abstained`.

WHY "NOW" IS INJECTED, NOT HARDCODED
------------------------------------
Lab 0 Stop 2 fails because nothing tells the model what today is or what window
the data covers. `coverage_note()` reads the real min/max out of the tables at
startup, so the model is told the truth rather than a constant that rots.

Run:  python -m team.track_a_engine.router "which neighborhood files the most 311 requests?"
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = REPO_ROOT / "data" / "downloads"
DB_PATH = str(REPO_ROOT / "boston.db")
MODEL = os.environ.get("OLLAMA_MODEL", "granite3.1-dense:8b")


# --------------------------------------------------------------------------
# Answers carry their evidence. A string alone is not an answer you can defend
# on stage, and "Trust & Transparency" is a scored innovation area.
# --------------------------------------------------------------------------
@dataclass
class Answer:
    text: str
    lane: str
    citations: list[str] = field(default_factory=list)
    sql: str | None = None
    abstained: bool = False
    rows: list[tuple] = field(default_factory=list)

    def render(self) -> str:
        out = [self.text.strip()]
        if self.sql:
            out.append("\n-- how I computed it " + "-" * 38 + f"\n{self.sql.strip()}")
        if self.citations:
            out.append("\n-- sources " + "-" * 47)
            out.extend(f"  [{i}] {c}" for i, c in enumerate(self.citations, 1))
        return "\n".join(out)


OnStep = Callable[[str], None]


class Lane(Protocol):
    """A lane answers a question or declines. Declining is a valid outcome."""

    name: str

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer | None: ...


# --------------------------------------------------------------------------
# Classification. Rules first because they are free, deterministic, and
# explainable; the LLM only sees questions the rules genuinely can't place.
# Ordering matters — "how many ... since 2023" is a count, not a trend.
# --------------------------------------------------------------------------
COUNT_RE = re.compile(
    r"\b(how many|how much|count|number of|total|most|least|top|fewest|"
    r"average|median|per (?:year|month|neighborhood|district)|busiest|worst)\b", re.I)
TEMPORAL_RE = re.compile(
    r"\b(trend|over time|getting (?:better|worse)|since|between \d{4}|"
    r"year[- ]over[- ]year|how long|compared to last|rising|falling|declin)\w*\b", re.I)
GRAPH_RE = re.compile(
    r"\b(who owns|owner of|which owner|connected to|linked to|related to|"
    r"degrees? of separation|same owner|also owns?|owns? (?:other|another)|"
    r"path between|associated with)\b", re.I)
NARRATIVE_RE = re.compile(r"\b(why|describe|what happened|explain|reason|cause)\b", re.I)


def classify(question: str, llm_fallback: Callable[[str], str] | None = None) -> str:
    """Return a lane name. Rules cover the clear cases; the LLM breaks ties."""
    q = question.strip()
    # Graph first: "who owns X and how many violations" is a graph walk that
    # happens to contain a count word.
    if GRAPH_RE.search(q):
        return "graph"
    # Counting beats temporal: a count with a date filter is still a count.
    if COUNT_RE.search(q):
        return "sql"
    if TEMPORAL_RE.search(q):
        return "sql"
    if NARRATIVE_RE.search(q):
        return "narrative"
    if llm_fallback is not None:
        guess = llm_fallback(q).strip().lower()
        if guess in {"sql", "narrative", "graph"}:
            return guess
    return "narrative"


# --------------------------------------------------------------------------
# Coverage. Stated to the model AND to the reader, because half of temporal
# scoring is refusing to imply coverage you do not have.
# --------------------------------------------------------------------------
COVERAGE_SQL = {
    "requests": ("311 service requests", "open_dt"),
    "crime": ("crime incidents", "occurred_on"),
    "inspections": ("food inspections", "resultdttm"),
    "permits": ("building permits", "issued_date"),
    "crashes": ("Vision Zero crashes", "dispatch_ts"),
}


def coverage_note(con) -> str:
    """Read real min/max from the tables. Never hardcode a window."""
    lines = [f"Today is {date.today().isoformat()}."]
    for table, (label, col) in COVERAGE_SQL.items():
        try:
            lo, hi = con.sql(f'SELECT min("{col}"), max("{col}") FROM {table}').fetchone()
        except Exception:
            continue
        if lo is not None:
            lines.append(f"The {label} data covers {str(lo)[:10]} to {str(hi)[:10]}.")
    lines.append("Do not state or imply anything about dates outside these windows.")
    return " ".join(lines)


# --------------------------------------------------------------------------
# Lane 1 — SQL. Counting and temporal questions get computed, not retrieved.
# --------------------------------------------------------------------------
SCHEMA_CARD = """\
requests(case_enquiry_id, open_dt TIMESTAMP, closed_dt TIMESTAMP, is_closed BOOLEAN,
         days_to_close DOUBLE, open_year, open_month, on_time, case_status,
         closure_reason, case_title, subject, reason, type, department,
         neighborhood, zip5, latitude, longitude, source, ward)
    Boston 311 requests, 2026 year-to-date ONLY (78,526 rows).
    !! Do NOT add a date filter unless the question asks for a sub-period.
    !! 49.1% of rows are still OPEN (is_closed = false, closed_dt NULL).
       NEVER filter on closed_dt for a count -- it silently drops half the data.
       For duration questions use days_to_close WHERE is_closed, and SAY that
       open cases were excluded (their median age is far higher, so the closed-
       only figure understates real wait times).

crime(incident_number, offense_code, offense_description, district, shooting,
      occurred_on TIMESTAMP, year, month, day_of_week, hour, street, latitude, longitude)
    Boston crime, 2023-01-01 to 2026-08-15 (290,130 rows).
    !! 2026 is a PARTIAL year (through Aug 15). Comparing it raw against a full
       year shows a fake ~36% drop. For trends use full years 2023/2024/2025,
       or compare like-for-like Jan 1-Aug 15 windows.
    day_of_week values are already trimmed. Group on offense_description.

inspections(licenseno, resultdttm TIMESTAMP, businessname, address, zip5, result,
            licensecat, n_violations, n_critical, result_year)
    Food inspections at EVENT grain -- one row per actual inspection (219,732).
    !! ALWAYS use this table for counting inspections.
    !! result has 20 distinct codes, not 6. NEVER write `result IN (...)` as a
       whitelist -- it silently drops the long tail (HE_OutBus, HE_TSOP,
       HE_VolClos, Pass, HE_Closure, Fail, HE_Misc, DATAERR, ...).
    !! "failed" is NOT just HE_Fail. It spans HE_Fail (64,228), HE_FailExt
       (10,269), Fail (80), HE_FAILNOR (25), Failed (1). Match with
       lower(result) LIKE '%fail%'.

inspection_violations(businessname, licenseno, result, resultdttm, violation,
                      viol_level, violdesc, comments, address, zip5, property_id)
    The RAW file -- one row per violation cited (896,379). Counting rows here
    overstates inspections 4.08x. Use ONLY when asked about specific violations.
    violdesc is a code gloss, never a narrative cause.

permits(permitnumber, worktype, description, comments, applicant,
        declared_valuation_usd DOUBLE, total_fees_usd DOUBLE, issued_date TIMESTAMP,
        issued_year, status, occupancytype, sq_feet, address, zip5, ward,
        property_id, parcel_id, latitude, longitude)
    Boston building permits, 2006-2026 (660,716). Valuation already cast to USD.
    !! parcel_id joins to assessment.pid. property_id does NOT (0 overlap).

assessment(pid, gis_id, st_name, city, zip5, lu, lu_desc, owner, land_value,
           bldg_value, total_value, gross_tax, yr_built, overall_cond)
    Every Boston parcel (184,552). owner is a real entity name.

crashes(dispatch_ts TIMESTAMP, year, month, mode_type, location_type, street,
        xstreet1, xstreet2, latitude, longitude)
    Vision Zero crashes 2015-2026 (44,344). mode_type is 'mv', 'bike', or 'ped'.

budget(cabinet, dept, program, expense_category, fy24_actual, fy25_actual,
       fy26_appropriation, fy27_budget)
    917 line items. The grand-total row is already removed -- SUM() is safe here.
"""

# --------------------------------------------------------------------------
# THE GUARD — why free text-to-SQL is not demo-safe on its own.
#
# Measured on the first end-to-end run, all three answers were confidently
# wrong in different ways:
#   "top 311 neighborhood"  -> model added WHERE is_closed = false,
#                              returning 4,774 (open cases) instead of 9,935
#   "Dunkin inspections"    -> WHERE businessname = 'Dunkin' matched 99 of
#                              6,466; the chain spans 6+ spellings
#   "is crime rising?"      -> returned a 500-row district x month dump and
#                              never computed a trend
#
# None raised an error. That is the exact failure mode this hackathon is about.
# So the model's SQL is treated as a PROPOSAL that must clear static checks and
# result-shape checks before it is allowed to become an answer.
# --------------------------------------------------------------------------
NAME_COLS = ("businessname", "owner", "applicant", "street", "st_name", "address")
DURATION_RE = re.compile(r"\b(how long|days to close|duration|time to (?:close|resolve)|"
                         r"still open|outstanding|backlog)\b", re.I)
TREND_RE = re.compile(r"\b(trend|getting (?:better|worse)|rising|falling|over the last|"
                      r"year[- ]over[- ]year|compared to)\b", re.I)
SUPERLATIVE_RE = re.compile(r"\b(most|least|top|worst|busiest|highest|lowest|fewest)\b", re.I)


def check_sql(sql: str, question: str) -> list[str]:
    """Static checks on the proposed SQL. Returns complaints, empty if clean."""
    low, bad = sql.lower(), []

    # 1. The open/closed trap. Filtering a COUNT on closure status silently
    #    halves 311 -- unless the question is actually about duration/backlog.
    if not DURATION_RE.search(question):
        if re.search(r"where[^;]*\b(is_closed|closed_dt)\b", low, re.S):
            bad.append("Remove the is_closed/closed_dt filter. 49.1% of 311 rows are "
                       "still open; filtering on closure status answers a different "
                       "question and halves the count.")

    # 2. Exact equality on a messy name column. City data spells one entity
    #    many ways ('Dunkin', 'Dunkin Donuts', "DUNKIN'", ...).
    for col in NAME_COLS:
        if re.search(rf"\b{col}\s*=\s*'", low):
            bad.append(f"Do not use {col} = '...'. Names have many spellings in this "
                       f"data. Use lower({col}) LIKE '%...%' instead.")
            break
        # 2c. A case-sensitive LIKE looks like it fixed the equality trap but
        #     didn't: 'Dunkin' LIKE misses 'DUNKIN'' and lowercase variants.
        #     Measured: `businessname LIKE '%Dunkin%'` returns 4,313 against a
        #     verified 6,466 -- a 34% undercount that raises no error.
        if re.search(rf"\b{col}\s+(?:not\s+)?like\s*'", low) and f"lower({col})" not in low:
            bad.append(f"Wrap {col} in lower(...) before LIKE, e.g. lower({col}) LIKE "
                       f"'%...%'. Names in this data appear in mixed case (\"DUNKIN'\", "
                       f"\"Dunkin Donuts\", \"dunkin\"), and a case-sensitive LIKE silently "
                       f"misses most of them.")
            break

    # 2b. A result whitelist drops the long tail of status codes.
    if re.search(r"result\s+in\s*\(", low):
        bad.append("Do not filter with `result IN (...)`. There are 20 result codes and a "
                   "whitelist silently drops rows. Omit the filter, or for failures use "
                   "lower(result) LIKE '%fail%'.")

    # 3. A trend question must return one row per period, not a raw dump.
    if TREND_RE.search(question):
        if "group by" not in low:
            bad.append("A trend question must aggregate. GROUP BY the time period.")
        else:
            grouped = low.split("group by", 1)[1].split("order by")[0]
            if "year" not in grouped:
                bad.append("GROUP BY year so the trend is readable. One row per year.")
            extra = [d for d in ("district", "neighborhood", "street", "month")
                     if re.search(rf"\b{d}\b", grouped)]
            if extra:
                bad.append(f"Remove {', '.join(extra)} from the GROUP BY. A trend needs "
                           f"one row per year, not a breakdown -- grouping by "
                           f"{extra[0]} turns the answer into a dump.")
        # 2026 is partial (through Aug 15). Only complain if it is NOT excluded.
        # An earlier version of this check was inverted and rejected correct
        # queries that had already filtered it out -- hence the explicit test.
        excluded = bool(
            re.search(r"year\s*(<\s*2026|<=\s*2025)", low)
            or re.search(r"year\s+between\s+\d{4}\s+and\s+202[0-5]", low)
            or re.search(r"year\s*(!=|<>)\s*2026", low)
            or re.search(r"occurred_on\s*<\s*'2026-01-01'", low)
            or (re.search(r"year\s+in\s*\(([^)]*)\)", low)
                and "2026" not in (re.search(r"year\s+in\s*\(([^)]*)\)", low).group(1)))
        )
        if not excluded:
            bad.append("2026 is a PARTIAL year (through Aug 15). Exclude it "
                       "(e.g. WHERE year BETWEEN 2023 AND 2025) or compare like-for-like "
                       "Jan 1-Aug 15 windows -- never compare partial 2026 to a full year.")
    return bad


def check_rows(rows: list, cols: list[str], question: str) -> list[str]:
    """Result-shape checks — the checks that cannot be fooled.

    Static checks on SQL text are brittle: three successive regex rules here
    were each defeated by a query that LOOKED compliant (`occurred_on <
    '2026-08-16'` reads like it excludes 2026 and does not). Inspecting the rows
    that actually came back is the check that holds.
    """
    bad = []

    # A partial year sitting next to full years is the temporal trap itself.
    # The current calendar year is partial for every dataset, by definition.
    if TREND_RE.search(question) and "year" in [c.lower() for c in cols]:
        idx = [c.lower() for c in cols].index("year")
        years = {r[idx] for r in rows if isinstance(r[idx], (int, float))}
        this_year = date.today().year
        if this_year in years and len(years) > 1:
            bad.append(f"The result includes {this_year}, which is a PARTIAL year and is "
                       f"listed beside full years -- that is exactly the comparison that "
                       f"reads as a sharp fake decline. Exclude it with "
                       f"WHERE year < {this_year}.")
    if SUPERLATIVE_RE.search(question) and len(rows) > 25:
        bad.append(f"That returned {len(rows)} rows. A 'most/top/worst' question needs "
                   f"an ordered, LIMITed answer -- add ORDER BY <count> DESC LIMIT 10.")
    if TREND_RE.search(question) and len(rows) > 12:
        bad.append(f"That returned {len(rows)} rows for a trend question. Return one "
                   f"row per year so the direction is readable.")
    return bad


SQL_PROMPT = """You write DuckDB SQL. Output ONLY the query, no prose, no markdown fence.

Schema:
{schema}

{coverage}

Rules:
- Return few rows (LIMIT 10 unless the question demands more).
- Never use a column not listed above.
- For "top"/"most" questions, ORDER BY the count DESC.
- If the question cannot be answered from this schema, output exactly: CANNOT_ANSWER

Question: {question}
SQL:"""


class SqlLane:
    """Text-to-SQL over DuckDB, with execution and one repair attempt.

    Retrieval fetches text; counting is computation. This lane is why the
    engine gets aggregation questions right where the baseline guesses.
    """

    name = "sql"

    def __init__(self, con, llm: Callable[[str], str] | None):
        self.con = con
        self.llm = llm
        self.coverage = coverage_note(con)

    def _run(self, sql: str):
        return self.con.sql(sql).fetchall(), [d[0] for d in self.con.sql(sql).description]

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer | None:
        step = on_step or (lambda _msg: None)
        if self.llm is None:
            return None
        step("Asking the model to propose SQL (attempt 1)…")
        prompt = SQL_PROMPT.format(schema=SCHEMA_CARD, coverage=self.coverage, question=question)
        sql = _strip_fence(self.llm(prompt))
        if "CANNOT_ANSWER" in sql.upper():
            step("Model says this schema cannot answer the question.")
            return None
        step(f"Proposal 1:\n{sql}")

        rows: list = []
        cols: list[str] = []
        complaints: list[str] = []
        # Up to 3 proposals. Each rejection tells the model exactly what was
        # wrong, which is far more effective than restating the schema.
        for attempt in range(3):
            complaints = check_sql(sql, question)
            if complaints:
                step("Rejected by static checks:\n" + "\n".join(f"- {c}" for c in complaints))
            else:
                step("Passed static checks — running the query…")
                try:
                    rows, cols = self._run(sql)
                except Exception as exc:
                    complaints = [f"That query failed to execute: {exc}"]
                    step(f"Execution failed: {exc}")
                else:
                    complaints = check_rows(rows, cols, question)
                    if complaints:
                        step("Rejected by result-shape checks:\n" + "\n".join(f"- {c}" for c in complaints))
                    else:
                        step(f"Accepted — {len(rows)} row(s) returned.")
                        break
            if attempt == 2:
                break
            step(f"Asking for a corrected query (attempt {attempt + 2})…")
            sql = _strip_fence(self.llm(
                f"{prompt}\n\nYour previous query was REJECTED:\n{sql}\n\n"
                + "\n".join(f"- {c}" for c in complaints)
                + "\n\nReturn a corrected DuckDB query, nothing else.\nSQL:"))
            step(f"Proposal {attempt + 2}:\n{sql}")

        if complaints:
            return Answer(
                text="I could not produce a query I trust for that question. "
                     "Rather than show you a confident wrong number, here is what "
                     "kept failing:\n  - " + "\n  - ".join(complaints),
                lane=self.name, sql=sql, abstained=True)

        # ABSTENTION AS A CODE PATH. Zero rows means the data does not say --
        # it does not mean "improvise something plausible".
        if not rows:
            return Answer(
                text="The data does not contain an answer to that. The query ran "
                     "successfully and returned no rows.",
                lane=self.name, sql=sql, abstained=True)

        table = _format_rows(cols, rows)
        text = (f"{table}\n\nComputed directly over the Boston open data — "
                f"this is an exact count, not a retrieved estimate.")
        return Answer(text=text, lane=self.name, sql=sql, rows=rows,
                      citations=[f"Analyze Boston via DuckDB ({', '.join(_tables_in(sql))})"])


# --------------------------------------------------------------------------
# Lane 0 — GOLDEN QUERIES. Tried before text-to-SQL, and the reason the engine
# is trustworthy rather than merely clever.
#
# Measured: granite3.1-dense:8b at temperature 0 regenerates the SAME flawed
# query on every repair attempt, so the guard can only ever force an ABSTAIN
# on the crime-trend question -- never a correct answer. The loop cannot fix a
# model that will not vary.
#
# So the shapes we expect are answered by hand-verified SQL with a parameter
# slot. The LLM is not in the path at all. Free text-to-SQL stays as the
# fallback for everything unanticipated, where the guard still applies.
# --------------------------------------------------------------------------
GOLDEN: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(neighborhood|neighbourhood|area|part of (?:the )?city)\b.*"
                r"\b(most|top|highest|busiest|rank)\b|"
                r"\b(most|top|highest|busiest)\b.*\bneighborhood", re.I | re.S),
     "Top Boston neighborhoods by 311 volume, 2026 year-to-date. All cases, open "
     "and closed -- filtering on closure status would halve these numbers.",
     """SELECT neighborhood, count(*) AS requests,
               mode(type) AS most_common_type
        FROM requests WHERE neighborhood IS NOT NULL
        GROUP BY neighborhood ORDER BY requests DESC LIMIT 10"""),

    (re.compile(r"\bcrime\b.*\b(better|worse|trend|rising|falling|over the last|"
                r"year[- ]over[- ]year)\b|"
                r"\b(better|worse|trend|rising|falling)\b.*\bcrime\b", re.I | re.S),
     "Boston crime by FULL calendar year. 2026 is excluded because it only runs "
     "through Aug 15 -- including it shows a fake ~36% decline that is purely a "
     "calendar artifact.",
     """SELECT year, count(*) AS incidents
        FROM crime WHERE year < year(current_date)
        GROUP BY year ORDER BY year"""),

    (re.compile(r"\b(inspections?|violations?|health|restaurants?|"
                r"fail(?:ed|s|ure|ures)?)\b", re.I),
     "Food inspections at event grain (one row per inspection, not per violation). "
     "Name matched loosely -- chains are spelled many ways.",
     """SELECT count(*) AS inspections,
               sum(CASE WHEN lower(result) LIKE '%fail%' THEN 1 ELSE 0 END) AS failed,
               count(DISTINCT businessname) AS name_variants
        FROM inspections WHERE lower(businessname) LIKE '%{entity}%'"""),

    (re.compile(r"\b(how long|days? to close|time to (?:close|resolve)|backlog)\b", re.I),
     "311 closure time. Reported on closed cases AND on the still-open backlog, "
     "because the closed-only median alone understates real waits by ~18x.",
     """SELECT median(days_to_close) FILTER (WHERE is_closed) AS median_days_closed,
               count(*) FILTER (WHERE NOT is_closed) AS still_open,
               median(date_diff('day', open_dt, current_timestamp))
                   FILTER (WHERE NOT is_closed) AS median_age_open
        FROM requests"""),
]

ENTITY_RE = re.compile(r"\b(?:did|for|at|by)\s+([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*)*)", re.S)


class GoldenLane:
    """Verified queries for anticipated question shapes. No LLM in the path."""

    name = "golden"

    def __init__(self, con):
        self.con = con

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer | None:
        step = on_step or (lambda _msg: None)
        step("Checking hand-verified golden queries…")
        for pattern, note, sql in GOLDEN:
            if not pattern.search(question):
                continue
            if "{entity}" in sql:
                m = ENTITY_RE.search(question)
                if not m:
                    continue
                sql = sql.replace("{entity}", m.group(1).lower().split()[0].strip("'\""))
            step(f"Matched a golden query pattern — running:\n{sql}")
            try:
                rows = self.con.sql(sql).fetchall()
                cols = [d[0] for d in self.con.sql(sql).description]
            except Exception:
                continue
            if not rows or all(v is None for v in rows[0]):
                return Answer(text="The data does not contain an answer to that.",
                              lane=self.name, sql=sql, abstained=True)
            return Answer(text=f"{_format_rows(cols, rows)}\n\n{note}",
                          lane=self.name, sql=sql, rows=rows,
                          citations=[f"Analyze Boston via DuckDB "
                                     f"({', '.join(_tables_in(sql))}) — verified query"])
        return None


# --------------------------------------------------------------------------
# Lane 2 — narrative retrieval. Only for questions that genuinely want prose.
# --------------------------------------------------------------------------
class NarrativeLane:
    """Dense retrieval over the handful of columns that are actually prose.

    Deliberately narrow: DATA_NOTES.md section 7.3 shows that embedding wide
    CSV rows produces chunks dominated by shared column names, which is exactly
    the "similarity is not identity" failure the baseline demonstrates.
    """

    name = "narrative"

    def __init__(self, collection, llm: Callable[[str], str] | None, k: int = 5):
        self.coll = collection
        self.llm = llm
        self.k = k

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer | None:
        step = on_step or (lambda _msg: None)
        if self.coll is None:
            return None
        step(f"Searching the narrative index for the top {self.k} matches…")
        res = self.coll.query(query_texts=[question], n_results=self.k)
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = (res.get("distances") or [[None] * len(docs)])[0]
        if not docs:
            step("No documents indexed.")
            return Answer(text="Nothing in the indexed text addresses that.",
                          lane=self.name, abstained=True)

        # A weak best match is a reason to abstain, not to answer confidently.
        if dists[0] is not None and dists[0] > 1.15:
            step(f"Best match distance {dists[0]:.2f} — too weak, abstaining.")
            return Answer(
                text="Nothing in the indexed text is a close enough match to answer that "
                     f"confidently (best distance {dists[0]:.2f}). I would rather say so "
                     "than guess.",
                lane=self.name, abstained=True)

        cites = [f"{m.get('dataset','?')} row {m.get('row_id','?')}" for m in metas]
        best_dist = f"{dists[0]:.2f}" if dists[0] is not None else "n/a"
        step(f"Found {len(docs)} matches (best distance {best_dist}).")
        if self.llm is None:
            return Answer(text="\n\n".join(docs[:3]), lane=self.name, citations=cites)

        step("Asking the model to answer from the retrieved context…")
        ctx = "\n\n---\n\n".join(f"[{c}]\n{d}" for c, d in zip(cites, docs))
        text = self.llm(
            "Answer using ONLY the context. If the context does not contain the answer, "
            "say so plainly and stop — do not speculate about causes that are not written "
            f"down.\n\nContext:\n{ctx}\n\nQuestion: {question}\nAnswer:")
        return Answer(text=text, lane=self.name, citations=cites)


# --------------------------------------------------------------------------
# Lane 3 — graph. Delegates to boston.graph when it is available.
# --------------------------------------------------------------------------
class GraphLane:
    """Entity-graph walks. The graph is built lazily — it is the one lane with
    a real startup cost, so questions that never reach it never pay for it."""

    name = "graph"

    def __init__(self):
        self._cg = None
        self._tried = False

    def _graph(self):
        if not self._tried:
            self._tried = True
            try:
                from team.boston.graph import build_graph
                self._cg = build_graph(verbose=False)
            except Exception as exc:
                print(f"[!] graph lane unavailable ({type(exc).__name__}: {exc})",
                      file=sys.stderr)
        return self._cg

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer | None:
        step = on_step or (lambda _msg: None)
        step("Building/loading the entity graph (one-time cost)…")
        cg = self._graph()
        if cg is None:
            step("Graph unavailable.")
            return None
        step("Walking the graph for a path…")
        try:
            from team.boston.graph import graph_answer
            text, path, cites = graph_answer(question, cg)
        except Exception as exc:
            print(f"[!] graph lane failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            step(f"Graph walk failed: {exc}")
            return None
        if not text:
            return None
        step("Found a path — formatting the answer.")
        return Answer(text=text, lane=self.name, citations=list(cites))


# --------------------------------------------------------------------------
class Router:
    def __init__(self, lanes: dict[str, Lane], llm=None):
        self.lanes = lanes
        self.llm = llm

    def answer(self, question: str, on_step: OnStep | None = None) -> Answer:
        step = on_step or (lambda _msg: None)
        want = classify(question, llm_fallback=None)
        step(f"Classified as: {want}")
        order = ["golden", want] + [n for n in ("sql", "graph", "narrative") if n != want]
        for name in order:
            lane = self.lanes.get(name)
            if lane is None:
                continue
            step(f"Trying lane: {name}")
            got = lane.answer(question, on_step=on_step)
            if got is not None:
                # golden is tried first by design, so it is never a "fallback".
                if name in ("golden", want):
                    got.lane = name
                else:
                    got.lane = f"{name} (fell back from {want})"
                return got
        return Answer(text="No lane could answer that.", lane="none", abstained=True)


# --------------------------------------------------------------------------
def _strip_fence(s: str) -> str:
    s = re.sub(r"^```(?:sql)?|```$", "", s.strip(), flags=re.M).strip()
    return s.rstrip(";")


def _tables_in(sql: str) -> list[str]:
    return sorted(set(re.findall(
        r"\b(requests|crime|permits|crashes|inspections|inspection_violations|"
        r"assessment|budget)\b", sql, re.I))) or ["boston"]


def _format_rows(cols: list[str], rows: list[tuple], limit: int = 10) -> str:
    rows = rows[:limit]
    w = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    head = "  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols))
    body = ["  ".join(str(r[i]).ljust(w[i]) for i in range(len(cols))) for r in rows]
    return "\n".join([head, "  ".join("-" * x for x in w), *body])


def make_llm(model: str = MODEL):
    """One place that knows about Ollama, so lanes stay testable without it."""
    try:
        from langchain_ollama import ChatOllama
        chat = ChatOllama(model=model, temperature=0)
        return lambda p: chat.invoke(p).content
    except Exception as exc:
        print(f"[!] Ollama unavailable ({type(exc).__name__}: {exc}) — SQL lane disabled.",
              file=sys.stderr)
        return None


def build(with_llm: bool = True) -> Router:
    con = _connect()
    llm = make_llm() if with_llm else None
    coll = None
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(REPO_ROOT / ".chroma" / "engine"))
        coll = client.get_or_create_collection("narrative")
        if coll.count() == 0:
            coll = None  # nothing indexed yet; let the lane decline
    except Exception:
        pass
    return Router({"golden": GoldenLane(con), "sql": SqlLane(con, llm),
                   "narrative": NarrativeLane(coll, llm), "graph": GraphLane()}, llm=llm)


def _connect():
    """Prefer boston.duck: it sets TimeZone=UTC and builds the typed tables.

    That TZ line is not cosmetic. permits/crime/inspections/crashes stamp '+00'
    but the wall-clock values are Boston LOCAL time, so a session in
    America/New_York slides every timestamp back 4-5 hours and pushes 318 crime
    rows across a year boundary. Without it, two teammates on differently
    configured laptops get different answers from identical code.
    """
    try:
        from team.boston import duck
        return duck.connect()
    except Exception as exc:  # fall back so the router still runs standalone
        import duckdb
        print(f"[!] team.boston.duck unavailable ({type(exc).__name__}) — "
              f"opening {DB_PATH} directly.", file=sys.stderr)
        con = duckdb.connect(DB_PATH)
        con.execute("SET TimeZone='UTC'")
        return con


def main() -> None:
    ap = argparse.ArgumentParser(description="Route one question to the lane that can answer it.")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--no-llm", action="store_true", help="rules + retrieval only")
    ap.add_argument("--explain", action="store_true", help="show the classification only")
    args = ap.parse_args()
    q = " ".join(args.question).strip()
    if not q:
        raise SystemExit('Usage: python -m team.track_a_engine.router "your question"')
    if args.explain:
        print(f"{q!r} -> lane {classify(q)!r}")
        return
    router = build(with_llm=not args.no_llm)
    print(f"\nQ: {q}")
    ans = router.answer(q)
    print(f"-- lane: {ans.lane}{'  (ABSTAINED)' if ans.abstained else ''} " + "-" * 30)
    print(ans.render())


if __name__ == "__main__":
    main()
