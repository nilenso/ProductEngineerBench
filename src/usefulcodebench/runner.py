import asyncio
import json
import os
import subprocess
from pathlib import Path

import yaml
from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent
from pydantic import SecretStr


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

    async def implement_story(self, cwd: Path, story: str, story_name: str) -> None:
        self.current_story_name = story_name

        # Configure LLM
        api_key = os.environ.get("LLM_API_KEY")
        if not api_key:
            raise ValueError("LLM_API_KEY environment variable not set")

        base_url = os.environ.get("LLM_BASE_URL")

        llm = LLM(
            model=self.implementer_model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            usage_id="implementer",
            max_input_tokens=os.getenv("MAX_INPUT_TOKENS", 100000),
            max_output_tokens=os.getenv("MAX_OUTPUT_TOKENS", 20000),
        )

        # Create agent with default tools
        agent = get_default_agent(llm=llm, cli_mode=True)

        # Prepare user message
        prompt_path = Path(__file__).parent / "prompts" / "implementation.txt"
        user_message = prompt_path.read_text().format(story=story)

        # Track events and errors
        events_log = []
        has_error = False

        # Get event loop for scheduling coroutines from thread
        loop = asyncio.get_event_loop()

        def event_callback(event):
            """Callback to capture events during conversation"""
            nonlocal has_error
            events_log.append(event)

            # Log and print event
            # Use run_coroutine_threadsafe since callback runs in executor thread
            asyncio.run_coroutine_threadsafe(
                self.log_message("implement", event), loop
            )
            self.print_event_human_readable(event)

            # Check for errors or failures
            event_dict = event.to_dict() if hasattr(event, "to_dict") else {}
            if (
                event_dict.get("source") == "agent"
                and "error" in str(event_dict).lower()
            ):
                has_error = True

        # Create conversation with callback
        conversation = Conversation(
            agent=agent,
            workspace=str(cwd),
            callbacks=[event_callback],
        )

        # Log user message
        await self.log_message("implement", {"type": "user", "content": user_message})

        # Send message and run conversation
        conversation.send_message(user_message)

        # Run in executor since conversation.run() is synchronous
        await loop.run_in_executor(None, conversation.run)

        # Check for errors
        if has_error:
            raise CodeImplementationError("Code implementation failed with errors")

    async def evaluate_acceptance_criteria(
        self, cwd: Path, story: str, story_name: str
    ) -> None:
        self.current_story_name = story_name

        # Configure LLM
        api_key = os.environ.get("LLM_API_KEY")
        if not api_key:
            raise ValueError("LLM_API_KEY environment variable not set")

        base_url = os.environ.get("LLM_BASE_URL")

        llm = LLM(
            model=self.evaluator_model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            usage_id="evaluator",
        )

        # Create agent with default tools
        agent = get_default_agent(llm=llm, cli_mode=True)

        # Prepare user message
        prompt_path = Path(__file__).parent / "prompts" / "evaluation.txt"
        user_message = prompt_path.read_text().format(story=story)

        # Track events and errors
        events_log = []
        has_error = False

        # Get event loop for scheduling coroutines from thread
        loop = asyncio.get_event_loop()

        def event_callback(event):
            """Callback to capture events during conversation"""
            nonlocal has_error
            events_log.append(event)

            # Log and print event
            # Use run_coroutine_threadsafe since callback runs in executor thread
            asyncio.run_coroutine_threadsafe(
                self.log_message("evaluate", event), loop
            )
            self.print_event_human_readable(event)

            # Check for errors or failures
            event_dict = event.to_dict() if hasattr(event, "to_dict") else {}
            if (
                event_dict.get("source") == "agent"
                and "error" in str(event_dict).lower()
            ):
                has_error = True

        # Create conversation with callback
        conversation = Conversation(
            agent=agent,
            workspace=str(cwd),
            callbacks=[event_callback],
        )

        # Log user message
        await self.log_message("evaluate", {"type": "user", "content": user_message})

        # Send message and run conversation
        conversation.send_message(user_message)

        # Run in executor since conversation.run() is synchronous
        await loop.run_in_executor(None, conversation.run)

        # Check for errors
        if has_error:
            raise UATEvaluationError("UAT evaluation failed with errors")

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
                    print(f"Running agent for story: {story_file}")
                    await self.implement_story(
                        cwd=self.repo_dir, story=story_text, story_name=story_file
                    )
                    self.add_and_commit_changes(f"Implement story: {story_file}")
                    await self.evaluate_acceptance_criteria(
                        cwd=self.repo_dir, story=story_text, story_name=story_file
                    )

                    if result_file.exists():
                        destination_file.write_text(result_file.read_text())
                        result_file.unlink()
                    else:
                        destination_file.write_text("no result")
            except (CodeImplementationError, UATEvaluationError) as e:
                destination_file.write_text(f"Result: Fail\n\n{e}")
                break

    async def log_message(self, phase: str, event):
        """Log Openhands events to JSONL file"""
        if self.current_story_name is None:
            return

        log_file = self.results_dir / f"{self.current_story_name}.{phase}.jsonl"

        # Serializer for event objects
        def _to_jsonable(obj):
            from dataclasses import asdict, is_dataclass

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
            try:
                json.dumps(obj)
                return obj
            except TypeError:
                return str(obj)

        # Convert event to serializable format
        log_entry = {
            "type": type(event).__name__,
            "timestamp": asyncio.get_event_loop().time(),
        }

        # Handle different event types
        if isinstance(event, dict):
            # User message or simple dict
            log_entry["content"] = _to_jsonable(event)
        elif hasattr(event, "to_dict"):
            # Openhands Event object
            log_entry["content"] = _to_jsonable(event.to_dict())
        else:
            # Fallback: serialize as is
            log_entry["content"] = _to_jsonable(event)

        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def print_event_human_readable(self, event):
        """Print Openhands events in human-readable format to stdout"""
        if isinstance(event, dict):
            # User message
            print(f"[User] {event.get('content', event)}")
        elif hasattr(event, "to_dict"):
            # Openhands Event object
            event_dict = event.to_dict()
            event_type = event_dict.get("event_type", type(event).__name__)
            source = event_dict.get("source", "unknown")

            # Format based on event type
            if "action" in event_type.lower():
                action_name = event_dict.get("action", event_type)
                print(f"[{source.upper()} Action] {action_name}")

                # Print action details if available
                if "args" in event_dict:
                    print(f"  Args: {json.dumps(event_dict['args'], indent=2)[:200]}")
                if "thought" in event_dict:
                    print(f"  Thought: {event_dict['thought'][:200]}")

            elif "observation" in event_type.lower():
                obs_name = event_dict.get("observation", event_type)
                print(f"[{source.upper()} Observation] {obs_name}")

                # Print observation content if available
                if "content" in event_dict:
                    content = str(event_dict["content"])[:200]
                    print(f"  Content: {content}")
                if "output" in event_dict:
                    output = str(event_dict["output"])[:200]
                    print(f"  Output: {output}")
            else:
                # Generic event
                print(f"[{source.upper()} {event_type}]")
                print(f"  {json.dumps(event_dict, indent=2)[:200]}")
        else:
            # Fallback: print string representation
            print(f"[Event] {str(event)[:200]}")

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
