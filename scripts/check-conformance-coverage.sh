#!/usr/bin/env bash
# Conformance-coverage guard (#portconformancecoverage).
#
# Fails the build when the canonical corpus in ../lazily-spec/conformance/ grows a
# fixture that no test in this repo even mentions. That is the drift this guard
# exists for: a fixture lands upstream, every binding stays green, and nobody
# learns that one of them is not replaying it.
#
# This binding uses the RUNTIME manifest (#lazilyupgradeconformance), not the
# static grep it started with. The test run records every file it actually reads
# from the conformance corpus, so a fixture named in a comment but hand-transcribed
# — the drift found in lazily-cpp's queue tests — is caught here. A source grep
# cannot see that case at all.
#
# A missing manifest is missing EVIDENCE and fails. It does not mean "no fixtures
# were read"; it means the suite ran without the recorder attached, and passing in
# that state is the vacuous green this guard exists to prevent.
#
# The same reasoning is why this script ends with a positive-evidence FLOOR
# (#lzvacuousrun). Every check between here and there is a statement about the
# fixtures the run opened, and every one of them is trivially satisfied when that
# set is empty. Reporting "coverage OK" after examining zero fixtures is the
# failure mode, not a degenerate case of success, so the magnitude is asserted
# before OK is printed.
set -euo pipefail

SPEC_DIR="${LAZILY_SPEC_CONFORMANCE_DIR:-../lazily-spec/conformance}"
if [ ! -d "$SPEC_DIR" ]; then
  # Skipping is right for a local checkout without the sibling clone, and wrong
  # everywhere the run is supposed to PROVE something (#lzvacuousrun). On CI (or
  # with LAZILY_CONFORMANCE_REQUIRE_CORPUS=1) an absent corpus is the vacuous
  # green this whole ladder exists to reject: every rung below reasons about
  # fixtures the run OPENED, so an absent corpus reports OK over nothing at all.
  # Under CI that is missing EVIDENCE, not evidence of absence — the checkout is
  # wrong, not the corpus.
  if [ -n "${CI:-}" ] || [ -n "${LAZILY_CONFORMANCE_REQUIRE_CORPUS:-}" ]; then
    echo "::error::canonical corpus not found at $SPEC_DIR — the coverage guard would" >&2
    echo "         compare against nothing and pass vacuously. Clone lazily-spec as a" >&2
    echo "         sibling, or point LAZILY_SPEC_CONFORMANCE_DIR at a copy." >&2
    exit 1
  fi
  echo "SKIP: canonical corpus not found at $SPEC_DIR (clone the lazily-spec sibling)" >&2
  echo "      Local checkout only — this would be a hard failure under CI." >&2
  exit 0
fi

# Fixtures deliberately not covered by this binding yet. Each entry is a claim that
# someone looked; shrinking this list is the work. Adding to it silently is how the
# guard rots, so keep a reason with any new entry.
#
# This list is half of what lazily-py does not prove. The other half is
# SCENARIO_EXCUSES in tests/conformance_assert.py (#lzscenariocoverage): this list
# names whole fixtures this binding never opens, that one names scenarios inside a
# fixture it DOES open, which this guard cannot see because opening a fixture for
# one scenario satisfies it. Read the two together, and keep them disjoint — a
# fixture named here is already excused a level up and must not also carry scenario
# excuses.
KNOWN_UNCOVERED=(
  # Experimental protobuf-v1 generation is piloted in Rust/Kotlin/TypeScript;
  # Python must negotiate the capability before replaying this typed trace.
  "protobuf/graph_boundary_traces.json"
  "agent-doc/delta_agent_doc_state.json"
  "agent-doc/snapshot_agent_doc_state.json"
  "reliable-sync/coalesce_bounds_outbox.json"
  "reliable-sync/liveness_lease_eviction.json"
  # The canonical journal-decoder trace has no Python replay runner yet.
  "reliable-sync/outbox_journal_decode.json"
)

MANIFEST="${LAZILY_CONFORMANCE_MANIFEST:-build/conformance-fixtures-loaded.txt}"
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

if [ ! -s "$MANIFEST" ]; then
  echo "FAIL: no conformance manifest at $MANIFEST." >&2
  echo "      Run the suite with LAZILY_CONFORMANCE_MANIFEST set so the recorder" >&2
  echo "      attaches. An absent manifest is missing evidence, not evidence of" >&2
  echo "      absence." >&2
  exit 1
