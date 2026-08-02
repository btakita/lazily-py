"""Lossless full-document tree CRDT — ``LosslessTreeCrdt`` (M1 core, #lzlosstree).

The Python port of ``lazily-rs::lossless_tree_crdt`` (and a structural twin of
the JS / Kotlin bindings). Where :class:`~lazily.textcrdt.TextCrdt` is a *flat*
lossless floor and :class:`~lazily.seqcrdt.SeqCrdt` orders opaque keyed
siblings, this is a **single rooted concrete-syntax tree** whose *leaves own
every rendered byte*. The guiding invariant is losslessness —
``render(tree) == source_text`` for valid, invalid, and unknown source alike —
so the tree itself can be the wire authority instead of a semantic AST over a
separate text floor. Internal element nodes own *structure only*; all text
lives in leaf nodes tagged Token / Trivia / Raw / Error, so unknown/invalid
spans round-trip exactly as Raw/Error leaves rather than being discarded.

M1 scope: create / tombstone / intra-parent reorder / leaf-edit / split-leaf /
merge-adjacent-leaves, plus op-based delta sync over a **dotted, non-contiguous
version frontier**. Positions and seed text travel inside ops so both replicas
store byte-identical keys and converge. Leaf text embeds
:class:`~lazily.textcrdt.TextCrdt` wholesale; child order is a minimal
fractional index (:func:`key_between`, mirroring SeqCrdt); the clock is a
Lamport :class:`TreeOpId`. Leaf-local wire offsets are UTF-8 bytes, converted
through :func:`byte_to_char`.

The dotted frontier is a dot *set* (contiguous prefix plus sparse holes),
never a per-peer max: a missing non-contiguous op stays representable and
re-requestable. This is the property a version-vector shortcut cannot provide
(proven in ``LazilyFormal.LosslessTreeSync`` — ``frontier_no_skip`` /
``perPeerMax_skips``). The conformance fixtures in
``lazily-spec/conformance/lossless-tree`` are the cross-binding test contract.
"""

from __future__ import annotations


__all__ = [
    "LEAF_KIND_FROM_WIRE",
    "ROOT",
    "DotRange",
    "LeafKind",
    "LosslessTreeCrdt",
    "NodeSeed",
    "SeedElement",
    "SeedLeaf",
    "SortKey",
    "TreeError",
    "TreeNodeId",
    "TreeOp",
    "TreeOpId",
    "TreeOpKind",
    "TreeUpdate",
    "TreeVersionFrontier",
    "byte_to_char",
    "key_between",
    "tree_update_from_wire",
    "tree_update_to_wire",
    "tree_version_frontier_from_wire",
    "tree_version_frontier_to_wire",
]


from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .textcrdt import ROOT as TEXT_ROOT
from .textcrdt import OpId, TextCrdt, TextOp


class LeafKind(Enum):
    """Classification of a leaf's exact source span (PascalCase = wire form).

    Every rendered byte belongs to exactly one leaf; unknown/invalid spans are
    Raw/Error so nothing is ever discarded.
    """

    TOKEN = "Token"
    TRIVIA = "Trivia"
    RAW = "Raw"
    ERROR = "Error"

    @classmethod
    def from_wire(cls, value: str) -> LeafKind:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"unknown leaf kind: {value!r} (expected one of Token/Trivia/Raw/Error)"
            ) from exc


# Lowercase fixture kind -> enum (the conformance seeds use lowercase kinds).
LEAF_KIND_FROM_WIRE = {
    "token": LeafKind.TOKEN,
    "trivia": LeafKind.TRIVIA,
    "raw": LeafKind.RAW,
    "error": LeafKind.ERROR,
}


class TreeError(Exception):
    """A tree mutation was rejected — text preservation always wins.

    Mirrors ``lazily-rs::lossless_tree_crdt::TreeError``. A mutation is
    rejected (never silently drops bytes) when its target is absent, not a leaf,
    off a UTF-8 char boundary, or not an adjacent sibling.
    """


@dataclass(frozen=True)
class TreeOpId:
    """A dotted, totally-ordered operation id — Lamport ``(counter, peer)``.

    Also the transparent wire form of a :class:`TreeNodeId` (a node's id is the
    id of the ``CreateNode`` op that materialized it). The document root is
    :data:`ROOT` = ``(0, 0)``. The total order is lexicographic
    ``(counter, peer)``; the clock advances past every *observed* op so a
    causally-later op sorts higher (last-writer-wins) and concurrent ops
    tiebreak by peer.
    """

    counter: int
    peer: int

    def __lt__(self, other: TreeOpId) -> bool:
        return (self.counter, self.peer) < (other.counter, other.peer)

    def __le__(self, other: TreeOpId) -> bool:
        return (self.counter, self.peer) <= (other.counter, other.peer)

    def to_dict(self) -> dict[str, int]:
        return {"counter": self.counter, "peer": self.peer}

    @classmethod
    def from_dict(cls, d: Any) -> TreeOpId:
        return cls(counter=int(d["counter"]), peer=int(d["peer"]))


