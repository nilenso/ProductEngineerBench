import json
from pathlib import Path

from productengineerbench.state import RunState


def test_ensure_story_status_creates_pending(tmp_path: Path) -> None:
    state = RunState(tmp_path, {}, {}, resume=False, force_resume=False)
    status = state.ensure_story_status("story-001.md")

    loaded = json.loads(state.status_path("story-001.md").read_text())

    assert status["phase"] == "pending"
    assert loaded["phase"] == "pending"
    assert loaded["story_file"] == "story-001.md"


def test_determine_resume_sha_prefers_after(tmp_path: Path) -> None:
    state = RunState(tmp_path, {}, {}, resume=False, force_resume=False)
    sf = "story-001.md"
    state.set_story_order([sf])
    state.record_commits(sf, before="base-sha")
    state.record_commits(sf, after="after-sha")

    resume_sha = state.determine_resume_sha()

    assert resume_sha == "after-sha"


def test_log_event_increments_seq(tmp_path: Path) -> None:
    state = RunState(tmp_path, {}, {}, resume=False, force_resume=False)
    sf = "story-001.md"
    state.start_session(sf, "implement")

    state.log_event(sf, "implement", "typeA", {"a": 1})
    state.log_event(sf, "implement", "typeB", {"b": 2})

    log = state.log_path(sf, "implement").read_text().strip().splitlines()
    entries = [json.loads(line) for line in log]
    assert [entry["seq"] for entry in entries] == [0, 1]
