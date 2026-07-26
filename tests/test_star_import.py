"""`from lazily import *` must work.

Regression guard: `__all__` once carried a quoted pseudo-comment entry
(`"# -- new reactive-primitive families ... #"`), a section divider that had been
written INSIDE the list instead of above it. Every name in `__all__` must resolve as a
module attribute, so that entry made star-import raise AttributeError for everyone —
while every targeted `from lazily import X` kept working, which is why it went unnoticed.
"""

import lazily


def test_star_import_resolves_every_exported_name() -> None:
    namespace: dict[str, object] = {}
    exec("from lazily import *", namespace)
    assert len(namespace) > 1


def test_every_all_entry_is_a_real_attribute() -> None:
    missing = [name for name in lazily.__all__ if not hasattr(lazily, name)]
    assert missing == [], f"__all__ names that do not resolve: {missing}"


def test_no_all_entry_looks_like_a_comment_or_divider() -> None:
    # A divider is not merely unresolvable — it is the specific shape of the original bug,
    # so it is worth failing on directly rather than only via the attribute check above.
    suspicious = [name for name in lazily.__all__ if not name.isidentifier()]
    assert suspicious == [], f"__all__ entries that are not identifiers: {suspicious}"