# A node id is structurally an op id (the create op's id). Kept as a distinct
# alias for readability; the wire form is the bare op id (newtype-transparent).
TreeNodeId = TreeOpId

# The sentinel id of the document root.
ROOT = TreeOpId(0, 0)


def _min_id(a: TreeOpId, b: TreeOpId) -> TreeOpId:
    return a if a <= b else b


@dataclass(frozen=True)
class SortKey:
    """A fractional-index child position: orderable bytes tiebroken by peer.

    ``frac`` is stored as a tuple of u8 internally; the wire form is a JSON
    array of ints (never base64).
    """

    frac: tuple[int, ...]
    peer: int

    def to_dict(self) -> dict[str, Any]:
        return {"frac": list(self.frac), "peer": self.peer}

    @classmethod
    def from_dict(cls, d: Any) -> SortKey:
        return cls(frac=tuple(int(x) for x in d["frac"]), peer=int(d["peer"]))


def _cmp_sort(a: SortKey, b: SortKey) -> int:
    """Lexicographic compare of two sort keys (frac bytes, then peer)."""
    n = min(len(a.frac), len(b.frac))
    for i in range(n):
        if a.frac[i] != b.frac[i]:
            return -1 if a.frac[i] < b.frac[i] else 1
    if len(a.frac) != len(b.frac):
        return -1 if len(a.frac) < len(b.frac) else 1
    return -1 if a.peer < b.peer else (1 if a.peer > b.peer else 0)


def key_between(
    lo: tuple[int, ...] | None, hi: tuple[int, ...] | None
) -> tuple[int, ...]:
    """A fractional key strictly between ``lo`` and ``hi`` (``None`` = open end).

    Byte-identical to ``lazily-rs::lossless_tree_crdt::key_between`` and the
    JS / Kotlin ports, so concurrent inserts from two replicas (and
    cross-language inserts) land at identical sort keys.
    """
    result: list[int] = []
    i = 0
    cap = (len(lo) if lo else 0) + (len(hi) if hi else 0) + 2
    while i <= cap:
        a = lo[i] if (lo is not None and i < len(lo)) else 0
        b = (
            (hi[i] if (hi is not None and i < len(hi)) else 0)
            if hi is not None
            else 256
        )
        if a + 1 < b:
            result.append((a + b) // 2)
            return tuple(result)
        result.append(a)
        i += 1
        if a < b:
            lo_tail = lo[i:] if (lo is not None and i <= len(lo)) else ()
            result.extend(key_between(lo_tail, None))
            return tuple(result)
    result.append(128)
    return tuple(result)


def byte_to_char(text: str, byte_offset: int) -> int | None:
    """Convert a leaf-local UTF-8 byte offset to a code-point index.

    Returns ``None`` when ``byte_offset`` is out of range or does not land on a
    UTF-8 character boundary. The wire and API text offsets are UTF-8 bytes;
    the embedded text CRDT is char-indexed, so the conversion happens only at
    the two byte-taking mutators (:meth:`LosslessTreeCrdt.edit_leaf` and
    :meth:`LosslessTreeCrdt.split_leaf`). No binding may treat UTF-16 code
    units as wire offsets.
    """
    if byte_offset < 0:
        return None
    encoded = text.encode("utf-8")
    if byte_offset > len(encoded):
        return None
    try:
        return len(encoded[:byte_offset].decode("utf-8"))
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# Node seeds and op vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedElement:
    """An element shell seed — structure only, owns no text."""

    kind: str


@dataclass(frozen=True)
class SeedLeaf:
    """A text leaf seed — exact initial text both replicas rebuild from."""

    kind: LeafKind
    text: str


NodeSeed = SeedElement | SeedLeaf


@dataclass(frozen=True)
class CreateNode:
    TAG = "CreateNode"

    id: TreeOpId
    parent: TreeOpId
    sort: SortKey
    seed: NodeSeed


@dataclass(frozen=True)
class Tombstone:
    TAG = "Tombstone"

    node: TreeOpId


@dataclass(frozen=True)
class Reorder:
    TAG = "Reorder"

    node: TreeOpId
    sort: SortKey


@dataclass(frozen=True)
class LeafEdit:
    TAG = "LeafEdit"

    node: TreeOpId
    prev: TreeOpId
    ops: list[TextOp]


@dataclass(frozen=True)
class SplitLeaf:
    TAG = "SplitLeaf"

    node: TreeOpId
    new: TreeOpId
    sort: SortKey
    at_char: int
    prev: TreeOpId


@dataclass(frozen=True)
class MergeLeaves:
    TAG = "MergeLeaves"

    left: TreeOpId
    right: TreeOpId
    prev_left: TreeOpId
    prev_right: TreeOpId


TreeOpKind = CreateNode | Tombstone | Reorder | LeafEdit | SplitLeaf | MergeLeaves


@dataclass(frozen=True)
class TreeOp:
    """A transport-ready tree operation: its dotted id plus the change."""

    id: TreeOpId
    kind: TreeOpKind


@dataclass(frozen=True)
class TreeUpdate:
    """An ordered batch of tree ops — the diff / gossip payload."""

    ops: list[TreeOp] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dotted version frontier
# ---------------------------------------------------------------------------


class DotRange:
    """Observed dots for one peer: a contiguous prefix plus out-of-order holes.

    Never a per-peer max — a hole above ``contiguous`` stays representable in
    ``sparse`` so it is re-requested rather than skipped.
    """

    __slots__ = ("contiguous", "sparse")

    def __init__(self) -> None:
        self.contiguous: int = 0
        self.sparse: set[int] = set()

    def contains(self, counter: int) -> bool:
        return counter <= self.contiguous or counter in self.sparse

    def observe(self, counter: int) -> None:
        if counter <= self.contiguous:
            return
        self.sparse.add(counter)
        while (self.contiguous + 1) in self.sparse:
            self.sparse.discard(self.contiguous + 1)
            self.contiguous += 1

    def copy(self) -> DotRange:
        dup = DotRange()
        dup.contiguous = self.contiguous
        dup.sparse = set(self.sparse)
        return dup

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DotRange):
            return NotImplemented
        return self.contiguous == other.contiguous and self.sparse == other.sparse

    def __repr__(self) -> str:
        return f"DotRange(contiguous={self.contiguous}, sparse={sorted(self.sparse)})"

    def to_dict(self) -> dict[str, Any]:
        return {"contiguous": self.contiguous, "sparse": sorted(self.sparse)}

    @classmethod
    def from_dict(cls, d: Any) -> DotRange:
        dup = cls()
        dup.contiguous = int(d["contiguous"])
        dup.sparse = {int(x) for x in d.get("sparse", [])}
        # Absorb any contiguous runs present in the sparse set on load.
        while (dup.contiguous + 1) in dup.sparse:
            dup.sparse.discard(dup.contiguous + 1)
            dup.contiguous += 1
        return dup


