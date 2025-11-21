from __future__ import annotations

import os
import logging
import signal
import subprocess
import tempfile
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import structlog
from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent
from pydantic import SecretStr

from .clients import Phase, S3SyncClient, SubprocessGitClient, TarArchiver
from .config import load_repo_settings, load_storymachine_settings
from .state import RunState


class CodeImplementationError(Exception):
    pass


class UATEvaluationError(Exception):
    pass


def configure_logging() -> None:
    """Configure structlog with either console or JSON output.

    Default is console output; set LOG_FORMAT=json to emit structured logs.
    """

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log_format = os.environ.get("LOG_FORMAT", "console").lower()
    processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
    ]
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        context_class=dict,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


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

        self.logger = structlog.get_logger("productengineerbench.runner")

        self.sync_bucket = os.environ.get("SYNC_BUCKET")
        self.sync_prefix = os.environ.get("SYNC_PREFIX")
        self.resume_prefix = os.environ.get("RESUME_PREFIX")

        if self.sync_bucket and not self.sync_prefix:
            raise ValueError("SYNC_PREFIX must be set when SYNC_BUCKET is provided")
        if self.sync_prefix and not self.sync_bucket:
            raise ValueError("SYNC_BUCKET must be set when SYNC_PREFIX is provided")
        if self.resume_prefix and not self.sync_bucket:
            raise ValueError("RESUME_PREFIX requires SYNC_BUCKET to be set")

        resume_env = os.environ.get("RESUME", "0") == "1"
        self.resume = resume_env or bool(self.resume_prefix)
        self.force_resume = os.environ.get("FORCE_RESUME", "0") == "1"
        self.skip_generate_stories = os.environ.get("SKIP_GENERATE_STORIES", "0") == "1"
        self.skip_implement = os.environ.get("SKIP_IMPLEMENT", "0") == "1"
        self.skip_evaluate = os.environ.get("SKIP_EVALUATE", "0") == "1"
        self.force_retry_failed = os.environ.get("FORCE_RETRY_FAILED", "0") == "1"
        self.evaluate_if_result_present = (
            os.environ.get("EVALUATE_IF_RESULT_PRESENT", "0") == "1"
        )

        if self.resume and os.environ.get("RESUME") != "1":
            os.environ["RESUME"] = "1"

        self.results_root = Path(os.environ.get("RESULTS_DIR", "/results")).expanduser()
        self.run_root = self._resolve_run_root()
        self.run_id = os.environ.get("RUN_ID") or self.run_root.name
        self.logger = self.logger.bind(run_id=self.run_id)

        self.sync_client = S3SyncClient(self.sync_bucket) if self.sync_bucket else None
        self.sync_enabled = bool(self.sync_client and self.sync_prefix)
        self._final_status = "running"
        self._sigterm_handler_registered = False
        if self.resume_prefix and self.sync_client:
            self._download_resume_state()

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
        self.git = SubprocessGitClient(self.repo_dir)

        self.implementer_model = os.environ.get("IMPLEMENTER_MODEL", "claude-haiku-4-5")
        self.evaluator_model = os.environ.get("EVALUATOR_MODEL", "claude-sonnet-4-5")

        self._initialize_manifest()
        self.state.update_manifest_status("running")
        self._register_sigterm_handler()
        self._checkpoint_state("init")

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
            run_id=self.run_id,
            image=image_name,
            tools=tools_meta,
            models=models_meta,
            repo_meta=repo_meta,
        )
        self.state.update_manifest_models(models_meta)
        self.state.update_manifest_repo(repo_meta)

    def _register_sigterm_handler(self) -> None:
        if self._sigterm_handler_registered:
            return
        try:
            signal.signal(signal.SIGTERM, self._handle_sigterm)
            self._sigterm_handler_registered = True
        except ValueError:
            self._sigterm_handler_registered = False

    def _handle_sigterm(self, signum: int, frame: Optional[Any]) -> None:
        print("SIGTERM received; marking run cancelled and syncing results...")
        self.mark_cancelled()
        raise SystemExit(130)

    def _checkpoint_state(self, reason: str) -> None:
        if not self.sync_client or not self.sync_prefix:
            return
        try:
            self.state.flush()
            self.state.record_checkpoint(reason, extra={"run_id": self.run_id})
        except Exception as exc:
            self.logger.warning(
                "checkpoint_record_failed", reason=reason, error=str(exc)
            )
        try:
            sync_prefix = self.sync_prefix or ""
            self.sync_client.sync_directory(self.run_root, sync_prefix, delete=True)
            archive = TarArchiver.create_archive(self.run_root)
            try:
                archive_key = f"{sync_prefix.rstrip('/')}.tar.gz"
                self.sync_client.upload_file(archive, archive_key)
            finally:
                archive.unlink(missing_ok=True)
            self.logger.info("checkpoint_synced", reason=reason)
        except Exception as exc:
            self.logger.warning("checkpoint_sync_failed", reason=reason, error=str(exc))

    def _download_resume_state(self) -> None:
        if not self.resume_prefix or not self.sync_client:
            return
        archive_key = f"{self.resume_prefix.rstrip('/')}.tar.gz"
        fd, tmp_path = tempfile.mkstemp(prefix="resume_", suffix=".tar.gz")
        os.close(fd)
        archive_path = Path(tmp_path)
        try:
            self.logger.info("resume_download_start", key=archive_key)
            self.sync_client.download_file(archive_key, archive_path)
            if self.run_root.exists():
                for child in self.run_root.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            TarArchiver.extract_archive(archive_path, self.run_root)
            self.logger.info("resume_download_complete", path=str(self.run_root))
        finally:
            archive_path.unlink(missing_ok=True)

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
        git_username = os.environ.get("GIT_USERNAME", "x-access-token")

        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"], check=True
        )

        # Git expects the personal access token in the password slot.
        encoded_token = quote(git_token, safe="")
        encoded_username = quote(git_username, safe="")
        credentials_file = Path.home() / ".git-credentials"
        credentials_file.write_text(
            f"https://{encoded_username}:{encoded_token}@github.com\n"
        )
        credentials_file.chmod(0o600)

    def commit_story_changes(self, story_file: str) -> Optional[str]:
        sha = self.git.commit_all(f"Implement story: {story_file}")
        if sha is None:
            self.logger.warning("git_commit_failed", story=story_file)
        return sha

    def setup_repository(self) -> None:
        self.setup_git_credentials()
        self.setup_git_user()

        repo_cfg = self.repo_config["repository"]
        branch = f"eval_{repo_cfg['name']}"

        if self.resume and (self.repo_dir / ".git").exists():
            print("Reusing existing repository checkout")
            self.git.fetch_all()
            self.git.checkout(branch)
            resume_sha = self.state.determine_resume_sha()
            if resume_sha:
                self.git.reset_hard(resume_sha)
        else:
            if self.repo_dir.exists() and any(self.repo_dir.iterdir()):
                raise RuntimeError(
                    f"Repository directory {self.repo_dir} already exists and is not empty."
                )
            self.git.clone(repo_cfg["url"], self.repo_dir)
            self.git.checkout(branch, repo_cfg["revision"])

        self._run_repo_setup_commands()

        repo_meta = {
            "name": repo_cfg.get("name"),
            "url": repo_cfg.get("url"),
            "base_revision": repo_cfg.get("revision"),
            "branch": branch,
            "head": self.git.rev_parse("HEAD"),
        }
        self.state.update_manifest_repo(repo_meta)
        self._checkpoint_state("repo-ready")

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
        self._checkpoint_state("stories-ready")

    def implement_story(self, story: str, story_file: str) -> None:
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
        has_error = False

        def event_callback(event: Any) -> None:
            nonlocal has_error
            self.log_message(story_file, "implement", event)
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

        self.log_message(
            story_file, "implement", {"type": "user", "content": user_message}
        )
        conversation.send_message(user_message)
        conversation.run()

        if has_error:
            raise CodeImplementationError("Code implementation failed with errors")

    def evaluate_acceptance_criteria(self, story: str, story_file: str) -> None:
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
        has_error = False

        def event_callback(event: Any) -> None:
            nonlocal has_error
            self.log_message(story_file, "evaluate", event)
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

        self.log_message(
            story_file, "evaluate", {"type": "user", "content": user_message}
        )
        conversation.send_message(user_message)
        conversation.run()

        if has_error:
            raise UATEvaluationError("UAT evaluation failed with errors")

    def run_benchmark(self) -> None:
        story_order = (
            self.state.get_story_order() or self.state.rebuild_story_index_from_files()
        )
        if not story_order:
            self.logger.info("no_stories")
            return

        for story_file in story_order:
            story_path = self.stories_dir / story_file
            if not story_path.exists():
                self.logger.warning("story_missing", story=story_file)
                continue

            status = self.state.ensure_story_status(story_file)
            phase = self._normalize_phase(story_file, status)
            if phase is None:
                continue

            story_text = story_path.read_text()
            phase = self._maybe_implement(story_file, story_text, phase)
            if phase is None:
                continue
            self._maybe_evaluate(story_file, story_text, phase)

    def _normalize_phase(
        self, story_file: str, status: Dict[str, Any]
    ) -> Optional[Phase]:
        phase = Phase(status.get("phase", Phase.PENDING.value))
        if phase is Phase.COMPLETED:
            self.logger.info("story_skip_completed", story=story_file)
            return None
        if phase is Phase.FAILED:
            if not self.force_retry_failed:
                self.logger.info(
                    "story_skip_failed", story=story_file, hint="FORCE_RETRY_FAILED=1"
                )
                return None
            commits = status.get("commits") or {}
            if commits.get("after_implement"):
                phase = Phase.EVALUATING
                self.state.update_status(
                    story_file, phase=Phase.EVALUATING.value, error=None
                )
            else:
                phase = Phase.PENDING
                self.state.update_status(
                    story_file, phase=Phase.PENDING.value, error=None
                )
        return phase

    def _maybe_implement(
        self, story_file: str, story_text: str, phase: Phase
    ) -> Optional[Phase]:
        if self.skip_implement and phase in {Phase.PENDING, Phase.IMPLEMENTING}:
            self.logger.info("story_mark_evaluating_skip_impl", story=story_file)
            status = self.state.mark_evaluating(story_file)
            return Phase(status.get("phase", Phase.EVALUATING.value))

        if self.skip_implement or phase not in {Phase.PENDING, Phase.IMPLEMENTING}:
            return phase

        self.logger.info("story_implement_start", story=story_file)
        self.state.mark_implementing(story_file)
        before_sha = self.git.rev_parse("HEAD")
        try:
            self.implement_story(story_text, story_file)
        except CodeImplementationError as exc:
            self.state.record_commits(story_file, before=before_sha)
            summary = f"Result: Fail\n\nImplementation error: {exc}\n"
            self.state.write_result(story_file, summary)
            self.state.mark_failed(story_file, str(exc))
            self._checkpoint_state(f"{story_file}-implement-failed")
            return None

        after_sha = self.commit_story_changes(story_file)
        self.state.record_commits(story_file, before=before_sha, after=after_sha)
        status = self.state.mark_evaluating(story_file)
        phase = Phase(status.get("phase", Phase.EVALUATING.value))
        self._checkpoint_state(f"{story_file}-implement-committed")
        return phase

    def _maybe_evaluate(self, story_file: str, story_text: str, phase: Phase) -> None:
        if self.skip_evaluate:
            self.logger.info("story_skip_evaluate", story=story_file, phase=phase.value)
            return

        status = self.state.ensure_story_status(story_file)
        if status.get("phase") == Phase.COMPLETED.value:
            self.logger.info("story_skip_eval_completed", story=story_file)
            return
        if status.get("phase") == Phase.IMPLEMENTING.value:
            self.logger.info("story_skip_eval_implementing", story=story_file)
            return
        if (
            status.get("phase") == Phase.EVALUATING.value
            and self.evaluate_if_result_present
            and self.state.story_result_exists(story_file)
        ):
            self.logger.info("story_evaluate_skip_result_present", story=story_file)
            self.state.mark_completed(story_file)
            self._checkpoint_state(f"{story_file}-evaluate-auto-completed")
            return

        if status.get("phase") not in {
            Phase.EVALUATING.value,
            Phase.IMPLEMENTING.value,
            Phase.PENDING.value,
        }:
            return

        self.logger.info("story_evaluate_start", story=story_file)
        try:
            self.evaluate_acceptance_criteria(story_text, story_file)
        except UATEvaluationError as exc:
            summary = f"Result: Fail\n\nEvaluation error: {exc}\n"
            self.state.write_result(story_file, summary)
            self.state.mark_failed(story_file, str(exc))
            self._checkpoint_state(f"{story_file}-evaluate-failed")
            return

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
        self._checkpoint_state(f"{story_file}-evaluate-completed")

    def log_message(self, story_file: str, phase: str, event: Any) -> None:
        event_type, content = self._serialize_event(event)
        self.state.log_event(story_file, phase, event_type, content)

    @staticmethod
    def _serialize_event(event: Any) -> Tuple[str, Any]:
        def _to_jsonable(obj: Any) -> Any:
            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, (list, tuple, set)):
                return [_to_jsonable(x) for x in obj]
            if isinstance(obj, dict):
                return {str(_to_jsonable(k)): _to_jsonable(v) for k, v in obj.items()}
            if is_dataclass(obj) and not isinstance(obj, type):
                return _to_jsonable(asdict(obj))
            to_dict = getattr(obj, "to_dict", None)
            if callable(to_dict):
                return _to_jsonable(to_dict())
            if hasattr(obj, "__dict__"):
                return _to_jsonable(vars(obj))
            return str(obj)

        event_type = type(event).__name__
        if isinstance(event, dict):
            event_type = event.get("type", event_type)
            content = _to_jsonable(event)
        elif callable(getattr(event, "to_dict", None)):
            payload = event.to_dict()
            event_type = payload.get("event_type", event_type)
            content = _to_jsonable(payload)
        else:
            content = _to_jsonable(event)
        return event_type, content

    @staticmethod
    def print_event_human_readable(event: Any) -> None:
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

    def execute(self) -> None:
        self.logger.info("repo_setup_start")
        self.setup_repository()

        self.logger.info("story_generation_start")
        self.generate_stories()

        self.logger.info("benchmark_start")
        self.run_benchmark()

    def finalize(self) -> None:
        if self.sync_enabled:
            self._checkpoint_state("finalize")

    def mark_success(self) -> None:
        if self._final_status != "running":
            return
        self._final_status = "success"
        self.state.update_manifest_status("success")

    def mark_failure(self) -> None:
        if self._final_status == "cancelled":
            return
        self._final_status = "failure"
        self.state.update_manifest_status("failure")

    def mark_cancelled(self) -> None:
        if self._final_status == "cancelled":
            return
        self._final_status = "cancelled"
        self.state.update_manifest_status("cancelled")

    @property
    def final_status(self) -> str:
        return self._final_status


def run() -> None:
    configure_logging()
    repo_config_path = Path(os.environ.get("REPO_CONFIG", "/data/repo.yaml"))
    storymachine_config_path = Path(
        os.environ.get("STORYMACHINE_CONFIG", "/config/storymachine.yaml")
    )

    repo_settings = load_repo_settings(repo_config_path)
    storymachine_settings = load_storymachine_settings(storymachine_config_path)

    repo_config = repo_settings.model_dump(mode="python")
    storymachine_config = storymachine_settings.model_dump(mode="python")

    runner = BenchmarkRunner(
        storymachine_config=storymachine_config,
        repo_config=repo_config,
        repo_config_path=repo_config_path,
        storymachine_config_path=storymachine_config_path,
    )
    try:
        runner.execute()
    except KeyboardInterrupt:
        runner.mark_cancelled()
        raise
    except SystemExit:
        if runner.final_status != "cancelled":
            runner.mark_failure()
        raise
    except Exception:
        runner.mark_failure()
        raise
    else:
        runner.mark_success()
    finally:
        runner.finalize()
