"""Fork-clock regression tests for :class:`~lazily.seqcrdt.SeqCrdt`
(``#lzzigforkhlcpeer``).

Forking a replica has to get **two independent things** right, and getting
either one wrong silently corrupts a replica rather than raising:

1. **Carry the causal POSITION.** A fork has already observed everything the
   source holds, so its clock must not restart at zero. If it does, an ordinary
   skewed ``now`` below the source's last wall time — which is the entire
   reason hybrid logical clocks exist — mints a stamp causally *behind* state
   the fork already carries. LWW adopts only on strictly-greater, so the fork's
   own local write is silently dropped by its own register.

2. **Do NOT carry the PEER.** The peer is the stamp's final tiebreaker. Two
   replicas stamping under one peer id can mint the identical
   ``(wall, logical, peer)`` triple — and because LWW adopts only on
   strictly-greater, a tie means *neither* side adopts and the replicas diverge
   permanently, the one outcome a CRDT exists to make impossible. lazily-zig
   shipped exactly that bug while fixing (1).

``conformance/collections/seqcrdt_convergence.json`` cannot reach either
failure: every fork in the canonical corpus is followed by an op whose ``now``
EXCEEDS the source's last wall time, so the clock takes its
``now > last_wall`` branch and the logical counter resets to 0 no matter which
clock the fork started from. These tests are the reachability the corpus is
missing.
"""

from __future__ import annotations

from lazily import SeqCrdt


def test_fork_carries_the_source_clock_so_a_skewed_local_write_is_not_dropped() -> None:
    """A fork's own write must survive even when its ``now`` runs backwards.

    ``b``'s ``now`` is BELOW ``a``'s last wall time — ordinary clock skew. With
    a clock that restarted at zero, ``b`` mints ``(50, 0, 2)``, which is not
    greater than the ``(100, 0, 1)`` it already holds, and ``b``'s own write
    vanishes into ``b``'s own state.
    """
    a: SeqCrdt[int] = SeqCrdt(1)
    a.insert_back("x", 1, 100)  # value stamp (100, 0, 1)

    b = a.fork(2)
    b.set_value("x", 99, 50)  # carried clock -> (100, 1, 2), beats (100, 0, 1)

    # The fork's OWN write must survive in the fork.
    assert b.get("x") == 99

    # ...and the two replicas must agree once they exchange state.
    a.merge(b, 200)
    b.merge(a, 200)
    assert a.get("x") == b.get("x")
    assert a.get("x") == 99


def test_fork_stamps_with_its_own_peer_so_equal_wall_edits_still_converge() -> None:
    """Equal-wall writes are decided by the logical counter, then the peer.

    ::

        a@peer1  insert x @ now=10  -> (10, 0, 1)
        b = a.fork(2)               -> clock at (10, 0)
        b        set x=99 @ now=10  -> (10, 1, 2)
        a        set x=55 @ now=10  -> (10, 1, 1)

    If the fork inherited peer 1, both stamps would be ``(10, 1, 1)``, neither
    merge would adopt, and ``a=55`` / ``b=99`` would stand forever.
    """
    a: SeqCrdt[int] = SeqCrdt(1)
    a.insert_back("x", 1, 10)

    b = a.fork(2)
    b.set_value("x", 99, 10)
    a.set_value("x", 55, 10)

    a.merge(b, 20)
    b.merge(a, 20)

    # Convergence FIRST: the replicas must agree at all, before which value won
    # is even a meaningful question.
    assert a.get("x") == b.get("x")
    # And the winner is b's write, because peer 2 outranks peer 1.
    assert a.get("x") == 99


def test_clone_then_reassigning_peer_is_the_same_fork_as_fork() -> None:
    """The hand-rolled fork the conformance runner used must behave identically.

    ``clone()`` + ``replica.peer = 2`` is how ``{"fork": "b", "peer": 2}`` was
    replayed before :meth:`SeqCrdt.fork` existed, and it is the idiom already in
    the wild. It stays a real fork: the clock carries and the stamp peer follows
    the replica's current ``peer``, so this path cannot inherit the source's
    tiebreaker either.
    """
    a: SeqCrdt[int] = SeqCrdt(1)
    a.insert_back("x", 1, 100)

    b = a.clone()
    b.peer = 2
    b.set_value("x", 99, 50)

    assert b.get("x") == 99

    a.merge(b, 200)
    b.merge(a, 200)
    assert a.get("x") == b.get("x") == 99


def test_clone_keeps_the_source_peer_and_its_clock_so_the_copy_is_a_snapshot() -> None:
    """``clone`` is the one case where keeping the peer is correct.

    It is the same logical replica, not a second writer. It must still carry the
    clock, or the copy regresses exactly the way a fork would.
    """
    a: SeqCrdt[int] = SeqCrdt(1)
    a.insert_back("x", 1, 100)

    copy = a.clone()
    assert copy.peer == 1
    copy.set_value("x", 42, 50)  # skewed `now`, same peer

    assert copy.get("x") == 42
    assert copy.order() == ["x"]
