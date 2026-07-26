#!/usr/bin/env bash
# Conformance-coverage guard (#portconformancecoverage).
#
# Fails the build when the canonical corpus in ../lazily-spec/conformance/ grows a
# fixture that no test in this repo even mentions. That is the drift this guard
# exists for: a fixture lands upstream, every binding stays green, and nobody
# learns that one of them is not replaying it.
#
# WHAT THIS CATCHES AND WHAT IT DOES NOT — read before trusting it.
#
# This is a STATIC guard. It greps the test sources for each canonical fixture's
# filename. So:
#   * absent   -> caught. A fixture no test names cannot be being replayed.
#   * present  -> NOT proof of replay. A test may name a fixture in a comment and
#                 hand-transcribe its contents, which is exactly the drift found in
#                 lazily-cpp's queue tests. Only a RUNTIME manifest proves the
#                 bytes were opened, which is what lazily-kt and lazily-cpp do via
#                 LAZILY_CONFORMANCE_MANIFEST.
#
# So a green run here means "no canonical fixture is unmentioned", not "every
# canonical fixture is replayed". Upgrading this binding to the runtime manifest is
# strictly better; this is the portable floor, not the ceiling.
set -euo pipefail

SPEC_DIR="${LAZILY_SPEC_CONFORMANCE_DIR:-../lazily-spec/conformance}"
if [ ! -d "$SPEC_DIR" ]; then
  echo "SKIP: canonical corpus not found at $SPEC_DIR (clone the lazily-spec sibling)" >&2
  exit 0
fi

# Fixtures deliberately not covered by this binding yet. Each entry is a claim that
# someone looked; shrinking this list is the work. Adding to it silently is how the
# guard rots, so keep a reason with any new entry.
KNOWN_UNCOVERED=(
  "agent-doc/delta_agent_doc_state.json"
  "agent-doc/snapshot_agent_doc_state.json"
  "collections/topiccell_broadcast_cursor_isolation.json"
  "collections/topiccell_durable_replay_gc.json"
  "collections/topiccell_ephemeral_lifecycle.json"
  "collections/topiccell_offline_tail_bounds.json"
  "collections/workqueue_competing_delivery.json"
  "collections/workqueue_lease_deadletter.json"
  "reliable-sync/coalesce_bounds_outbox.json"
  "reliable-sync/liveness_lease_eviction.json"
)

TEST_DIRS=("tests")
EXTS=(".py")

collect_sources() {
  for d in "${TEST_DIRS[@]}"; do
    [ -d "$d" ] || continue
    for e in "${EXTS[@]}"; do
      find "$d" -type f -name "*$e" -print0
    done
  done
}

SOURCES="$(collect_sources | xargs -0 cat 2>/dev/null || true)"
if [ -z "$SOURCES" ]; then
  echo "FAIL: read no test sources from ${TEST_DIRS[*]}; this check would be vacuous" >&2
  exit 1
fi

missing=0
total=0
covered=0
while IFS= read -r fixture; do
  total=$((total + 1))
  name="$(basename "$fixture")"
  # Here-string, NOT a pipe. With `set -o pipefail`, `printf ... | grep -q` reports
  # FAILURE when grep matches: grep -q exits immediately on the first hit, printf
  # takes SIGPIPE writing the rest, and pipefail surfaces printf's death as the
  # pipeline's status. The check then inverts — every covered fixture is reported
  # missing. That is exactly how it behaved before this line changed.
  if grep -qF "$name" <<< "$SOURCES"; then
    covered=$((covered + 1))
    continue
  fi
  excused=0
  for known in "${KNOWN_UNCOVERED[@]:-}"; do
    if [ "$known" = "$fixture" ]; then excused=1; break; fi
  done
  if [ "$excused" -eq 0 ]; then
    echo "ERROR: canonical fixture '$fixture' exists but no test in this repo names it." >&2
    echo "       Write a runner that replays it, or add it to KNOWN_UNCOVERED with a reason." >&2
    missing=$((missing + 1))
  fi
done < <(cd "$SPEC_DIR" && find . -name '*.json' | sed 's|^\./||' | sort)

# A stale allowlist is its own drift: an entry naming a fixture that no longer
# exists means the corpus moved and nobody updated the excuse.
for known in "${KNOWN_UNCOVERED[@]:-}"; do
  if [ ! -f "$SPEC_DIR/$known" ]; then
    echo "ERROR: KNOWN_UNCOVERED lists '$known', which is not in the canonical corpus." >&2
    missing=$((missing + 1))
  fi
done

if [ "$missing" -gt 0 ]; then
  echo "conformance coverage FAILED: $missing problem(s)" >&2
  exit 1
fi

echo "conformance coverage OK: $covered/$total canonical fixtures named by tests" \
     "(${#KNOWN_UNCOVERED[@]} listed as known-uncovered; static check — naming is not replaying)"
