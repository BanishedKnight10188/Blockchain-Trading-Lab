"""The new Watch core obeys existing domain/application/ports import rules."""

from tests.architecture.test_dependency_boundaries import PACKAGE, PROJECT, violations


def test_watch_dependency_boundaries():
    files = {
        "domain": ("watches.py", "watch_features.py", "watch_rules.py", "agent_events.py"),
        "application": ("watches.py",),
        "ports": ("watches.py", "agent_events.py", "watch_data.py"),
    }
    for layer, names in files.items():
        for name in names:
            path = PACKAGE / layer / name
            package = ".".join(path.parent.relative_to(PROJECT).parts)
            assert violations(path.read_text(encoding="utf-8"), layer, package) == []