class TreeVersionFrontier:
    """A dotted version frontier: per peer, exactly which op dots are held.

    Unlike a version vector (per-peer max), this represents non-contiguous
    delivery so :meth:`LosslessTreeCrdt.diff` never omits a missing interior
    op.
    """

    __slots__ = ("_dots",)

    def __init__(self) -> None:
        self._dots: dict[int, DotRange] = {}

    def contains(self, op_id: TreeOpId) -> bool:
        r = self._dots.get(op_id.peer)
        return r.contains(op_id.counter) if r is not None else False

    def observe(self, op_id: TreeOpId) -> None:
        r = self._dots.get(op_id.peer)
        if r is None:
            r = DotRange()
            self._dots[op_id.peer] = r
        r.observe(op_id.counter)

    def copy(self) -> TreeVersionFrontier:
        out = TreeVersionFrontier()
        out._dots = {peer: r.copy() for peer, r in self._dots.items()}
        return out

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TreeVersionFrontier):
            return NotImplemented
        return self._dots == other._dots

    def __repr__(self) -> str:
        return f"TreeVersionFrontier({self._dots!r})"


def tree_version_frontier_to_wire(frontier: TreeVersionFrontier) -> dict[str, Any]:
    """Serialize a frontier to the schema's ``TreeVersionFrontier`` shape."""
    return {"dots": {str(peer): rng.to_dict() for peer, rng in frontier._dots.items()}}


def tree_version_frontier_from_wire(d: Any) -> TreeVersionFrontier:
    frontier = TreeVersionFrontier()
    for peer_key, rng_d in d.get("dots", {}).items():
        frontier._dots[int(peer_key)] = DotRange.from_dict(rng_d)
    return frontier


# ---------------------------------------------------------------------------
# Internal node record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LeafBody:
    kind: LeafKind
    text: TextCrdt


@dataclass(slots=True)
class _ElementBody:
    kind: str


_NodeBody = _LeafBody | _ElementBody


@dataclass(slots=True)
class _NodeRecord:
    id: TreeOpId
    parent: TreeOpId | None
    sort: SortKey
    sort_stamp: TreeOpId
    body: _NodeBody
    tomb: TreeOpId | None = None
    text_head: TreeOpId = field(default=ROOT)


def _seed_to_text(peer: int, seed: NodeSeed) -> TextCrdt:
    assert isinstance(seed, SeedLeaf)
    return TextCrdt.seed(peer, seed.text)


def _seed_wire(seed: NodeSeed) -> dict[str, Any]:
    if isinstance(seed, SeedElement):
        return {"Element": {"kind": seed.kind}}
    return {"Leaf": {"kind": seed.kind.value, "text": seed.text}}


