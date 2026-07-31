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
set -euo pipefail

SPEC_DIR="${LAZILY_SPEC_CONFORMANCE_DIR:-../lazily-spec/conformance}"
if [ ! -d "$SPEC_DIR" ]; then
  # Skipping is right for a local checkout without the sibling clone, and wrong
  # everywhere the run is supposed to PROVE something. On CI (or with
  # LAZILY_CONFORMANCE_REQUIRE_CORPUS=1) an absent corpus is the vacuous green
  # this whole ladder exists to reject: nothing was compared, and a skip that
  # exits 0 reports that as success.
  if [ -n "${CI:-}" ] || [ -n "${LAZILY_CONFORMANCE_REQUIRE_CORPUS:-}" ]; then
    echo "::error::canonical corpus not found at $SPEC_DIR — the coverage guard would" >&2
    echo "         compare against nothing and pass vacuously. Clone lazily-spec as a" >&2
    echo "         sibling, or point LAZILY_SPEC_CONFORMANCE_DIR at a copy." >&2
    exit 1
  fi
  echo "SKIP: canonical corpus not found at $SPEC_DIR (clone the lazily-spec sibling)" >&2
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
  "agent-doc/delta_agent_doc_state.json"
  "agent-doc/snapshot_agent_doc_state.json"
  # msgpack is a protocol.md MUST that lazily-py does not implement
  # (#lzmsgpackparity). The gap was already declared, but only in the
  # interop-peer handshake's `carve_outs` — a place no parity surface reads. It
  # belongs here, beside every other declared gap: the `json` half of the codec
  # obligation IS replayed (tests/test_codec_conformance.py), so this entry
  # names exactly what is missing rather than the whole obligation. Closing it
  # means encoding/decoding IpcMessage as a named-field MessagePack map.
  "codec/frame_roundtrip_msgpack.json"
  "reliable-sync/coalesce_bounds_outbox.json"
  "reliable-sync/liveness_lease_eviction.json"
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
  for known in "${KNOWN_UNCOVERED[@]:-}"; do
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
for known in "${KNOWN_UNCOVERED[@]:-}"; do
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

echo "conformance coverage OK: $covered/$total canonical fixtures OPENED by the suite" \
     "(${#KNOWN_UNCOVERED[@]} listed as known-uncovered; runtime manifest — these bytes were really read)"
