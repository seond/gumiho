"""Route recording: the command filter, saving to routes.json, and the reloader
merging it into knowledge['travel_routes']. Cleans up routes.json."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import registry, routes
from gumiho.reload import get_knowledge, Reloader, save_route
from gumiho.webui import _route_worthy

ROUTES = routes.ROUTES_PATH


def test_filter():
    keep = ["남", "북", "동", "아래", "맨홀 열", "뚜껑 밀", "들어", "나가"]
    drop = ["봐", "지도", "장비", "누구", "자", "귀환", "방비! 말",
            "개구리 공격", "거미 고려", "불고기피자 복용"]
    for c in keep:
        assert _route_worthy(c), f"should record {c!r}"
    for c in drop:
        assert not _route_worthy(c), f"should skip {c!r}"
    print("filter: keeps movement/door, drops look/combat/speech OK")


def main():
    backup = ROUTES.read_text(encoding="utf-8") if ROUTES.exists() else None
    try:
        test_filter()
        Reloader(lambda: []).load_all()

        save_route("테스트존", ["남", "남", "맨홀 열", "아래"])
        # live knowledge updated immediately
        assert get_knowledge()["travel_routes"]["테스트존"] == ["남", "남", "맨홀 열", "아래"]
        # persisted to routes.json
        assert routes.load()["테스트존"] == ["남", "남", "맨홀 열", "아래"]
        print("save_route: live + persisted OK")

        # a fresh reload picks it up (merged into travel_routes)
        get_knowledge().clear()
        Reloader(lambda: []).load_all()
        assert get_knowledge()["travel_routes"]["테스트존"] == ["남", "남", "맨홀 열", "아래"]
        print("reload merges routes.json into travel_routes OK")

        routes.delete_route("테스트존")
        assert "테스트존" not in routes.load()
        print("delete_route OK")
        print("\nALL RECORD TESTS PASSED")
    finally:
        if backup is not None:
            ROUTES.write_text(backup, encoding="utf-8")
        elif ROUTES.exists():
            ROUTES.unlink()


if __name__ == "__main__":
    main()
