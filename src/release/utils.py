import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from rich import get_console

EDITOR = os.environ.get("EDITOR", "nano")
WORK_DIR = Path("work")
FC_NIXOS = WORK_DIR / "fc-nixos"
FC_DOCS = WORK_DIR / "doc"
TEMP_CHANGELOG = WORK_DIR / "temp_changelog.md"


def prompt(
    prompt: str,
    *,
    default: Optional[Any] = None,
    default_display: Optional[str] = None,
    str_default: Optional[str] = None,
    conv: Callable = str,
):
    if str_default is None and default is not None:
        str_default = str(default)
    if default_display is None:
        default_display = str_default
    if default_display is not None:
        prompt += f" ([prompt.default]{default_display}[/prompt.default])"
    prompt += ": "
    while True:
        i = get_console().input(prompt)
        try:
            if not i:
                if default is not None:
                    return default
                elif str_default is not None:
                    return conv(str_default)
                else:
                    continue
            return conv(i)
        except (ValueError, argparse.ArgumentTypeError) as e:
            get_console().print(
                str(e) or "Invalid value", style="prompt.invalid"
            )


def git_tty(path: Path, *cmd: str, check=True, **kw):
    return subprocess.run(["git"] + list(cmd), cwd=path, check=check, **kw)


def git(path: Path, *cmd: str, **kw):
    return subprocess.check_output(
        ["git"] + list(cmd), cwd=path, text=True, **kw
    )


def rev_parse(path: Path, rev: str):
    return git(path, "rev-parse", "--verify", rev).strip()


def load_json(path: Path, rev: str, obj_path: str):
    return json.loads(git(path, "show", rev + ":" + obj_path))


def git_remote(path: Path):
    out = git(path, "remote", "-v")
    return re.findall(r"^origin\s(.+?)\s\(.+\)$", out, re.MULTILINE)


def ensure_repo(path: Path, url: str, *fetch_args: str):
    if not path.exists():
        path.mkdir(parents=True)
        git(path, "init")
    if (remotes := set(git_remote(path))) != {url}:
        if remotes:
            try:
                git(path, "remote", "rm", "origin")
            except subprocess.SubprocessError:
                pass
        git(path, "remote", "add", "origin", url)
    git(
        path,
        "fetch",
        "origin",
        "--tags",
        "--prune",
        "--prune-tags",
        "--force",
        *fetch_args,
    )


def checkout(path: Path, branch: str, reset: bool = False, clean: bool = False):
    if reset:
        git(path, "checkout", "-q", "-f", branch)
        git(path, "reset", "-q", "--hard", f"origin/{branch}")
    else:
        git(path, "checkout", "-q", branch)
    if clean:
        git(path, "clean", "-d", "--force")


def machine_prefix(nixos_version: str):
    return "release" + nixos_version.replace(".", "")
