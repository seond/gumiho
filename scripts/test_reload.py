"""Validate hot-reload: reflex/workflow re-parse, the syntax guard (keep
last-good on parse error), and hook-module reload. Restores all files."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import registry
from gumiho.reload import BEHAVIOR, Reloader

survival = BEHAVIOR / "reflexes" / "survival.toml"
probe = BEHAVIOR / "hooks" / "_reload_probe.py"


def main():
    original = survival.read_text(encoding="utf-8")
    rl = Reloader(lambda: [])
    rl.load_all()

    b = registry.get_bundle("survival")
    assert any(r.when == "should_drink_hp:50" for r in b.reflexes), b
    print("initial survival threshold: 50")

    # edit + reload -> new threshold live
    survival.write_text(original.replace("should_drink_hp:50", "should_drink_hp:60"),
                        encoding="utf-8")
    assert rl._load(survival) is True
    b = registry.get_bundle("survival")
    assert any(r.when == "should_drink_hp:60" for r in b.reflexes), b
    print("after edit -> reloaded threshold: 60")

    # syntax error -> keep last-good (60), report failure
    survival.write_text("name = \nreflex = [ broken", encoding="utf-8")
    assert rl._load(survival) is False
    b = registry.get_bundle("survival")
    assert any(r.when == "should_drink_hp:60" for r in b.reflexes), "last-good lost!"
    print("broken edit -> parse rejected, last-good (60) kept")

    survival.write_text(original, encoding="utf-8")
    assert rl._load(survival) is True
    print("restored survival.toml")

    # hook module reload: register a probe guard, change it, reload, confirm
    probe.write_text(
        "from gumiho.registry import guard\n"
        "@guard('probe_val')\ndef _(s): return 111\n", encoding="utf-8")
    assert rl._load(probe) is True
    assert registry.get_guard("probe_val")(None) == 111
    probe.write_text(
        "from gumiho.registry import guard\n"
        "@guard('probe_val')\ndef _(s): return 222\n", encoding="utf-8")
    assert rl._load(probe) is True
    assert registry.get_guard("probe_val")(None) == 222
    print("hook reload: guard changed 111 -> 222 live")
    probe.unlink()

    print("\nALL RELOAD TESTS PASSED")


if __name__ == "__main__":
    main()