fi
OPENED="$(sort -u "$MANIFEST")"

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
  if grep -qxF "$fixture" <<< "$OPENED"; then
    covered=$((covered + 1))
    continue
  fi
  excused=0
  # `[@]+...`, not `[@]:-`. Under `set -u` an EMPTY array spelled `"${a[@]:-}"`
  # expands to one EMPTY STRING rather than to nothing, so emptying the allowlist
  # — which is the goal state, "shrinking this list is the work" — injects a
  # phantom entry `''` into both loops. Here it is only a wasted comparison; in
  # the stale-allowlist loop below it hard-fails every run.
  for known in ${KNOWN_UNCOVERED[@]+"${KNOWN_UNCOVERED[@]}"}; do
    if [ "$known" = "$fixture" ]; then excused=1; break; fi
  done
  if [ "$excused" -eq 0 ]; then
    echo "ERROR: canonical fixture '$fixture' was NOT opened by the suite." >&2
    echo "       A runner may still name it in source while no longer reading it —" >&2
    echo "       that is the drift this manifest exists to catch. Replay it, or add" >&2
    echo "       it to KNOWN_UNCOVERED with a reason." >&2
    missing=$((missing + 1))
  fi
done < <(cd "$SPEC_DIR" && find . -name '*.json' | sed 's|^\./||' | sort)

# A stale allowlist is its own drift, in two directions:
#
#   1. An entry naming a fixture that no longer exists means the corpus moved and
#      nobody updated the excuse.
#   2. An entry naming a fixture the suite DOES open is a stale excuse. Nobody
#      files a bug about coverage they are told they lack, so a stale excuse hides
#      real work already done and pads the list until the genuine gaps are
#      unreadable. This is ledger rot in the understating direction.
#
# The covered-check above and the stale-check below use the SAME comparison
# (`grep -qxF` against "$OPENED") so the two can never disagree about whether a
# given fixture was opened.
for known in ${KNOWN_UNCOVERED[@]+"${KNOWN_UNCOVERED[@]}"}; do
  if [ ! -f "$SPEC_DIR/$known" ]; then
    echo "ERROR: KNOWN_UNCOVERED lists '$known', which is not in the canonical corpus." >&2
    missing=$((missing + 1))
    continue
  fi
  if grep -qxF "$known" <<< "$OPENED"; then
    echo "ERROR: KNOWN_UNCOVERED lists '$known', but the suite DID open it." >&2
    echo "       The excuse is stale: this fixture is covered. Delete the entry from" >&2
    echo "       KNOWN_UNCOVERED so the list keeps naming only the real gaps." >&2
    missing=$((missing + 1))
  fi
done

if [ "$missing" -gt 0 ]; then
  echo "conformance coverage FAILED: $missing problem(s)" >&2
  exit 1
fi

# ---- Positive-evidence floor (#lzvacuousrun) ----
# Everything above reasons about fixtures this run OPENED, so all of it is
# vacuously satisfied by an empty population: zero fixtures means zero uncovered
# fixtures and zero stale excuses. The loops cannot distinguish "nothing is
# wrong" from "nothing was examined", so assert the MAGNITUDE explicitly before
# reporting OK. Do not lower these to fix a red run — a drop here means the
# corpus or the recorder shrank, which is the finding.
#
# MIN_FIXTURES is calibrated slightly below the real current number (136 of 140
# canonical fixtures opened, 4 known-uncovered) so ordinary corpus churn does not
# trip it while a collapse does. It ratchets UP with coverage — the slack stays
# fixed at four so a newly replayed fixture cannot be silently dropped again. The
# env override exists for bisecting an upstream corpus change, not for making a
# red run green.
MIN_FIXTURES="${MIN_FIXTURES:-132}"
if [ "$total" -eq 0 ]; then
  echo "ERROR: the corpus at $SPEC_DIR listed ZERO fixtures." >&2
  echo "       Every check above is vacuously green over an empty population:" >&2
  echo "       no fixture can be uncovered when there are no fixtures. The" >&2
  echo "       directory exists but holds no *.json — wrong path or wrong" >&2
  echo "       checkout (#lzvacuousrun)." >&2
  exit 1
fi
if [ "$covered" -lt "$MIN_FIXTURES" ]; then
  echo "ERROR: only $covered distinct canonical fixtures were OPENED, expected >= $MIN_FIXTURES." >&2
  echo "       A replay was removed, renamed, or short-circuited, or the recorder" >&2
  echo "       detached mid-run. Do not lower MIN_FIXTURES to fix this." >&2
  exit 1
fi

echo "conformance coverage OK: $covered/$total canonical fixtures OPENED by the suite" \
     "(${#KNOWN_UNCOVERED[@]} listed as known-uncovered; runtime manifest — these bytes were really read)"
