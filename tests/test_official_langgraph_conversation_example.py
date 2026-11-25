"""Wheel-friendly deterministic upstream graph and handler checks."""

from __future__ import annotations

from pathlib import Path

from tests.example_imports import load_example_file

ROOT = Path(__file__).resolve().parents[1]


def test_official_conversation_retains_upstream_graph_and_thread_state() -> None:
    # Given the independently transcribed pinned graph and the extracted hosted graph.
    proof = load_example_file(ROOT / "scripts/certify_upstream_examples.py", "_upstream_proof")
    directory = ROOT / "examples/official_langgraph_conversation"
    proof.load_file(directory / "model.py", "model")
    hosted = proof.load_file(directory / "upstream_graph.py", "upstream_graph")
    original = proof.baseline("official_langgraph_conversation")
    from langgraph.checkpoint.memory import InMemorySaver

    graph = hosted.builder.compile(checkpointer=InMemorySaver())
    # When invoking twice on one thread and once on an independent thread.
    results = []
    for thread, text in (("a", "first"), ("a", "second"), ("b", "isolated")):
        request = {"messages": [{"role": "human", "content": text}]}
        config = {"configurable": {"thread_id": thread}}
        actual = graph.invoke(request, config)
        expected = original.graph.invoke(request, config)
        assert [m.content for m in actual["messages"]] == [m.content for m in expected["messages"]]
        results.append([m.content for m in actual["messages"]])
    # Then continuity, isolation and state-aware fake responses agree with the baseline.
    assert results == [
        ["first", "observed 1 human turn(s)"],
        ["first", "observed 1 human turn(s)", "second", "observed 2 human turn(s)"],
        ["isolated", "observed 1 human turn(s)"],
    ]


def test_official_conversation_preserves_node_edges_and_schema() -> None:
    # Given the independent pinned snippet and the hosted source.
    import ast

    proof = load_example_file(ROOT / "scripts/certify_upstream_examples.py", "_upstream_ast")
    source = (ROOT / "examples/official_langgraph_conversation/upstream_graph.py").read_text()
    # When comparing executable node/schema/edge statements (imports and model wiring excluded).
    baseline = ast.parse(proof.CONVERSATION_SOURCE).body[1:]
    hosted = ast.parse(source).body[3:]
    # Then formatting and provenance can change, but graph semantics cannot.
    assert [ast.dump(node) for node in hosted] == [ast.dump(node) for node in baseline]
