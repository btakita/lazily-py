"""Cross-language conformance for membership + failure detection (``#lzmemb``).

Replays the SWIM lifecycle fixture from
``lazily-spec/conformance/membership`` through the lazily-py
:class:`~lazily.membership.MembershipCell`, asserting the spec's
language-agnostic expectations. Each op asserts the acted peers' ``state``, the
``alive_set`` (the reactive peer set), and that the peer-set reader invalidates
exactly when the alive set changes (via :meth:`~lazily.slot.Slot.is_in`).

The same fixture is replayed by the Rust, Zig, Kotlin, Go, C++, and JS bindings,
so all implementations stay byte-compatible on the compute invariants — the phi
transitions in particular.
"""

from __future__ import annotations

import json
from typing import Any

from conformance_assert import (
    assert_key,
    assert_key_with,
    corpus_subdir,
    instrument,
    sub_entries,
)

from lazily import Slot
from lazily.membership import (
    MembershipCell,
    MembershipConfig,
)


_SPEC = corpus_subdir("membership")


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"membership/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "membership_lifecycle.json").exists()


def _build_config(cfg: dict) -> MembershipConfig:
    return MembershipConfig(
        phi_threshold=float(cfg["phi_threshold"]),
        suspect_timeout=int(cfg["suspect_timeout"]),
        max_samples=int(cfg["max_samples"]),
        min_std=float(cfg["min_std"]),
    )


def _run_fixture(fixture: dict) -> None:
    ctx: dict = {}
    config = _build_config(fixture["config"])
    m: MembershipCell = MembershipCell(ctx, config)

    # Wrap the alive-set reader in an observer Slot; ``is_in(ctx)`` reports
    # whether the cached value survived the last op (cached ⇒ not invalidated).
    observed: Slot = Slot(callable=lambda _ctx: m.peer_set(_ctx))
    observed(ctx)  # materialize the cache

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        op_type = op["type"]
        now = int(op["now"])

        if op_type == "join":
            m.join(op["peer"], now)
        elif op_type == "heartbeat":
            m.heartbeat(op["peer"], now)
        elif op_type == "leave":
            m.leave(op["peer"], now)
        elif op_type == "tick":
            m.tick(now)
        else:
            raise AssertionError(f"step {i}: unknown op type: {op_type}")

        expected = step["expected"]

        # Per-peer state. DESCEND (#lzsubblockkeyset): a child tracker owns the
        # map's key set, so a peer the corpus adds fails as unconsumed rather
        # than being compared by nothing.
        for peer, want_state in sub_entries(expected, "states", where=f"step {i}"):
            state = m.state(_coerce_peer(peer))
            got = state.value if state is not None else None
            assert got == want_state, (
                f"step {i}: peer {peer!r} state is {got!r}, expected {want_state!r}"
            )

        # Alive set.
        assert_key_with(
            expected,
            "alive_set",
            lambda want: m.peer_set() == {_coerce_peer(p) for p in want},
            where=f"step {i}",
        )

        # Peer-set invalidation: cached ⇒ not invalidated.
        was_cached = observed.is_in(ctx)
        assert_key(expected, "invalidates", not was_cached, where=f"step {i}")
        observed(ctx)  # re-materialize for the next step


def _coerce_peer(peer: Any) -> Any:
    """Fixture peer ids are JSON numbers (object keys arrive as strings)."""
    if isinstance(peer, str):
        return int(peer)
    return peer


def test_membership_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fixture = _load("membership_lifecycle.json")
    _run_fixture(fixture)
