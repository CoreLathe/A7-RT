"""
Tests for Repository.get_events().
Run standalone: python tests/test_get_events.py
No network calls.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.core.models import create_project
from a7_rt_core.storage.repository import Repository


def _ok(msg: str) -> None:
    print(f"  \033[32m[OK]\033[0m  {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31m[FAIL]\033[0m {msg}")


def run() -> bool:
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            _ok(label)
        else:
            _fail(label)
            errors.append(label)

    print("\n=== test_get_events ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = create_project(
            "test-proj", "get_events test", max_turns=10, drain_turn=8
        )
        repo = Repository.init(root, project)

        # 1. No events yet → []
        check("empty log returns []", repo.get_events() == [])

        # 2. Missing events.jsonl → []
        repo._events_path.unlink()
        check("missing file returns []", repo.get_events() == [])
        repo._events_path.touch()

        # Seed some events directly
        events = [
            {"turn": 0, "action": "dispatch", "target": "node_a"},
            {"turn": 1, "action": "validated", "target": "node_a"},
            {"turn": 2, "action": "node_updated", "target": "node_a"},
            {"turn": 3, "action": "dispatch", "target": "node_b"},
            {"turn": 4, "action": "death", "target": None},
        ]
        for e in events:
            repo.append_event(e)

        # 3. No filters → all events
        all_events = repo.get_events()
        check("no filters returns all events", len(all_events) == 5)

        # 4. since_turn filter
        check("since_turn=3 returns 2 events", len(repo.get_events(since_turn=3)) == 2)
        check("since_turn=4 returns 1 event", len(repo.get_events(since_turn=4)) == 1)
        check("since_turn=5 returns 0 events", len(repo.get_events(since_turn=5)) == 0)
        check(
            "since_turn=0 returns all events", len(repo.get_events(since_turn=0)) == 5
        )

        # 5. event_type filter
        check(
            "event_type=dispatch returns 2",
            len(repo.get_events(event_type="dispatch")) == 2,
        )
        check(
            "event_type=death returns 1", len(repo.get_events(event_type="death")) == 1
        )
        check(
            "event_type=unknown returns 0",
            len(repo.get_events(event_type="unknown")) == 0,
        )

        # 6. Combined filters
        check(
            "since_turn=3 + event_type=dispatch returns 1",
            len(repo.get_events(since_turn=3, event_type="dispatch")) == 1,
        )
        check(
            "since_turn=3 + event_type=dispatch is node_b",
            repo.get_events(since_turn=3, event_type="dispatch")[0]["target"]
            == "node_b",
        )

        # 7. Malformed lines are skipped
        with open(repo._events_path, "a") as f:
            f.write("THIS IS NOT JSON\n")
        check("malformed line skipped, rest intact", len(repo.get_events()) == 5)

    print()
    if errors:
        print(f"\033[31m{len(errors)} check(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        return False
    else:
        print("\033[32mAll checks passed.\033[0m")
        return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
