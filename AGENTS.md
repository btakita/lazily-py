# lazily-py

Python port of the lazily **Cell kernel** (`#lzcellkernel`) — a `Source` cell
(value from outside; `set` / `merge`) and a `Computed` cell (value from upstream,
via a compute function), with `Effect` the value-less sink. `Cell` is the value
node concept; `Source` is its native writable handle and `Cell` is an
identity-preserving migration alias. Every cell is **guarded** — an equal recompute
suppresses the downstream cascade (matching TC39 `Signal.Computed`) — with **no
unguarded derived mode**; `computed` *is* the guarded derived constructor. The
eager construction is `computed(ctx, f).eager()` (`.eager()` / `.lazy()` /
`.is_eager()`); `Slot` is retained as the context-as-dict storage position (the
Python analog of `lazily-rs`'s surviving storage-sense `Slot`). The v1 value
vocabulary (`Signal` / `signal` / `signal_def`, `formula` / `formula_def` /
`FormulaCell`, `SourceCell` / `SourceCellSlot`, `.drive()` / `.undrive()` /
`is_driven` / `is_active`) is **removed**; `source` / `computed` are canonical
and `cell` / `cell_def` / `slot` are **deprecated** aliases. Python has no
compile-time read/write split (design §4) — the split is convention (a `Source`
has `set` / `merge`, a `Computed` does not). Includes
automatic dependency tracking, the full lazily-spec wire protocol, CRDT
collection types, the lossless tree CRDT, and the command/RPC message plane.

## Commit & Push

Commit and push completed work at the end of every turn that changed code,
tests, docs, or fixtures — do not leave finished work uncommitted. Run `make
check` first and ensure it is green; stage only the files that belong to the
change (never secrets or private customer names — see the workspace
`runbooks/private-name-hygiene.md`); write a concise commit message in the
repo's existing style; push to the current branch on `origin`. This standing
rule overrides the harness default of "commit only when explicitly asked" for
this repo.

<!-- tsift:code-navigation v=0.1.80 -->
## Code Navigation

Run `tsift status` at session start from the owning repo root. If the task or file lives under a git submodule (for example `src/tsift/...`), switch to that submodule root first so the harness loads the narrower local instructions and repo state instead of the superproject root. If status prints a `run:` recommendation for stale or missing tsift state, run `tsift status --fix` before relying on tsift results; when the harness cannot perform write commands, ask the user to run the printed command instead.

Prefer tsift envelopes over raw reads:
- `tsift --envelope search <query>` instead of `grep`/`rg`
- `tsift --envelope source-read <file>` / `tsift --envelope symbol-read <symbol>` instead of `cat`/`head`
- `tsift --envelope explain <symbol>` and `tsift graph <symbol> --callers` / `--callees` for call graphs
- `tsift diff-digest [path]` instead of `git diff`, `git show`, or patch-style `git log`
- `tsift --envelope session-review <path>` / `tsift --envelope context-pack <path>` instead of replaying long session docs, transcripts, or runtime logs
- `tsift --envelope digest-runner --kind test|log --path . --shell-command '<command>'` instead of raw test/build output

Command detail lives in [`runbooks/code-navigation.md`](runbooks/code-navigation.md) — budgets, `tsift workflow search`, `report.scale_guard` handling, the harness rewrite path for `PreToolUse`-less harnesses, and Codex/OpenCode integration. `tsift init` writes and versions that runbook alongside this block, so it is present in every initialized checkout; read it before broad exploration instead of expanding this block. A repository that also ships a current `.claude/skills/tsift/SKILL.md` should use that skill as the deeper source.

For local verification, run `make check` before committing. After local changes, check the latest GitHub Actions CI run with `gh run list --workflow CI --limit 1` and fix any failing tests before calling the work complete.

Only read full source files when tsift results are insufficient.
<!-- /tsift:code-navigation -->
