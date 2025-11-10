import asyncio
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent
from pydantic import SecretStr

from .state import RunState


class CodeImplementationError(Exception):
    pass


class UATEvaluationError(Exception):
    pass


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class BenchmarkRunner:
    def __init__(
        self,
        storymachine_config: Dict[str, Any],
        repo_config: Dict[str, Any],
        *,
        repo_config_path: Path,
        storymachine_config_path: Path,
    ) -> None:
        self.repo_config = repo_config
        self.storymachine_config = storymachine_config
        self.repo_config_path = repo_config_path
        self.storymachine_config_path = storymachine_config_path

        self.resume = os.environ.get("RESUME", "0") == "1"
        self.force_resume = os.environ.get("FORCE_RESUME", "0") == "1"
        self.skip_generate_stories = os.environ.get("SKIP_GENERATE_STORIES", "0") == "1"
        self.skip_implement = os.environ.get("SKIP_IMPLEMENT", "0") == "1"
        self.skip_evaluate = os.environ.get("SKIP_EVALUATE", "0") == "1"
        self.force_retry_failed = os.environ.get("FORCE_RETRY_FAILED", "0") == "1"
        self.evaluate_if_result_present = (
            os.environ.get("EVALUATE_IF_RESULT_PRESENT", "0") == "1"
        )

        self.results_root = Path(os.environ.get("RESULTS_DIR", "/results")).expanduser()
        self.run_root = self._resolve_run_root()

        self.state = RunState(
            self.run_root,
            repo_config,
            storymachine_config,
            resume=self.resume,
            force_resume=self.force_resume,
        )
        self.state.prepare_directories()
        self.state.copy_configs(self.repo_config_path, self.storymachine_config_path)

        self.repo_dir = self.state.repo_dir
        self.stories_dir = self.state.stories_dir
        self.current_story_file: Optional[str] = None

        self.implementer_model = os.environ.get("IMPLEMENTER_MODEL", "claude-haiku-4-5")
        self.evaluator_model = os.environ.get("EVALUATOR_MODEL", "claude-sonnet-4-5")

        self._initialize_manifest()

    def _resolve_run_root(self) -> Path:
        run_dir_env = os.environ.get("RUN_DIR")
        if run_dir_env:
            candidate = Path(run_dir_env).expanduser()
            if not candidate.is_absolute():
                candidate = (self.results_root / candidate).resolve()
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate

        if self.resume:
            self.results_root.mkdir(parents=True, exist_ok=True)
            return self.results_root

        repo_name = self.repo_config.get("repository", {}).get("name", "run")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_dir = self.results_root / f"{repo_name}_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _initialize_manifest(self) -> None:
        try:
            import importlib.metadata as metadata

            openhands_version = metadata.version("openhands")
        except Exception:
            openhands_version = "unknown"

        image_name = (
            os.environ.get("BENCHMARK_IMAGE")
            or os.environ.get("IMAGE_NAME")
            or "benchmark-runner"
        )

        repo_cfg = self.repo_config.get("repository", {})
        branch = f"eval_{repo_cfg.get('name')}" if repo_cfg.get("name") else None

        models_meta = {
            "implementer": {
                "name": self.implementer_model,
                "base_url": os.environ.get("IMPLEMENTER_LLM_BASE_URL"),
                "max_input_tokens": _env_int("MAX_INPUT_TOKENS", 100000),
                "max_output_tokens": _env_int("MAX_OUTPUT_TOKENS", 20000),
            },
            "evaluator": {
                "name": self.evaluator_model,
                "base_url": os.environ.get("EVALUATOR_LLM_BASE_URL"),
            },
        }

        tools_meta = {
            "openhands_sdk": openhands_version,
            "storymachine": {
                "package": self.storymachine_config.get("package"),
                "version": self.storymachine_config.get("version"),
            },
        }

        repo_meta = {
            "name": repo_cfg.get("name"),
            "url": repo_cfg.get("url"),
            "base_revision": repo_cfg.get("revision"),
            "branch": branch,
        }

        if self.resume:
            self.state.validate_resume_compatibility()

        self.state.initialize_manifest(
            run_id=self.run_root.name,
            image=image_name,
            tools=tools_meta,
            models=models_meta,
            repo_meta=repo_meta,
        )
        self.state.update_manifest_models(models_meta)
        self.state.update_manifest_repo(repo_meta)

    def setup_git_user(self) -> None:
        subprocess.run(
            ["git", "config", "--global", "user.name", "ProductEngineerBench"],
            check=True,
        )
        subprocess.run(
            ["git", "config", "--global", "user.email", "benchrunner@example.com"],
            check=True,
        )

    def setup_git_credentials(self) -> None:
        git_token = os.environ.get("GIT_TOKEN")
        if not git_token:
            raise ValueError("GIT_TOKEN environment variable not set")

        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"], check=True
        )

        credentials_file = Path.home() / ".git-credentials"
        credentials_file.write_text(f"https://{git_token}:@github.com\n")
        credentials_file.chmod(0o600)

    def _git(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = False,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            check=check,
            capture_output=capture_output,
            text=text,
        )

    def git_rev_parse(self, ref: str = "HEAD") -> Optional[str]:
        try:
            result = self._git("rev-parse", ref, capture_output=True)
        except subprocess.CalledProcessError:
            return None
        return result.stdout.strip()

    def git_has_changes(self) -> bool:
        result = self._git("status", "--porcelain", capture_output=True)
        return bool(result.stdout.strip())

    def commit_story_changes(self, story_file: str) -> Optional[str]:
        if not self.git_has_changes():
            return self.git_rev_parse("HEAD")
        try:
            self._git("add", "--all")
            self._git("commit", "-m", f"Implement story: {story_file}")
        except subprocess.CalledProcessError as exc:
            print(f"Warning: failed to commit changes for {story_file}: {exc}")
        return self.git_rev_parse("HEAD")

    def setup_repository(self) -> None:
        self.setup_git_credentials()
        self.setup_git_user()

        repo_cfg = self.repo_config["repository"]
        branch = f"eval_{repo_cfg['name']}"

        if self.resume and (self.repo_dir / ".git").exists():
            print("Reusing existing repository checkout")
            self._git("fetch", "--all", "--tags", "--prune", check=False)
            self._git("checkout", branch)
            resume_sha = self.state.determine_resume_sha()
            if resume_sha:
                self._git("reset", "--hard", resume_sha)
        else:
            if self.repo_dir.exists() and any(self.repo_dir.iterdir()):
                raise RuntimeError(
                    f"Repository directory {self.repo_dir} already exists and is not empty."
                )
            subprocess.run(
                ["git", "clone", repo_cfg["url"], str(self.repo_dir)], check=True
            )
            self._git("checkout", "-B", branch, repo_cfg["revision"])

        self._run_repo_setup_commands()

        repo_meta = {
            "name": repo_cfg.get("name"),
            "url": repo_cfg.get("url"),
            "base_revision": repo_cfg.get("revision"),
            "branch": branch,
            "head": self.git_rev_parse("HEAD"),
        }
        self.state.update_manifest_repo(repo_meta)

    def _run_repo_setup_commands(self) -> None:
        setup = self.repo_config.get("setup", {})
        commands = setup.get("commands", [])
        if not commands:
            return

        script_path = self.repo_dir / ".bench_setup.sh"
        env_dump_path = self.repo_dir / ".bench_env"
        script_lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            *commands,
            f'env -0 > "{env_dump_path.absolute()}"',
        ]
        script_path.write_text("\n".join(script_lines) + "\n")
        script_path.chmod(0o755)

        subprocess.run(
            ["/bin/bash", ".bench_setup.sh"],
            cwd=self.repo_dir,
            check=True,
            env={**os.environ, **setup.get("env", {})},
        )

        if env_dump_path.exists():
            raw = env_dump_path.read_bytes().decode("utf-8", errors="ignore")
            for entry in raw.split("\x00"):
                if not entry or "=" not in entry:
                    continue
                key, value = entry.split("=", 1)
                os.environ[key] = value

    def generate_stories(self) -> None:
        if self.skip_generate_stories:
            print("SKIP_GENERATE_STORIES=1 set; skipping story generation.")
            if not self.state.get_story_order():
                self.state.rebuild_story_index_from_files()
            return

        existing_story_files = sorted(self.stories_dir.glob("*.md"))
        if self.resume and existing_story_files:
            print("Stories already exist; resume skipping generation.")
            if not self.state.get_story_order():
                self.state.rebuild_story_index_from_files()
            return

        sm_config = self.storymachine_config
        repo_cfg = self.repo_config["repository"]

        prd_path = self.repo_dir / repo_cfg["prd"]
        spec_path = self.repo_dir / repo_cfg["tech_spec"]

        cmd = [
            "uvx",
            "--from",
            f"{sm_config['package']}@{sm_config['version']}",
            "storymachine",
            "--prd",
            str(prd_path),
            "--tech-spec",
            str(spec_path),
            "--repo",
            str(self.repo_dir),
            "--target",
            str(self.stories_dir),
        ]

        subprocess.run(cmd, check=True)

        story_files = sorted([path.name for path in self.stories_dir.glob("*.md")])
        self.state.set_story_order(story_files)

    async def implement_story(self, story: str, story_file: str) -> None:
        self.current_story_file = story_file
        self.state.start_session(story_file, "implement")

        api_key = os.environ.get("IMPLEMENTER_LLM_API_KEY")
        if not api_key:
            raise ValueError("IMPLEMENTER_LLM_API_KEY environment variable not set")

        base_url = os.environ.get("IMPLEMENTER_LLM_BASE_URL")

        llm = LLM(
            model=self.implementer_model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            usage_id="implementer",
            max_input_tokens=_env_int("MAX_INPUT_TOKENS", 100000),
            max_output_tokens=_env_int("MAX_OUTPUT_TOKENS", 20000),
        )

        agent = get_default_agent(llm=llm, cli_mode=True)

        prompt_path = Path(__file__).parent / "prompts" / "implementation.txt"
        user_message = prompt_path.read_text().format(story=story)

        loop = asyncio.get_event_loop()
        has_error = False

        def event_callback(event: Any) -> None:
            nonlocal has_error
            asyncio.run_coroutine_threadsafe(self.log_message("implement", event), loop)
            self.print_event_human_readable(event)
            event_dict = (
                event.to_dict()
                if hasattr(event, "to_dict")
                else event
                if isinstance(event, dict)
                else {}
            )
            if isinstance(event_dict, dict):
                message = str(event_dict)
                if "error" in message.lower():
                    has_error = True

        conversation = Conversation(
            agent=agent,
            workspace=str(self.repo_dir),
            callbacks=[event_callback],
        )

        await self.log_message("implement", {"type": "user", "content": user_message})
        conversation.send_message(user_message)
        await loop.run_in_executor(None, conversation.run)

        if has_error:
            raise CodeImplementationError("Code implementation failed with errors")

    async def evaluate_acceptance_criteria(self, story: str, story_file: str) -> None:
        self.current_story_file = story_file
        self.state.start_session(story_file, "evaluate")

        api_key = os.environ.get("EVALUATOR_LLM_API_KEY")
        if not api_key:
            raise ValueError("EVALUATOR_LLM_API_KEY environment variable not set")

        base_url = os.environ.get("EVALUATOR_LLM_BASE_URL")

        llm = LLM(
            model=self.evaluator_model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            usage_id="evaluator",
        )

        agent = get_default_agent(llm=llm, cli_mode=True)

        prompt_path = Path(__file__).parent / "prompts" / "evaluation.txt"
        user_message = prompt_path.read_text().format(story=story)

        loop = asyncio.get_event_loop()
        has_error = False

        def event_callback(event: Any) -> None:
            nonlocal has_error
            asyncio.run_coroutine_threadsafe(self.log_message("evaluate", event), loop)
            self.print_event_human_readable(event)
            event_dict = (
                event.to_dict()
                if hasattr(event, "to_dict")
                else event
                if isinstance(event, dict)
                else {}
            )
            if isinstance(event_dict, dict):
                message = str(event_dict)
                if "error" in message.lower():
                    has_error = True

        conversation = Conversation(
            agent=agent,
            workspace=str(self.repo_dir),
            callbacks=[event_callback],
        )

        await self.log_message("evaluate", {"type": "user", "content": user_message})
        conversation.send_message(user_message)
        await loop.run_in_executor(None, conversation.run)

        if has_error:
            raise UATEvaluationError("UAT evaluation failed with errors")

    async def run_benchmark(self) -> None:
        story_order = self.state.get_story_order()
        if not story_order:
            story_order = self.state.rebuild_story_index_from_files()
        if not story_order:
            print("No stories found; nothing to run.")
            return

        for story_file in story_order:
            story_path = self.stories_dir / story_file
            if not story_path.exists():
                print(f"Story file {story_file} missing, skipping.")
                continue

            status = self.state.ensure_story_status(story_file)
            phase = status.get("phase", "pending")

            if phase == "completed":
                print(f"Story {story_file} already completed; skipping.")
                continue

            if phase == "failed":
                if not self.force_retry_failed:
                    print(
                        f"Story {story_file} previously failed; skipping (set FORCE_RETRY_FAILED=1 to retry)."
                    )
                    continue
                commits = status.get("commits") or {}
                if commits.get("after_implement"):
                    phase = "evaluating"
                    status = self.state.update_status(
                        story_file, phase="evaluating", error=None
                    )
                else:
                    phase = "pending"
                    status = self.state.update_status(
                        story_file, phase="pending", error=None
                    )

            story_text = story_path.read_text()

            try:
                if not self.skip_implement and phase in {"pending", "implementing"}:
                    print(f"Implementing story: {story_file}")
                    self.state.mark_implementing(story_file)
                    before_sha = self.git_rev_parse("HEAD")
                    try:
                        await self.implement_story(story_text, story_file)
                    except CodeImplementationError as exc:
                        self.state.record_commits(story_file, before=before_sha)
                        summary = f"Result: Fail\n\nImplementation error: {exc}\n"
                        self.state.write_result(story_file, summary)
                        self.state.mark_failed(story_file, str(exc))
                        continue

                    after_sha = self.commit_story_changes(story_file)
                    self.state.record_commits(
                        story_file, before=before_sha, after=after_sha
                    )
                    status = self.state.mark_evaluating(story_file)
                    phase = status.get("phase", "evaluating")
                elif self.skip_implement and phase in {"pending", "implementing"}:
                    print(
                        f"SKIP_IMPLEMENT=1 set; marking {story_file} ready for evaluation."
                    )
                    status = self.state.mark_evaluating(story_file)
                    phase = status.get("phase", "evaluating")

                if self.skip_evaluate:
                    print(
                        f"SKIP_EVALUATE=1 set; leaving {story_file} in phase={phase}."
                    )
                    continue

                status = self.state.ensure_story_status(story_file)
                if status.get("phase") == "completed":
                    print(f"Story {story_file} already completed; skipping evaluation.")
                    continue

                if status.get("phase") == "implementing":
                    print(
                        f"Story {story_file} still implementing; skipping evaluation this pass."
                    )
                    continue

                if (
                    status.get("phase") == "evaluating"
                    and self.evaluate_if_result_present
                    and self.state.story_result_exists(story_file)
                ):
                    print(
                        f"Result already present for {story_file}; marking as completed."
                    )
                    self.state.mark_completed(story_file)
                    continue

                if status.get("phase") in {"evaluating", "implementing", "pending"}:
                    print(f"Evaluating story: {story_file}")
                    try:
                        await self.evaluate_acceptance_criteria(story_text, story_file)
                    except UATEvaluationError as exc:
                        summary = f"Result: Fail\n\nEvaluation error: {exc}\n"
                        self.state.write_result(story_file, summary)
                        self.state.mark_failed(story_file, str(exc))
                        continue

                    repo_result = self.repo_dir / "result.md"
                    if repo_result.exists():
                        content = repo_result.read_text()
                        self.state.write_result(story_file, content)
                        repo_result.unlink()
                    else:
                        self.state.write_result(
                            story_file,
                            "Result: Success\n\nNo detailed report provided.",
                        )
                    self.state.mark_completed(story_file)

            finally:
                self.current_story_file = None

    async def log_message(self, phase: str, event: Any) -> None:
        if self.current_story_file is None:
            return
        event_type, content = self._serialize_event(event)
        await asyncio.to_thread(
            self.state.log_event, self.current_story_file, phase, event_type, content
        )

    def _serialize_event(self, event: Any) -> Tuple[str, Any]:
        def _to_jsonable(obj: Any) -> Any:
            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, (list, tuple, set)):
                return [_to_jsonable(x) for x in obj]
            if isinstance(obj, dict):
                return {str(_to_jsonable(k)): _to_jsonable(v) for k, v in obj.items()}
            if is_dataclass(obj) and not isinstance(obj, type):
                return _to_jsonable(asdict(obj))
            if hasattr(obj, "to_dict"):
                return _to_jsonable(obj.to_dict())
            if hasattr(obj, "__dict__"):
                return _to_jsonable(vars(obj))
            return str(obj)

        event_type = type(event).__name__
        if isinstance(event, dict):
            event_type = event.get("type", event_type)
            content = _to_jsonable(event)
        elif hasattr(event, "to_dict"):
            payload = event.to_dict()
            event_type = payload.get("event_type", event_type)
            content = _to_jsonable(payload)
        else:
            content = _to_jsonable(event)
        return event_type, content

    def print_event_human_readable(self, event: Any) -> None:
        if isinstance(event, dict):
            print(f"[User] {event.get('content', event)}")
            return
        if hasattr(event, "to_dict"):
            event_dict = event.to_dict()
            event_type = event_dict.get("event_type", type(event).__name__)
            source = event_dict.get("source", "unknown")

            if "action" in event_type.lower():
                action_name = event_dict.get("action", event_type)
                print(f"[{source.upper()} Action] {action_name}")
                if "args" in event_dict:
                    print(f"  Args: {event_dict['args']}")
                if "thought" in event_dict:
                    print(f"  Thought: {event_dict['thought']}")
            elif "observation" in event_type.lower():
                obs_name = event_dict.get("observation", event_type)
                print(f"[{source.upper()} Observation] {obs_name}")
                if "content" in event_dict:
                    print(f"  Content: {event_dict['content']}")
                if "output" in event_dict:
                    print(f"  Output: {event_dict['output']}")
            else:
                print(f"[{source.upper()} {event_type}]")
                print(f"  {event_dict}")
            return
        print(f"[Event] {str(event)}")

    async def execute(self) -> None:
        print("Setting up repository...")
        self.setup_repository()

        print("Generating stories...")
        self.generate_stories()

        print("Running benchmark...")
        await self.run_benchmark()


def run() -> None:
    repo_config_path = Path(os.environ.get("REPO_CONFIG", "/data/repo.yaml"))
    storymachine_config_path = Path(
        os.environ.get("STORYMACHINE_CONFIG", "/config/storymachine.yaml")
    )

    repo_config = yaml.safe_load(repo_config_path.read_text())
    storymachine_config = yaml.safe_load(storymachine_config_path.read_text())

    runner = BenchmarkRunner(
        storymachine_config=storymachine_config,
        repo_config=repo_config,
        repo_config_path=repo_config_path,
        storymachine_config_path=storymachine_config_path,
    )
    asyncio.run(runner.execute())
