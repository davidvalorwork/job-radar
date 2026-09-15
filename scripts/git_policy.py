"""Small, offline Git Flow / Conventional Commit policy shared by hooks and CI."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

SLUG = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
WORK_BRANCH = re.compile(rf"codex/(feature|fix|chore|docs|refactor|test|ci|release|hotfix)/{SLUG}")
BOT_BRANCH = re.compile(r"dependabot/(pip|uv|github_actions)/[A-Za-z0-9._/-]+")
SUBJECT = re.compile(
    r"(build|chore|ci|docs|feat|fix|perf|refactor|revert|test)"
    r"(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S(?:[^\r\n]*\S)?"
)


def valid_subject(subject: str) -> bool:
    return len(subject) <= 100 and SUBJECT.fullmatch(subject) is not None


def valid_work_branch(branch: str) -> bool:
    return WORK_BRANCH.fullmatch(branch) is not None


def valid_pr_route(head: str, base: str) -> bool:
    if base == "main":
        return head == "develop" or (
            valid_work_branch(head) and head.split("/")[1] in {"release", "hotfix"}
        )
    if base == "develop":
        return head == "main" or valid_work_branch(head) or BOT_BRANCH.fullmatch(head) is not None
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    branch = commands.add_parser("branch")
    branch.add_argument("--name")
    commit = commands.add_parser("commit")
    commit.add_argument("message_file", type=Path)
    pr = commands.add_parser("pr")
    pr.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args(argv)
    try:
        if args.command == "branch":
            name = args.name
            if name is None:
                name = subprocess.check_output(
                    ["git", "branch", "--show-current"], text=True, timeout=10
                ).strip()
            valid = valid_work_branch(name)
            message = "Use codex/<feature|fix|chore|docs|refactor|test|ci|release|hotfix>/<slug>."
        elif args.command == "commit":
            lines = args.message_file.read_text(encoding="utf-8").splitlines()
            valid = bool(lines) and valid_subject(lines[0])
            message = "Use a Conventional Commit <= 100 characters, e.g. feat(cli): add status."
        else:
            if args.event is None:
                raise ValueError("Missing event file")
            event = json.loads(args.event.read_text(encoding="utf-8"))
            request = event["pull_request"]
            valid = valid_subject(request["title"]) and valid_pr_route(
                request["head"]["ref"], request["base"]["ref"]
            )
            message = "PR needs a Conventional Commit title and an allowed Git Flow source/target."
    except OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError:
        print("Git policy input could not be validated.")
        return 2
    if not valid:
        print(message)
        return 1
    print("Git policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
