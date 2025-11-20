from pathlib import Path

import pytest
import structlog

from productengineerbench.clients import Phase
from productengineerbench.runner import BenchmarkRunner
from productengineerbench.state import RunState


class _DummyGit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def rev_parse(self, ref: str = "HEAD") -> str:
        self.calls.append(f"rev_parse:{ref}")
        return "before-sha"

    def commit_all(self, message: str) -> str:
        self.calls.append(f"commit:{message}")
        return "after-sha"


@pytest.mark.asyncio
async def test_maybe_implement_skipped_marks_evaluating(tmp_path: Path) -> None:
    state = RunState(tmp_path, {}, {}, resume=False, force_resume=False)
    runner = object.__new__(BenchmarkRunner)
    runner.logger = structlog.get_logger("test")
    runner.state = state
    runner.skip_implement = True
    runner.evaluate_if_result_present = False
    runner.git = _DummyGit()
    runner._checkpoint_state = lambda *_args, **_kwargs: None

    phase = await BenchmarkRunner._maybe_implement(runner, "story.md", "text", Phase.PENDING)

    assert phase is Phase.EVALUATING
    status = state.ensure_story_status("story.md")
    assert status["phase"] == "evaluating"


@pytest.mark.asyncio
async def test_maybe_implement_records_commits(tmp_path: Path) -> None:
    state = RunState(tmp_path, {}, {}, resume=False, force_resume=False)
    runner = object.__new__(BenchmarkRunner)
    runner.logger = structlog.get_logger("test")
    runner.state = state
    runner.skip_implement = False
    runner.evaluate_if_result_present = False
    runner.git = _DummyGit()
    runner._checkpoint_state = lambda *_args, **_kwargs: None

    async def fake_impl(_story_text: str, _story_file: str) -> None:  # noqa: ANN001
        return None

    runner.implement_story = fake_impl  # type: ignore[assignment]
    runner.commit_story_changes = lambda sf: "after-sha"  # type: ignore[assignment]

    phase = await BenchmarkRunner._maybe_implement(runner, "story.md", "text", Phase.PENDING)

    assert phase is Phase.EVALUATING
    status = state.ensure_story_status("story.md")
    assert status["commits"]["before"] == "before-sha"
    assert status["commits"]["after_implement"] == "after-sha"
