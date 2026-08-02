"""Full Harel/SCXML state charts — native Python, conforming to
``lazily-spec/docs/state-charts.md`` and the Lean ``LazilyFormal.StateChart``
formal model (``lazily-formal``).

A chart is **compute, not protocol**: it is never serialized as a distinct
wire kind. lazily-py is a reactive binding, so the active configuration lives
in a :class:`~lazily.cell.Cell`; any ``Computed`` / ``Effect`` / subscriber that
reads :meth:`StateChart.configuration`, :meth:`StateChart.active_leaves`, or
:meth:`StateChart.matches` is invalidated on a real transition. A no-op
self-transition is suppressed by the Cell's ``!=`` (PartialEq) guard (see the
spec's "Self-transitions" section).

Implemented subset (per the spec's implementation-status note): compound
states, orthogonal (parallel) regions, shallow + deep history, entry/exit/
transition actions, named guards, external + internal transitions. Extended
state ``{"expr": ...}`` guards and ``run`` actions are rejected explicitly;
``final`` states are accepted as leaves without raising completion (``done``)
events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ChartDef", "StateChart"]

_HISTORY_SHALLOW = "shallow"
_HISTORY_DEEP = "deep"

#: The closed `kind` vocabulary of `lazily-spec/schemas/statechart.json`, whose
#: `State` object is `additionalProperties: false`. Omitting `kind` is legal and
#: means "infer me from the structural markers"; naming a value outside this set
#: is not, and `_parse_state` refuses it.
_STATE_KINDS = ("atomic", "compound", "parallel", "history", "final")


def _as_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return cast("dict[str, object]", value)


def _parse_action_list(raw: object, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TypeError(f"{label} must be an array of strings")
    actions: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeError(f"{label} actions must be strings")
        actions.append(item)
    return actions


class _Transition:
    __slots__ = ("action", "guard", "internal", "target")

    def __init__(
        self,
        target: str,
        guard: str | None,
        action: list[str],
        internal: bool,
    ) -> None:
        self.target = target
        self.guard = guard
        self.action = action
        self.internal = internal


def _parse_transition(raw: object, state_id: str, event: str) -> _Transition:
    label = f"transition {state_id}.{event}"
    if isinstance(raw, str):
        return _Transition(raw, None, [], False)
    obj = _as_object(raw, label)
    target = obj.get("target")
    if not isinstance(target, str):
        raise TypeError(f"{label} requires a string `target`")
    guard: str | None = None
    raw_guard = obj.get("guard")
    if raw_guard is not None:
        if isinstance(raw_guard, str):
            guard = raw_guard
        elif isinstance(raw_guard, dict) and "expr" in raw_guard:
            raise TypeError(
                f"{label}: context-expression `{{expr: ...}}` guards are not "
                "supported (rejecting explicitly per spec)"
            )
        else:
            raise TypeError(f"{label}: guard must be a string")
    return _Transition(
        target,
        guard,
        _parse_action_list(obj.get("action"), f"{label} action"),
        obj.get("internal") is True,
    )


class _StateDef:
    __slots__ = (
        "default",
        "entry",
        "exit",
        "history",
        "id",
        "initial",
        "kind",
        "parent",
        "transitions",
    )

    def __init__(
        self,
        id: str,
        parent: str | None,
        kind: str,
        history: str | None,
        initial: str | None,
        default: str | None,
        transitions: dict[str, _Transition],
        entry: list[str],
        exit: list[str],
    ) -> None:
        self.id = id
        self.parent = parent
        self.kind = kind
        self.history = history
        self.initial = initial
        self.default = default
        self.transitions = transitions
        self.entry = entry
        self.exit = exit


def _parse_state(id: str, raw: object) -> _StateDef:
    obj = _as_object(raw, f"state {id}")
    parent = obj.get("parent")
    parent_str = parent if isinstance(parent, str) else None
    initial = obj.get("initial")
    initial_str = initial if isinstance(initial, str) else None
    default = obj.get("default")
    default_str = default if isinstance(default, str) else None

    if obj.get("run") is not None:
        raise TypeError(
            f"state {id} uses `run` actions, which are not supported "
            "(rejecting explicitly per spec)"
        )

    # `kind` is a CLOSED vocabulary, and the schema that closes it is not a wire
    # schema: `statechart.json` says a chart "is never serialized over IPC/FFI as
    # a distinct type", so no forward-compatibility argument reaches this field —
    # there is no future peer whose newer `kind` we must survive, only a chart
    # this build was handed. Omission is the legal way to say "infer it" and is
    # handled below; a value outside the enum is a malformed chart and is named.
    raw_kind = obj.get("kind")
    if raw_kind is not None and raw_kind not in _STATE_KINDS:
        raise TypeError(f"state {id}: unknown state kind `{raw_kind}`")

    raw_history = obj.get("history")
    if raw_history is not None and not isinstance(raw_history, str):
        raise TypeError(f"state {id}: history must be a string")

    if isinstance(raw_history, str):
        if raw_history not in (_HISTORY_SHALLOW, _HISTORY_DEEP):
            raise TypeError(f"state {id}: unknown history kind `{raw_history}`")
        kind = "history"
        history = raw_history
    elif obj.get("parallel") is True:
        kind = "parallel"
        history = None
    elif obj.get("kind") == "final":
        kind = "final"
        history = None
    elif initial_str is not None:
        kind = "compound"
        history = None
    else:
        # **Inference from SILENCE, which is a different fact from an unknown
        # value.** By the time control reaches here `kind` is either absent or a
        # member of `_STATE_KINDS` — an unrecognised value was refused above —
        # so this arm reads only the markers the object carries (`history`,
        # `parallel: true`, `kind: "final"`, `initial`) and falls back to the
        # leaf case. That is what `statechart.json` documents for an omitted
        # `kind`: a state with no children and no initial child is atomic.
        #
        # The remaining leniency is narrower than it looks: an unrecognised
        # STRUCTURAL MARKER — an extra key, not a `kind` value — still lands
        # here and makes the state a LEAF.
        # `_enter_subtree` will not descend into it and `is_leaf` reports True,
        # so the chart runs with that subtree collapsed rather than refusing to
        # load. Every *closed* vocabulary in this parser fails closed: an
        # unknown `kind` and an unknown `history` value both raise above, and
        # `_parse_transition` rejects unknown transition fields.
        #
        # Pinned by `tests/test_library_leniency.py`.
        kind = "atomic"
        history = None

    transitions: dict[str, _Transition] = {}
    raw_on = obj.get("on")
    if raw_on is not None:
        on = _as_object(raw_on, f"state {id}.on")
        for event, raw_t in on.items():
            transitions[event] = _parse_transition(raw_t, id, event)

    return _StateDef(
        id,
        parent_str,
        kind,
        history,
        initial_str,
        default_str,
        transitions,
        _parse_action_list(obj.get("entry"), f"state {id} entry"),
        _parse_action_list(obj.get("exit"), f"state {id} exit"),
    )


def _compute_depth(
    states: dict[str, _StateDef], id: str, current: int, depth: dict[str, int]
) -> None:
    depth[id] = current
    for defn in states.values():
        if defn.parent == id:
            _compute_depth(states, defn.id, current + 1, depth)


class ChartDef:
    """A parsed, immutable chart definition.

    ``kind`` is inferred when not stated: ``history`` when ``history`` is set;
    ``parallel`` when ``parallel`` is true; ``compound`` when the state has an
    ``initial``; otherwise ``atomic``.
    """

    __slots__ = ("children", "depth", "order", "root", "states")

    def __init__(
        self,
        states: dict[str, _StateDef],
        children: dict[str, list[str]],
        order: dict[str, int],
        depth: dict[str, int],
        root: str,
    ) -> None:
        self.states = states
        self.children = children
        self.order = order
        self.depth = depth
        self.root = root

    @staticmethod
    def from_chart(value: Mapping[str, object]) -> ChartDef:
        """Parse and validate the declarative chart form.

        Raises ``TypeError`` on malformed charts or unsupported features
        (``run`` actions, ``{expr: ...}`` guards).
        """
        obj = _as_object(value, "chart")
        if not isinstance(obj.get("initial"), str):
            raise TypeError("chart.initial is required")
        states_obj = obj.get("states")
        states_map = _as_object(states_obj, "chart.states")

        states: dict[str, _StateDef] = {}
        order: dict[str, int] = {}
        for idx, (sid, raw) in enumerate(states_map.items()):
            order[sid] = idx
            states[sid] = _parse_state(sid, raw)

        children: dict[str, list[str]] = {}
        root: str | None = None
        for defn in states.values():
            if defn.parent is not None:
                children.setdefault(defn.parent, []).append(defn.id)
            else:
                if root is not None:
                    raise TypeError("chart has more than one root (parent-less state)")
                root = defn.id
        for kids in children.values():
            kids.sort(key=lambda k: order.get(k, len(order) + 1))
        if root is None:
            raise TypeError("chart has no root (parent-less state)")

        depth: dict[str, int] = {}
        _compute_depth(states, root, 0, depth)

        return ChartDef(states, children, order, depth, root)

    def kind(self, id: str) -> str:
        """The kind of state ``id``, or ``"atomic"`` when ``id`` is not declared.

        **Deliberate leniency.** Transition targets are free-form strings on the
        wire and are never cross-checked against the state table at parse time,
        so a chart authored against a larger state set — or one whose target
        names a state a later revision adds — can reference an id this
        ``ChartDef`` does not hold. Treating an undeclared id as an atomic leaf
        keeps the machine running: the transition fires, the id enters the
        configuration, and it simply has no children to descend into and no
        entry/exit actions to run (`_enter_subtree` returns early on the same
        missing lookup). Refusing instead would turn one unknown target into a
        dead chart.

        Consequence, stated: a *typo* in a target id is indistinguishable from a
        forward reference and is accepted the same way, landing the machine in a
        state with no behaviour rather than reporting the typo. Callers that
        need the stricter reading should validate targets against
        :attr:`states` before running the chart.

        Pinned by ``tests/test_library_leniency.py``.
        """
        defn = self.states.get(id)
        return defn.kind if defn is not None else "atomic"

    def is_leaf(self, id: str) -> bool:
        return self.kind(id) in ("atomic", "final")

    def ancestors_inclusive(self, id: str) -> list[str]:
        out: list[str] = []
        cur: str | None = id
        while cur is not None:
            defn = self.states.get(cur)
            if defn is None:
                break
            out.append(cur)
            cur = defn.parent
        return out

    def lca(self, a: str, b: str) -> str:
        anc_a = set(self.ancestors_inclusive(a))
        for cid in self.ancestors_inclusive(b):
            if cid in anc_a:
                return cid
        return self.root

    def is_proper_descendant(self, desc: str, anc: str) -> bool:
        return desc != anc and anc in self.ancestors_inclusive(desc)

    def depth_of(self, id: str) -> int:
        return self.depth.get(id, 0)


def _enter_subtree(
    defn: ChartDef,
    state: str,
    enter: set[str],
    actions: list[str] | None,
) -> None:
    node = defn.states.get(state)
    enter.add(state)
    if actions is not None and node is not None:
        actions.extend(node.entry)
    if node is None:
        return
    if node.kind == "compound":
        if node.initial is not None:
            _enter_subtree(defn, node.initial, enter, actions)
    elif node.kind == "parallel":
        for region in defn.children.get(state, ()):
            _enter_subtree(defn, region, enter, actions)


def _path_below(defn: ChartDef, lca: str, target: str) -> list[str]:
    chain = defn.ancestors_inclusive(target)  # [target, ..., root]
    try:
        idx = chain.index(lca)
    except ValueError:
        idx = len(chain)
    below = chain[:idx]  # drop lca and above
    below.reverse()  # [child-of-lca, ..., target]
    return below


def _history_child_of(defn: ChartDef, region: str) -> str | None:
    for kid in defn.children.get(region, ()):
        if defn.kind(kid) == "history":
            return kid
    return None


def _record_region(
    defn: ChartDef,
    region: str,
    hist_child: str,
    config: set[str],
    history: dict[str, _Recording],
) -> None:
    hist_def = defn.states.get(hist_child)
    if hist_def is None or hist_def.kind != "history":
        return
    if hist_def.history == _HISTORY_SHALLOW:
        for kid in defn.children.get(region, ()):
            if kid in config and defn.kind(kid) != "history":
                history[hist_child] = _Recording(child=kid)
                return
    else:
        below = {s for s in config if defn.is_proper_descendant(s, region)}
        history[hist_child] = _Recording(deep_set=below)


class _Recording:
    """A history recording: shallow records one child; deep records a set."""

    __slots__ = ("child", "deep_set")

    def __init__(
        self, child: str | None = None, deep_set: set[str] | None = None
    ) -> None:
        self.child = child
        self.deep_set = deep_set


def _guard_passes(transition: _Transition, guards: Mapping[str, bool]) -> bool:
    """Whether ``transition``'s guard admits it under ``guards``.

    **Deliberate leniency, in the fail-closed direction.** Guard names come from
    the chart JSON; the ``guards`` mapping comes from the host. The two are
    authored separately and lazily never resolves a guard to code, so a chart
    naming a guard the host did not supply is an ordinary interop state, not
    corruption. An absent guard evaluates **False**, so the transition does NOT
    fire — the unknown value denies rather than admits.

    Consequence, stated: a host that misspells a guard key silently loses that
    transition instead of being told. That is the deliberate direction — a
    statechart that refuses to move is recoverable, one that moves on an
    unevaluated guard is not — and it is why this default is False and not True.

    Pinned by ``tests/test_library_leniency.py``.
    """
    if transition.guard is None:
        return True
    return guards.get(transition.guard, False)  # fail-closed


def _restore_via_history(
    defn: ChartDef,
    history: dict[str, _Recording],
    hist: str,
    region: str,
    enter: set[str],
) -> None:
    recording = history.get(hist)
    if recording is None:
        hist_def = defn.states.get(hist)
        region_def = defn.states.get(region)
        start = (
            hist_def.default if hist_def is not None and hist_def.default else None
        ) or (region_def.initial if region_def is not None else None)
        if start is not None:
            for s in _path_below(defn, region, start):
                enter.add(s)
            _enter_subtree(defn, start, enter, None)
        return
    if recording.child is not None:
        enter.add(recording.child)
        _enter_subtree(defn, recording.child, enter, None)
    elif recording.deep_set is not None:
        enter.update(recording.deep_set)


def _compute_exit_enter(
    defn: ChartDef,
    source: str,
    transition: _Transition,
    leaf: str,
    config: set[str],
    history: dict[str, _Recording],
) -> tuple[set[str], set[str]]:
    target = transition.target
    internal = transition.internal and (
        target == source or defn.is_proper_descendant(target, source)
    )
    lca = source if internal else defn.lca(leaf, target)

    exit_set = {s for s in config if defn.is_proper_descendant(s, lca)}

    enter: set[str] = set()
    if defn.kind(target) == "history":
        target_def = defn.states.get(target)
        region = (
            target_def.parent
            if target_def is not None and target_def.parent
            else defn.root
        )
        enter.update(_path_below(defn, lca, region))
        _restore_via_history(defn, history, target, region, enter)
    else:
        enter.update(_path_below(defn, lca, target))
        _enter_subtree(defn, target, enter, None)

    return exit_set, enter


class _Candidate:
    __slots__ = ("leaf", "source", "transition")

    def __init__(self, source: str, transition: _Transition, leaf: str) -> None:
        self.source = source
        self.transition = transition
        self.leaf = leaf


class StateChart:
    """A reactive full-Harel state chart backed by a configuration :class:`Cell`.

    The active configuration lives in ``Cell[frozenset[str]]``; reading
    :meth:`configuration` / :meth:`active_leaves` / :meth:`matches` inside a
    ``Computed`` or ``Effect`` auto-subscribes, so the reader is invalidated on a
    real transition. A no-op self-transition is accepted (``True``) but the
    Cell's ``!=`` guard suppresses propagation.

    Deterministic by construction (mirroring the Lean ``StateChart.send`` total
    function): a given ``(chart, history, configuration, event, guards)`` yields
    a unique result.
    """

    __slots__ = ("_config", "_def", "_history", "_last_actions")

    def __init__(self, ctx: dict, defn: ChartDef) -> None:
        self._def = defn
        enter: set[str] = set()
        actions: list[str] = []
        _enter_subtree(defn, defn.root, enter, actions)
        self._config: Cell[frozenset[str]] = Cell(ctx, frozenset(enter))
        self._history: dict[str, _Recording] = {}
        self._last_actions: list[str] = actions

    def last_actions(self) -> list[str]:
        """Ordered action names from initial entry or the most recent
        :meth:`send` (exit -> transition -> entry). Empty after a rejected event."""
        return list(self._last_actions)

    def _read_config(self, ctx: Any) -> frozenset[str]:
        """Read the active-configuration cell, value-threading through ``ctx``
        (the caller's :class:`~lazily.compute.Compute` view) when given, else an
        untracked bare read (``#lzcellkernel`` bare-read removal)."""
        if ctx is None:
            return self._config.value
        return ctx.read(self._config)

    def configuration(self, ctx: Any = None) -> list[str]:
        """Full active configuration (leaves plus all active ancestors), sorted.
        Pass the caller's compute view (``ctx``) to value-thread the edge."""
        return sorted(self._read_config(ctx))

    def active_leaves(self, ctx: Any = None) -> list[str]:
        """Active atomic leaves, sorted (one per parallel region). Pass the
        caller's compute view (``ctx``) to value-thread the edge."""
        return sorted(s for s in self._read_config(ctx) if self._def.is_leaf(s))

    def matches(self, id: str, ctx: Any = None) -> bool:
        """Hierarchical "state-in" predicate: ``True`` iff ``id`` is active. Pass
        the caller's compute view (``ctx``) to value-thread the edge."""
        return id in self._read_config(ctx)

    def send(self, event: str, guards: Mapping[str, bool] | None = None) -> bool:
        """Run-to-completion transition.

        Returns ``True`` if any transition was taken, ``False`` if rejected
        (configuration unchanged, no actions fired). ``guards`` resolves named
        guards for this send (absent / unknown name -> fail-closed ``False``).
        """
        guard_map = guards or {}
        defn = self._def
        config = set(self._config.value)

        candidates: list[_Candidate] = []
        for leaf in (s for s in config if defn.is_leaf(s)):
            for anc in defn.ancestors_inclusive(leaf):
                state_def = defn.states.get(anc)
                transition = state_def.transitions.get(event) if state_def else None
                if transition is not None and _guard_passes(transition, guard_map):
                    candidates.append(_Candidate(anc, transition, leaf))
                    break  # innermost wins for this leaf's chain

        if not candidates:
            self._last_actions = []
            return False

        candidates.sort(
            key=lambda c: (
                -defn.depth_of(c.source),
                defn.order.get(c.source, len(defn.order) + 1),
            )
        )

        exit_union: set[str] = set()
        enter_union: set[str] = set()
        taken: list[_Transition] = []
        for cand in candidates:
            exit_set, enter_set = _compute_exit_enter(
                defn, cand.source, cand.transition, cand.leaf, config, self._history
            )
            if exit_set & exit_union:
                continue  # conflicts with an already-taken transition
            exit_union |= exit_set
            enter_union |= enter_set
            taken.append(cand.transition)

        if not taken:
            self._last_actions = []
            return False

        for s in exit_union:
            hist_child = _history_child_of(defn, s)
            if hist_child is not None:
                _record_region(defn, s, hist_child, config, self._history)

        actions: list[str] = []
        for s in sorted(
            exit_union,
            key=lambda x: (-defn.depth_of(x), x),
        ):
            node = defn.states.get(s)
            if node is not None:
                actions.extend(node.exit)
        for t in taken:
            actions.extend(t.action)
        for s in sorted(enter_union, key=lambda x: (defn.depth_of(x), x)):
            node = defn.states.get(s)
            if node is not None:
                actions.extend(node.entry)

        config -= exit_union
        config |= enter_union

        self._last_actions = actions
        # Cell's != guard suppresses touch on a no-op (equal) configuration,
        # matching the spec's self-transition rule.
        self._config.set(frozenset(config))
        return True