def _seed_from_wire(d: Any) -> NodeSeed:
    if "Element" in d:
        return SeedElement(kind=d["Element"]["kind"])
    if "Leaf" in d:
        body = d["Leaf"]
        return SeedLeaf(kind=LeafKind.from_wire(body["kind"]), text=body["text"])
    raise ValueError(f"malformed NodeSeed wire value: {d!r}")


def _origin_to_dict(origin: Any) -> dict[str, int] | None:
    if origin is TEXT_ROOT:
        return None
    assert isinstance(origin, OpId)
    return {"counter": origin.counter, "peer": origin.peer}


def _origin_from_dict(d: Any) -> Any:
    if d is None:
        return TEXT_ROOT
    return OpId(counter=int(d["counter"]), peer=int(d["peer"]))


def _text_op_to_dict(op: TextOp) -> dict[str, Any]:
    return {
        "id": {"counter": op.id.counter, "peer": op.id.peer},
        "ch": op.ch,
        "origin": _origin_to_dict(op.origin),
        "deleted": (
            {"counter": op.delete_id.counter, "peer": op.delete_id.peer}
            if op.deleted and op.delete_id is not None
            else None
        ),
    }


def _text_op_from_dict(d: Any) -> TextOp:
    deleted_field = d.get("deleted")
    delete_id = (
        OpId(counter=int(deleted_field["counter"]), peer=int(deleted_field["peer"]))
        if deleted_field is not None
        else None
    )
    return TextOp(
        id=OpId(counter=int(d["id"]["counter"]), peer=int(d["id"]["peer"])),
        ch=d["ch"],
        origin=_origin_from_dict(d.get("origin")),
        deleted=delete_id is not None,
        delete_id=delete_id,
    )


def _kind_to_dict(kind: TreeOpKind) -> dict[str, Any]:
    match kind:
        case CreateNode(id=oid, parent=oparent, sort=osort, seed=oseed):
            return {
                "CreateNode": {
                    "id": oid.to_dict(),
                    "parent": oparent.to_dict(),
                    "sort": osort.to_dict(),
                    "seed": _seed_wire(oseed),
                }
            }
        case Tombstone(node=node):
            return {"Tombstone": {"node": node.to_dict()}}
        case Reorder(node=node, sort=osort):
            return {"Reorder": {"node": node.to_dict(), "sort": osort.to_dict()}}
        case LeafEdit(node=node, prev=oprev, ops=oops):
            return {
                "LeafEdit": {
                    "node": node.to_dict(),
                    "prev": oprev.to_dict(),
                    "ops": [_text_op_to_dict(o) for o in oops],
                }
            }
        case SplitLeaf(node=node, new=new, sort=osort, at_char=at_char, prev=oprev):
            return {
                "SplitLeaf": {
                    "node": node.to_dict(),
                    "new": new.to_dict(),
                    "sort": osort.to_dict(),
                    "at_char": at_char,
                    "prev": oprev.to_dict(),
                }
            }
        case MergeLeaves(
            left=left, right=right, prev_left=prev_left, prev_right=prev_right
        ):
            return {
                "MergeLeaves": {
                    "left": left.to_dict(),
                    "right": right.to_dict(),
                    "prev_left": prev_left.to_dict(),
                    "prev_right": prev_right.to_dict(),
                }
            }
        case _:
            raise TypeError(f"unknown op kind: {type(kind).__name__}")


def _kind_from_dict(d: Any) -> TreeOpKind:
    if "CreateNode" in d:
        b = d["CreateNode"]
        return CreateNode(
            id=TreeOpId.from_dict(b["id"]),
            parent=TreeOpId.from_dict(b["parent"]),
            sort=SortKey.from_dict(b["sort"]),
            seed=_seed_from_wire(b["seed"]),
        )
    if "Tombstone" in d:
        return Tombstone(node=TreeOpId.from_dict(d["Tombstone"]["node"]))
    if "Reorder" in d:
        b = d["Reorder"]
        return Reorder(
            node=TreeOpId.from_dict(b["node"]), sort=SortKey.from_dict(b["sort"])
        )
    if "LeafEdit" in d:
        b = d["LeafEdit"]
        return LeafEdit(
            node=TreeOpId.from_dict(b["node"]),
            prev=TreeOpId.from_dict(b["prev"]),
            ops=[_text_op_from_dict(o) for o in b["ops"]],
        )
    if "SplitLeaf" in d:
        b = d["SplitLeaf"]
        return SplitLeaf(
            node=TreeOpId.from_dict(b["node"]),
            new=TreeOpId.from_dict(b["new"]),
            sort=SortKey.from_dict(b["sort"]),
            at_char=int(b["at_char"]),
            prev=TreeOpId.from_dict(b["prev"]),
        )
    if "MergeLeaves" in d:
        b = d["MergeLeaves"]
        return MergeLeaves(
            left=TreeOpId.from_dict(b["left"]),
            right=TreeOpId.from_dict(b["right"]),
            prev_left=TreeOpId.from_dict(b["prev_left"]),
            prev_right=TreeOpId.from_dict(b["prev_right"]),
        )
    raise ValueError(f"malformed TreeOpKind wire value: {d!r}")


