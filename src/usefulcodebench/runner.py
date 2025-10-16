import asyncio
import json
import os
import subprocess
from pathlib import Path

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
)
from claude_agent_sdk.types import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


class CodeImplementationError(Exception):
    pass


class UATEvaluationError(Exception):
    pass


class BenchmarkRunner:
    def __init__(self, storymachine_config: dict, repo_config: dict):
        self.repo_config = repo_config
        self.storymachine_config = storymachine_config
        self.repo_dir = Path("repo")
        self.stories_dir = Path("stories")
        self.results_dir = Path(os.environ.get("RESULTS_DIR", "/results"))
        self.current_story_name = None
        self.implementer_model = os.environ.get("IMPLEMENTER_MODEL", "claude-haiku-4-5")
        self.evaluator_model = os.environ.get("EVALUATOR_MODEL", "claude-sonnet-4-5")

    def setup_git_user(self):
        """Configure git user details"""
        subprocess.run(
            ["git", "config", "--global", "user.name", "UsefulCodeBench"], check=True
        )
        subprocess.run(
            ["git", "config", "--global", "user.email", "benchrunner@example.com"],
            check=True,
        )

    def setup_git_credentials(self):
        """Configure git credentials for private repos"""
        git_token = os.environ.get("GIT_TOKEN")
        if not git_token:
            raise ValueError("GIT_TOKEN environment variable not set")

        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"], check=True
        )

        credentials_file = Path.home() / ".git-credentials"
        credentials_file.write_text(f"https://{git_token}:@github.com\n")
        credentials_file.chmod(0o600)

    def setup_repository(self):
        """Clone and setup repository based on config"""
        self.setup_git_credentials()
        self.setup_git_user()
        repo_config = self.repo_config["repository"]

        # Clone
        subprocess.run(
            ["git", "clone", repo_config["url"], str(self.repo_dir)], check=True
        )

        # Checkout revision
        subprocess.run(
            [
                "git",
                "checkout",
                "-b",
                f"eval_{repo_config['name']}",
                repo_config["revision"],
            ],
            cwd=self.repo_dir,
            check=True,
        )

        # Run setup commands
        setup = self.repo_config.get("setup", {})
        commands = setup.get("commands", [])
        if commands:
            script_path = self.repo_dir / ".bench_setup.sh"
            env_dump_path = self.repo_dir / ".bench_env"

            # Build a script that runs all repo-provided commands and then dumps env
            script_lines = [
                "#!/bin/bash",
                "set -euo pipefail",
                *commands,
                f'env -0 > "{env_dump_path.absolute()}"',
            ]
            script_path.write_text("\n".join(script_lines) + "\n")
            script_path.chmod(0o755)

            # IMPORTANT: do NOT use a login shell (-l). Just run bash normally.
            # Also run from repo cwd and invoke the script by its relative name.
            subprocess.run(
                ["/bin/bash", ".bench_setup.sh"],
                cwd=self.repo_dir,
                check=True,
                env={**os.environ, **setup.get("env", {})},
            )

            # Import the environment that the script produced
            if env_dump_path.exists():
                raw = env_dump_path.read_bytes().decode("utf-8", errors="ignore")
                for entry in raw.split("\x00"):
                    if not entry or "=" not in entry:
                        continue
                    k, v = entry.split("=", 1)
                    os.environ[k] = v

    def add_and_commit_changes(self, message: str):
        """Add all changes and commit them with the provided message"""
        try:
            subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
            subprocess.run(
                ["git", "commit", "-m", message], cwd=self.repo_dir, check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to add and commit changes: {e}")

    def generate_stories(self):
        """Run storymachine to generate stories"""
        sm_config = self.storymachine_config
        repo_config = self.repo_config["repository"]

        prd_path = self.repo_dir / repo_config["prd"]
        spec_path = self.repo_dir / repo_config["tech_spec"]

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

    async def implement_story(
        self, cwd: Path, story: str, story_name: str
    ) -> ResultMessage | None:
        self.current_story_name = story_name
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code"},
            max_turns=100,
            permission_mode="bypassPermissions",
            cwd=cwd,
            model=self.implementer_model,
        )
        async with ClaudeSDKClient(options) as client:
            # Send nonstreaming input
            prompt_path = Path(__file__).parent / "prompts" / "implementation.txt"
            user_message = prompt_path.read_text().format(story=story)
            await client.query(user_message)
            await self.log_message("implement", UserMessage(content=user_message))

            # Process responses
            async for message in client.receive_response():
                await self.log_message("implement", message)
                self.print_message_human_readable(message)

                if isinstance(message, ResultMessage) and message.subtype != "success":
                    raise CodeImplementationError(
                        f"Code implementation failed: {message}"
                    )

    async def evaluate_acceptance_criteria(
        self, cwd: Path, story: str, story_name: str
    ) -> ResultMessage | None:
        self.current_story_name = story_name
        options = ClaudeAgentOptions(
            max_turns=100,
            permission_mode="bypassPermissions",
            cwd=cwd,
            model=self.evaluator_model,
        )
        async with ClaudeSDKClient(options) as client:
            # Send nonstreaming input
            prompt_path = Path(__file__).parent / "prompts" / "evaluation.txt"
            user_message = prompt_path.read_text().format(story=story)
            await client.query(user_message)
            await self.log_message("evaluate", UserMessage(content=user_message))

            # Process responses
            async for message in client.receive_response():
                await self.log_message("evaluate", message)
                self.print_message_human_readable(message)

                if isinstance(message, ResultMessage) and message.subtype != "success":
                    raise UATEvaluationError(f"UAT evaluation failed: {message}")

    async def run_benchmark(self):
        """Run implementation + evaluation for all stories"""

        # Ensure results directory exists
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Get all text files from target_dir
        story_files = sorted(
            [f for f in os.listdir(self.stories_dir) if f.endswith(".md")]
        )

        result_file = self.repo_dir / "result.md"

        # Run run_claude for each story file
        for story_file in story_files:
            destination_file = self.results_dir / f"{Path(story_file).stem}.result.md"
            try:
                with open(os.path.join(self.stories_dir, story_file), "r") as f:
                    story_text = f.read()
                    print(f"Running claude for story: {story_file}")
                    await self.implement_story(
                        cwd=self.repo_dir, story=story_text, story_name=story_file
                    )
                    self.add_and_commit_changes(f"Implement story: {story_file}")
                    result = await self.evaluate_acceptance_criteria(
                        cwd=self.repo_dir, story=story_text, story_name=story_file
                    )
                    print(result)

                    if result_file.exists():
                        destination_file.write_text(result_file.read_text())
                        result_file.unlink()
                    else:
                        destination_file.write_text("no result")
            except (CodeImplementationError, UATEvaluationError) as e:
                destination_file.write_text(f"Result: Fail\n\n{e}")
                break

    async def log_message(self, phase: str, message):
        """Log Claude messages to JSONL file"""
        if self.current_story_name is None:
            return

        log_file = self.results_dir / f"{self.current_story_name}.{phase}.jsonl"

        # Serializer for message blocks
        def _to_jsonable(obj):
            from dataclasses import asdict, is_dataclass

            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, (list, tuple, set)):
                return [_to_jsonable(x) for x in obj]
            if isinstance(obj, dict):
                return {str(_to_jsonable(k)): _to_jsonable(v) for k, v in obj.items()}
            if isinstance(obj, TextBlock):
                return {"type": "text", "text": getattr(obj, "text", "")}
            if isinstance(obj, ToolUseBlock):
                return {
                    "type": "tool_use",
                    "id": getattr(obj, "id", ""),
                    "name": getattr(obj, "name", ""),
                    "input": _to_jsonable(getattr(obj, "input", {})),
                }
            if isinstance(obj, ToolResultBlock):
                return {
                    "type": "tool_result",
                    "tool_use_id": getattr(obj, "tool_use_id", ""),
                    "content": _to_jsonable(getattr(obj, "content", None)),
                    "is_error": getattr(obj, "is_error", None),
                }
            if isinstance(obj, ThinkingBlock):
                return {
                    "type": "thinking",
                    "thinking": getattr(obj, "thinking", ""),
                    "signature": getattr(obj, "signature", ""),
                }
            if is_dataclass(obj) and not isinstance(obj, type):
                return _to_jsonable(asdict(obj))
            if hasattr(obj, "__dict__"):
                return _to_jsonable(vars(obj))
            try:
                json.dumps(obj)
                return obj
            except TypeError:
                return str(obj)

        # Convert message to serializable format
        log_entry = {
            "type": type(message).__name__,
            "timestamp": asyncio.get_event_loop().time(),
        }

        if isinstance(message, UserMessage):
            log_entry["content"] = _to_jsonable(message.content)
        elif isinstance(message, AssistantMessage):
            # Handle different block types in assistant message
            content = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": _to_jsonable(getattr(block, "input", {})),
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": getattr(block, "tool_use_id", ""),
                            "content": _to_jsonable(getattr(block, "content", None)),
                            "is_error": getattr(block, "is_error", None),
                        }
                    )
                elif isinstance(block, ThinkingBlock):
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": getattr(block, "thinking", ""),
                            "signature": getattr(block, "signature", ""),
                        }
                    )
                else:
                    content.append(_to_jsonable(block))
            log_entry["content"] = content
        elif isinstance(message, ResultMessage):
            log_entry["content"] = _to_jsonable(message)

        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def print_message_human_readable(self, message):
        """Print Claude messages in human-readable format to stdout"""
        if isinstance(message, UserMessage):
            print(f"[User] {message.content}")
        elif isinstance(message, AssistantMessage):
            print("[Assistant]")
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(
                        f"[Tool Use] {getattr(block, 'name', '')}: {getattr(block, 'input', {})}"
                    )
                elif isinstance(block, ToolResultBlock):
                    print(
                        f"[Tool Result] {getattr(block, 'tool_use_id', '')}: {getattr(block, 'content', '')[:10]}..."
                    )
                elif isinstance(block, ThinkingBlock):
                    print(f"[Thinking] {getattr(block, 'thinking', '')}")
                else:
                    # Print other block types as their string representation
                    print(str(block))
        elif isinstance(message, ResultMessage):
            print("[Result]")
            # Print available attributes of ResultMessage
            for key, value in vars(message).items():
                if value is not None:
                    print(f"{key.capitalize()}: {value}")

    async def execute(self):
        """Main execution flow"""
        print("Setting up repository...")
        self.setup_repository()

        print("Generating stories...")
        self.generate_stories()

        print("Running benchmark...")
        await self.run_benchmark()


def run():
    repo_config_path = os.environ.get("REPO_CONFIG", "/data/repo.yaml")
    storymachine_config_path = os.environ.get(
        "STORYMACHINE_CONFIG", "/config/storymachine.yaml"
    )
    repo_config = yaml.safe_load(Path(repo_config_path).read_text())
    storymachine_config = yaml.safe_load(Path(storymachine_config_path).read_text())

    runner = BenchmarkRunner(
        storymachine_config=storymachine_config, repo_config=repo_config
    )
    asyncio.run(runner.execute())
