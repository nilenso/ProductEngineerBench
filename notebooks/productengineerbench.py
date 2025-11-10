import marimo

__generated_with = "0.16.1"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    from claude_code_sdk import (
        ClaudeSDKClient,
        ClaudeCodeOptions,
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )
    from pathlib import Path
    import subprocess
    import os

    # load dotenv
    from dotenv import load_dotenv

    load_dotenv()
    return (
        AssistantMessage,
        ClaudeCodeOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        os,
        subprocess,
    )


@app.cell
def _(
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
):
    async def implement_story(cwd, story: str):
        options = ClaudeCodeOptions(
            max_turns=50, permission_mode="bypassPermissions", cwd=cwd
        )
        async with ClaudeSDKClient(options) as client:
            # Send nonstreaming input
            await client.query(f"Implement this story:\n{story}")

            # Process responses
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    print(message)
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(block.text)
    return (implement_story,)


@app.cell
def _(os, subprocess):
    def setup_git_repository(repo_url, revision, clone_dir="repo"):
        """
        Clone a git repository at a specific revision.

        Args:
            repo_url (str): URL of the git repository to clone
            revision (str): Revision (branch, tag, or commit hash) to checkout
            clone_dir (str): Directory to clone the repository into
        """
        # Clone the repository
        subprocess.run(["git", "clone", repo_url, clone_dir], check=True)

        # Checkout the specific revision
        subprocess.run(["git", "checkout", revision], check=True, cwd=clone_dir)

        print(f"Repository {repo_url} cloned and checked out to {revision}")


    def setup_storymachine(
        package, revision, prd, tech_spec, repo_dir="repo", target_dir="output"
    ):
        """
        Invoke the local 'uvx' tool to show help for the storymachine command
        from a specific revision.

        This runs:
            uvx --from <revision> storymachine --help

        The command is executed from the cloned repository directory ("repo").
        Returns the help text (stdout) as a string.
        """
        if not os.path.exists(repo_dir):
            raise FileNotFoundError(
                f"Repository directory '{repo_dir}' not found. Run setup_git_repository first."
            )

        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

        cmd = [
            "uvx",
            "--from",
            f"{package}@{revision}",
            "storymachine",
            "--prd",
            prd,
            "--tech-spec",
            tech_spec,
            "--repo",
            repo_dir,
            "--target",
            target_dir,
        ]
        try:
            result = subprocess.run(
                cmd, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            print(
                f"Command '{' '.join(cmd)}' failed with exit code {e.returncode}"
            )
            if e.stdout:
                print("stdout:")
                print(e.stdout)
            if e.stderr:
                print("stderr:")
                print(e.stderr)
            raise
        # Print and return the help text for further programmatic use
        print(result.stdout)
        return result.stdout


    repo_dir = "repo"
    target_dir = "output"

    if not os.path.exists(repo_dir):
        setup_git_repository(
            "../grand-central",
            "c0b34b1f25c4f9d503262d8256e2c2f9caaab275",
        )

    if not os.path.exists(target_dir):
        setup_storymachine(
            "git+https://github.com/nilenso/storymachine",
            "context-enhanced-stories-codex-impl",
            prd="/Users/atharva/storymachine/gc-prd.md",
            tech_spec="/Users/atharva/storymachine/gc-tech-spec.md",
            repo_dir=repo_dir,
            target_dir=target_dir,
        )
    return repo_dir, target_dir


@app.cell
def _(
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
):
    async def evaluate_acceptance_criteria(cwd, story: str):
        options = ClaudeCodeOptions(
            max_turns=50, permission_mode="bypassPermissions", cwd=cwd
        )
        async with ClaudeSDKClient(options) as client:
            # Send nonstreaming input
            await client.query(
                f"Setup and perform user acceptance tests to confirm if the code meets the given acceptance criteria for the given story:\n{story}\nProvide the results with commentary for each criteria in result.md."
            )

            # Process responses
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    return message
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(block.text)
    return (evaluate_acceptance_criteria,)


@app.cell
async def _(
    evaluate_acceptance_criteria,
    implement_story,
    os,
    repo_dir,
    target_dir,
):
    # Get all text files from target_dir
    story_files = [f for f in os.listdir(target_dir) if f.endswith(".md")]

    # Run run_claude for each story file
    for story_file in story_files:
        with open(os.path.join(target_dir, story_file), "r") as f:
            story_text = f.read()
            print(f"Running claude for story: {story_file}")
            await implement_story(cwd=repo_dir, story=story_text)
            result = await evaluate_acceptance_criteria(
                cwd=repo_dir, story=story_text
            )
            print(result)
    return


if __name__ == "__main__":
    app.run()
