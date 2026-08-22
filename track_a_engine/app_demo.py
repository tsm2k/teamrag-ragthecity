"""Track A demo page: shows the router's thinking live, not just the final answer.

Track A is judged on RAG architecture, not UI polish (that's Track B) — but the
2-minute demo video still needs something on screen, and "Trust & Transparency"
is a scored anchor. This page exists to make the router's own reasoning
visible: classification, which lane was tried, each SQL proposal, why a
proposal was rejected, and the corrected retry — as it happens, not just the
final number.

Run:  .venv/bin/streamlit run team/track_a_engine/app_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # so `team.track_a_engine` imports under `streamlit run`

from team.track_a_engine.router import build  # noqa: E402

st.set_page_config(page_title="RAG the City — Track A Engine", page_icon=":gear:")
st.title("RAG the City — Track A Engine")
st.caption("Route, don't retrieve. Every step below is real — nothing is staged for the demo.")


@st.cache_resource
def get_router():
    return build(with_llm=True)


if "history" not in st.session_state:
    st.session_state.history = []

for turn in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(turn["question"])
    with st.chat_message("assistant"):
        with st.status(f"Lane: {turn['lane']}", state="complete", expanded=False):
            for event in turn["events"]:
                st.text(event)
        st.code(turn["answer"], language=None)
        for cite in turn["citations"]:
            st.info(cite, icon=":material/dataset:")

if question := st.chat_input("e.g. how many Dunkin inspections were there?"):
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        events: list[str] = []
        with st.status("Thinking…", expanded=True) as status:
            def on_step(msg: str) -> None:
                events.append(msg)
                status.write(msg)

            router = get_router()
            ans = router.answer(question, on_step=on_step)
            status.update(label=f"Lane: {ans.lane}", state="complete", expanded=False)

        if ans.abstained:
            st.success("Abstained — the honest answer, not a guess.", icon=":material/verified:")
        st.code(ans.render(), language=None)
        for cite in ans.citations:
            st.info(cite, icon=":material/dataset:")

    st.session_state.history.append({
        "question": question,
        "lane": ans.lane,
        "events": events,
        "answer": ans.render(),
        "citations": ans.citations,
    })