def tree_update_to_wire(update: TreeUpdate) -> dict[str, Any]:
    """Serialize a :class:`TreeUpdate` to the schema's ``TreeUpdate`` shape.

    Validates against ``lazily-spec/schemas/lossless-tree-delta.json``: ops,
    seeds, and node ids become the schema's normative externally-tagged forms
    with PascalCase leaf kinds and ``frac`` as JSON u8 arrays.
    """
    return {
        "ops": [
            {"id": op.id.to_dict(), "kind": _kind_to_dict(op.kind)} for op in update.ops
        ]
    }


def tree_update_from_wire(d: Any) -> TreeUpdate:
    return TreeUpdate(
        ops=[
            TreeOp(
                id=TreeOpId.from_dict(op["id"]),
                kind=_kind_from_dict(op["kind"]),
            )
            for op in d.get("ops", [])
        ]
    )


# ---------------------------------------------------------------------------
# The CRDT
# ---------------------------------------------------------------------------


class LosslessTreeCrdt:
    """A lossless concrete-syntax tree CRDT (M1 core).

    Op-based: convergence is achieved by exchanging ops
    (:meth:`diff` / :meth:`apply_update`); each op-kind is itself a small
    last-writer-wins / union algebra (see the module docstring). Idempotent on
    re-delivery and order-tolerant (an op whose causal dependency has not
    arrived is buffered and retried to a fixpoint).
    """

    __slots__ = (
        "_buffered",
        "_children_by_parent",
        "_counter",
        "_frontier",
        "_log",
        "_nodes",
        "_peer",
    )

    def __init__(self, peer: int) -> None:
        self._peer = peer
        self._counter = 0
        self._nodes: dict[tuple[int, int], _NodeRecord] = {}
        # Secondary index: parent-key -> live children records (unsorted; sorted
        # lazily on read in _live_children). Replaces the O(N) full-scan per
        # parent that made Render O(N^2) over the tree (#lzlivelchildidx).
        # Maintained at every node-create site (CreateNode, SplitLeaf); tombstone
        # and reorder mutate record fields in place, so the index only needs to
        # know parent->child membership.
        self._children_by_parent: dict[tuple[int, int], list[_NodeRecord]] = {}
        root = _NodeRecord(
            id=ROOT,
            parent=None,
            sort=SortKey((), 0),
            sort_stamp=ROOT,
            body=_ElementBody("root"),
        )
        self._nodes[(0, 0)] = root
        self._frontier = TreeVersionFrontier()
        self._log: list[TreeOp] = []
        self._buffered: list[TreeOp] = []

    def _index_add(self, record: _NodeRecord) -> None:
        """Insert ``record`` into the parent->children index.

        Root (parent is None) is never indexed. Safe to call on every node
        creation; idempotent if the record is already tracked.
        """
        if record.parent is None:
            return
        pk = (record.parent.counter, record.parent.peer)
        bucket = self._children_by_parent.get(pk)
        if bucket is None:
            bucket = []
            self._children_by_parent[pk] = bucket
        # Idempotency check: apply_update may replay an op whose target already
        # exists (apply is idempotent). Skip if already tracked.
        for existing in bucket:
            if (
                existing.id.counter == record.id.counter
                and existing.id.peer == record.id.peer
            ):
                return
        bucket.append(record)

    # -- identity / fork ------------------------------------------------ #

    @property
    def peer(self) -> int:
        return self._peer

    def fork(self, peer: int) -> LosslessTreeCrdt:
        """Fork this replica's full state under a new owning ``peer``.

        A deep copy with a new identity; existing ids and sort keys are
        retained, so a forked replica converges with its origin via
        :meth:`diff` / :meth:`apply_update`.
        """
        out = LosslessTreeCrdt(peer)
        out._counter = self._counter
        nodes: dict[tuple[int, int], _NodeRecord] = {}
        for key, r in self._nodes.items():
            body: _NodeBody
            if isinstance(r.body, _LeafBody):
                body = _LeafBody(r.body.kind, r.body.text.clone())
            else:
                body = _ElementBody(r.body.kind)
            nodes[key] = _NodeRecord(
                id=r.id,
                parent=r.parent,
                sort=r.sort,
                sort_stamp=r.sort_stamp,
                body=body,
                tomb=r.tomb,
                text_head=r.text_head,
            )
        out._nodes = nodes
        # Rebuild the parent->children index from the copied node map (#lzlivelchildidx).
        out._children_by_parent = {}
        for r in nodes.values():
            out._index_add(r)
        out._frontier = self._frontier.copy()
        out._log = list(self._log)
        out._buffered = list(self._buffered)
        return out

    def _next_op_id(self) -> TreeOpId:
        self._counter += 1
        return TreeOpId(self._counter, self._peer)

    def _get(self, node: TreeOpId) -> _NodeRecord | None:
        return self._nodes.get((node.counter, node.peer))

    # -- reads ---------------------------------------------------------- #

    def _live_children(self, parent: TreeOpId) -> list[TreeOpId]:
        pk = (parent.counter, parent.peer)
        bucket = self._children_by_parent.get(pk)
        if bucket is None:
            return []
        # Tombstones remain in the bucket (logical delete); filter at read.
        live = [r for r in bucket if r.tomb is None]
        live.sort(key=lambda r: _SortableKey(r.sort))
        return [r.id for r in live]

    def render(self) -> str:
        """Concatenate live-leaf text in tree (child-sort) order."""
        out: list[str] = []

        def walk(node: TreeOpId) -> None:
            r = self._get(node)
            if r is None:
                return
            if isinstance(r.body, _LeafBody):
                out.append(r.body.text.text())
            else:
                for child in self._live_children(node):
                    walk(child)

        walk(ROOT)
        return "".join(out)

    def live_node_count(self) -> int:
        """Live nodes excluding the root — grows by one on split."""
        return sum(
            1 for key, r in self._nodes.items() if key != (0, 0) and r.tomb is None
        )

    def frontier(self) -> TreeVersionFrontier:
        """This replica's dotted version frontier (advertise to a partner)."""
        return self._frontier.copy()

    def element_kind(self, node: TreeOpId) -> str | None:
        r = self._get(node)
        return (
            r.body.kind
            if (r is not None and isinstance(r.body, _ElementBody))
            else None
        )

    def leaf_kind(self, node: TreeOpId) -> LeafKind | None:
        r = self._get(node)
        return (
            r.body.kind if (r is not None and isinstance(r.body, _LeafBody)) else None
        )

    def children(self, parent: TreeOpId) -> list[TreeOpId]:
        """Live children of ``parent`` in rendered (SortKey) order."""
        return self._live_children(parent)

    def leaf_text(self, node: TreeOpId) -> str:
        r = self._get(node)
        if r is None:
            raise TreeError("node not found")
        if not isinstance(r.body, _LeafBody):
            raise TreeError("node is not a leaf")
        return r.body.text.text()

    # -- mutations ------------------------------------------------------ #

    def _key_after(self, parent: TreeOpId, after: TreeOpId | None) -> SortKey:
        order = self._live_children(parent)
        lo: TreeOpId | None = None
        hi: TreeOpId | None = None
        if after is None:
            hi = order[0] if order else None
        else:
            idx = -1
            for i, c in enumerate(order):
                if c == after:
                    idx = i
                    break
            if idx >= 0:
                lo = after
                hi = order[idx + 1] if idx + 1 < len(order) else None
            else:
                # Anchor gone: append at the end.
                lo = order[-1] if order else None
        lo_rec = self._get(lo) if lo is not None else None
        hi_rec = self._get(hi) if hi is not None else None
        lo_frac = lo_rec.sort.frac if lo_rec is not None else None
        hi_frac = hi_rec.sort.frac if hi_rec is not None else None
        return SortKey(key_between(lo_frac, hi_frac), self._peer)

    def create_node(
        self, parent: TreeOpId, after: TreeOpId | None, seed: NodeSeed
    ) -> TreeOpId:
        if self._get(parent) is None:
            raise TreeError("node not found")
        sort = self._key_after(parent, after)
        op_id = self._next_op_id()
        node = TreeOpId(op_id.counter, op_id.peer)
        self._commit_local(
            TreeOp(op_id, CreateNode(id=node, parent=parent, sort=sort, seed=seed))
        )
        return node

    def tombstone_node(self, node: TreeOpId) -> None:
        if self._get(node) is None or (node.counter, node.peer) == (0, 0):
            raise TreeError("node not found")
        op_id = self._next_op_id()
        self._commit_local(TreeOp(op_id, Tombstone(node=node)))

    def reorder_child(self, node: TreeOpId, after: TreeOpId | None) -> None:
        rec = self._get(node)
        if rec is None or rec.parent is None:
            raise TreeError("node not found")
        sort = self._key_after(rec.parent, after)
        op_id = self._next_op_id()
        self._commit_local(TreeOp(op_id, Reorder(node=node, sort=sort)))

    def edit_leaf(
        self,
        node: TreeOpId,
        at_byte: int,
        delete_bytes: int = 0,
        insert: str = "",
    ) -> None:
        s = self.leaf_text(node)
        start = byte_to_char(s, at_byte)
        end = byte_to_char(s, at_byte + delete_bytes)
        if start is None or end is None:
            raise TreeError("offset not on a char boundary")
        delete_count = end - start

        rec = self._get(node)
        assert rec is not None and isinstance(rec.body, _LeafBody)
        # Re-own the leaf's text under this replica so concurrent edits from
        # different peers mint distinct char ids (no collision on merge).
        rec.body.text = rec.body.text.fork(self._peer)
        vv = rec.body.text.version_vector()
        for _ in range(delete_count):
            rec.body.text.delete(start)
        rec.body.text.insert_str(start, insert)
        ops = rec.body.text.delta_since(vv)

        prev = rec.text_head
        op_id = self._next_op_id()
        self._commit_local(TreeOp(op_id, LeafEdit(node=node, prev=prev, ops=ops)))

    def split_leaf(self, node: TreeOpId, at_byte: int) -> TreeOpId:
        s = self.leaf_text(node)
        at_char = byte_to_char(s, at_byte)
        if at_char is None:
            raise TreeError("offset not on a char boundary")
        rec = self._get(node)
        if rec is None or rec.parent is None:
            raise TreeError("node not found")
        assert isinstance(rec.body, _LeafBody)
        sort = self._key_after(rec.parent, node)
        prev = rec.text_head
        op_id = self._next_op_id()
        new_node = TreeOpId(op_id.counter, op_id.peer)
        self._commit_local(
            TreeOp(
                op_id,
                SplitLeaf(
                    node=node, new=new_node, sort=sort, at_char=at_char, prev=prev
                ),
            )
        )
        return new_node

    def merge_adjacent_leaves(self, left: TreeOpId, right: TreeOpId) -> None:
        # Validate both are leaves.
        self.leaf_text(left)
        self.leaf_text(right)
        rec = self._get(left)
        if rec is None or rec.parent is None:
            raise TreeError("node not found")
        order = self._live_children(rec.parent)
        idx = -1
        for i, c in enumerate(order):
            if c.counter == left.counter and c.peer == left.peer:
                idx = i
                break
        adjacent = (
            idx >= 0
            and idx + 1 < len(order)
            and order[idx + 1].counter == right.counter
            and order[idx + 1].peer == right.peer
        )
        if not adjacent:
            raise TreeError("leaves are not adjacent live siblings")
        left_rec = self._get(left)
        right_rec = self._get(right)
        assert left_rec is not None and right_rec is not None
        op_id = self._next_op_id()
        self._commit_local(
            TreeOp(
                op_id,
                MergeLeaves(
                    left=left,
                    right=right,
                    prev_left=left_rec.text_head,
                    prev_right=right_rec.text_head,
                ),
            )
        )

    # -- anti-entropy --------------------------------------------------- #

    def diff(self, their: TreeVersionFrontier) -> TreeUpdate:
        """Ops this replica holds that ``their`` frontier lacks, dotted-sorted."""
        ops = sorted(
            (op for op in self._log if not their.contains(op.id)),
            key=lambda op: (op.id.counter, op.id.peer),
        )
        return TreeUpdate(ops)

    def apply_update(self, update: TreeUpdate) -> None:
        """Apply a batch of remote ops.

        Idempotent (already-held ops skipped) and order-tolerant (an op whose
        target/parent has not arrived is buffered and retried). Advances the
        Lamport counter past every observed op.
        """
        for op in update.ops:
            if op.id.counter > self._counter:
                self._counter = op.id.counter
            if self._frontier.contains(op.id):
                continue
            self._buffered.append(op)
        self._drain_buffered()

    def _drain_buffered(self) -> None:
        while True:
            progressed = False
            pending = self._buffered
            self._buffered = []
            for op in pending:
                if self._frontier.contains(op.id):
                    continue
                if self._dependencies_ready(op):
                    self._apply_op(op)
                    self._record(op)
                    progressed = True
                else:
                    self._buffered.append(op)
            if not progressed:
                break

    def _dependencies_ready(self, op: TreeOp) -> bool:
        match op.kind:
            case CreateNode(parent=parent):
                return self._get(parent) is not None
            case Tombstone(node=node) | Reorder(node=node):
                return self._get(node) is not None
            case LeafEdit(node=node, prev=oprev) | SplitLeaf(node=node, prev=oprev):
                return self._get(node) is not None and self._frontier.contains(oprev)
            case MergeLeaves(
                left=left, right=right, prev_left=prev_left, prev_right=prev_right
            ):
                return (
                    self._get(left) is not None
                    and self._get(right) is not None
                    and self._frontier.contains(prev_left)
                    and self._frontier.contains(prev_right)
                )
            case _:
                # `TreeOpKind` is a closed six-variant union and `_kind_from_dict`
                # already rejects an unknown wire tag, so nothing else can reach
                # here. Returning False instead re-buffered the op forever: never
                # applied, never reported, and the `_buffered` list grows without
                # bound. Name the offender instead.
                raise TypeError(f"unknown TreeOp kind: {type(op.kind).__name__}")

    # -- internal apply ------------------------------------------------- #

    def _commit_local(self, op: TreeOp) -> None:
        self._apply_op(op)
        self._record(op)

    def _record(self, op: TreeOp) -> None:
        self._frontier.observe(op.id)
        self._log.append(op)

    def _apply_op(self, op: TreeOp) -> None:
        op_id = op.id
        match op.kind:
            case CreateNode(id=oid, parent=oparent, sort=osort, seed=oseed):
                if self._get(oid) is not None:
                    return
                body: _NodeBody
                match oseed:
                    case SeedLeaf(kind=lkind):
                        body = _LeafBody(lkind, _seed_to_text(oid.peer, oseed))
                    case SeedElement(kind=ekind):
                        body = _ElementBody(ekind)
                    case _:
                        # `NodeSeed` is `SeedElement | SeedLeaf`. The wildcard
                        # used to *run the SeedElement arm*, so any third seed
                        # shape silently materialised as an element shell and
                        # the node's text was dropped without a word.
                        raise TypeError(
                            f"unknown NodeSeed variant: {type(oseed).__name__}"
                        )
                rec = _NodeRecord(
                    id=oid,
                    parent=oparent,
                    sort=osort,
                    sort_stamp=op_id,
                    body=body,
                    text_head=op_id,
                )
                self._nodes[(oid.counter, oid.peer)] = rec
                self._index_add(rec)
            case Tombstone(node=node):
                rec = self._get(node)
                if rec is not None:
                    rec.tomb = op_id if rec.tomb is None else _min_id(rec.tomb, op_id)
            case Reorder(node=node, sort=osort):
                rec = self._get(node)
                if rec is not None and op_id > rec.sort_stamp:
                    rec.sort = osort
                    rec.sort_stamp = op_id
            case LeafEdit(node=node, ops=oops):
                rec = self._get(node)
                if rec is not None and isinstance(rec.body, _LeafBody):
                    rec.body.text.apply_delta(oops)
                    rec.text_head = op_id
            case SplitLeaf(node=node, new=new, sort=osort, at_char=at_char):
                self._apply_split(node, new, osort, at_char, op_id)
            case MergeLeaves(left=left, right=right):
                self._apply_merge(left, right, op_id)
            case _:
                # A `match` with no wildcard is not exhaustive — it falls
                # through and does NOTHING. An op of an unknown kind was
                # recorded into the frontier and log by `_record` while
                # changing no tree state, which is a silent divergence between
                # two replicas that both claim to have applied it.
                raise TypeError(f"unknown TreeOp kind: {type(op.kind).__name__}")

    def _apply_split(
        self,
        node: TreeOpId,
        new_node: TreeOpId,
        sort: SortKey,
        at_char: int,
        op_id: TreeOpId,
    ) -> None:
        rec = self._get(node)
        if rec is None or not isinstance(rec.body, _LeafBody):
            return
        leaf_kind = rec.body.kind
        parent = rec.parent
        text = rec.body.text.text()
        clamp = min(at_char, len(text))
        head = text[:clamp]
        tail = text[clamp:]
        # Reseed head under the original node's create peer so both replicas
        # rebuild byte-identical leaf state.
        rec.body = _LeafBody(leaf_kind, TextCrdt.seed(node.peer, head))
        rec.text_head = op_id
        if self._get(new_node) is None and parent is not None:
            record = _NodeRecord(
                id=new_node,
                parent=parent,
                sort=sort,
                sort_stamp=op_id,
                body=_LeafBody(leaf_kind, TextCrdt.seed(new_node.peer, tail)),
            )
            self._nodes[(new_node.counter, new_node.peer)] = record
            self._index_add(record)

    def _apply_merge(self, left: TreeOpId, right: TreeOpId, op_id: TreeOpId) -> None:
        left_rec = self._get(left)
        right_rec = self._get(right)
        if (
            left_rec is None
            or right_rec is None
            or not isinstance(left_rec.body, _LeafBody)
            or not isinstance(right_rec.body, _LeafBody)
        ):
            return
        combined = left_rec.body.text.text() + right_rec.body.text.text()
        left_rec.body = _LeafBody(
            left_rec.body.kind, TextCrdt.seed(left.peer, combined)
        )
        left_rec.text_head = op_id
        right_rec.tomb = (
            op_id if right_rec.tomb is None else _min_id(right_rec.tomb, op_id)
        )


@dataclass(frozen=True)
class _SortableKey:
    """A wrapper letting :class:`SortKey` participate in ``sorted``/``min``."""

    key: SortKey

    def __lt__(self, other: _SortableKey) -> bool:
        return _cmp_sort(self.key, other.key) < 0
