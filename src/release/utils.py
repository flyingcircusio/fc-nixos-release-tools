import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from rich import get_console, print
from rich.progress import Progress

EDITOR = os.environ.get("EDITOR", "nano")
WORK_DIR = Path("work")
FC_NIXOS = WORK_DIR / "fc-nixos"
FC_DOCS = WORK_DIR / "doc"
TEMP_CHANGELOG = WORK_DIR / "temp_changelog.md"
HYDRA_URL = "https://hydra.flyingcircus.io"
HYDRA_EVALS_URL = f"{HYDRA_URL}/jobset/flyingcircus/{{branch}}/evals"
HYDRA_RELEASE_BUILD_URL = f"{HYDRA_URL}/eval/{{eval_id}}/job/release"


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


def iter_hydra(url: str, item_key: str):
    page = ""
    while True:
        r = requests.get(url + page, headers={"Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
        for item in j[item_key]:
            yield item
        if "last" not in j or page == j["last"]:
            break
        page = j["next"]


def get_hydra_eval_id_for_commit(branch: str, commit_hash: str):
    try:
        evals = iter_hydra(HYDRA_EVALS_URL.format(branch=branch), "evals")
        for eval in evals:
            if eval["jobsetevalinputs"]["fc"]["revision"] == commit_hash:
                return eval["id"]
    except Exception as e:
        print("[red]Error fetching hydra evals", e)
        raise RuntimeError("Error fetching hydra evals")


def get_hydra_release_build(eval_id: str):
    """
    Gets the status of the `release` build of the eval with the given branch and commit
    :return: None, when build is still running. Otherwise, status int
    """
    hydra_build_url = HYDRA_RELEASE_BUILD_URL.format(eval_id=eval_id)
    r = requests.get(hydra_build_url, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


@dataclass
class HydraReleaseBuild:
    nix_name: str
    eval_id: str


def wait_for_successful_hydra_release_build(
    branch: str, commit_hash: str
) -> HydraReleaseBuild:
    with Progress(transient=True) as progress:
        task = progress.add_task(
            f"[yellow]Waiting for hydra eval in {branch} with commit {commit_hash} to be created...",
            total=None,
        )
        print("Check hydra evals: " + HYDRA_EVALS_URL.format(branch=branch))

        while not progress.finished:
            eval_id = get_hydra_eval_id_for_commit(branch, commit_hash)
            if eval_id is None:
                time.sleep(10)
                continue
            print(f"Staging new eval id: {eval_id}")
            progress.update(task, total=1, advance=1)

    with Progress(transient=True) as progress:
        task = progress.add_task(
            "[yellow]Waiting for hydra release build to finish...", total=None
        )
        print(
            "Check hydra release build for status: "
            + HYDRA_RELEASE_BUILD_URL.format(eval_id=eval_id)
        )
        while not progress.finished:
            build = get_hydra_release_build(eval_id)
            if build["finished"] != 1:
                time.sleep(10)
                continue

            if build["buildstatus"] == 0:
                progress.update(task, total=1, advance=1)
                nix_name = build["nixname"].split("release-")[1]
                return HydraReleaseBuild(nix_name, eval_id)

            print(
                f"[red]Hydra release build for {branch} and commit {commit_hash} is unsuccessful"
            )
            raise RuntimeError("Hydra release build is unsuccessful")


def run_maintenance_switch_on_vm(vm_name: str):
    with Progress(transient=True) as progress:
        task = progress.add_task("", total=None)
        cmds = [
            "sudo fc-manage update-enc",
            "sudo systemctl start fc-update-channel.service",
            "sudo fc-maintenance run --run-all-now",
        ]
        progress.update(task, total=len(cmds))
        for cmd in cmds:
            progress.update(
                task,
                description=f"{vm_name}: {cmd}",
            )
            try:
                subprocess.run(
                    ["ssh", vm_name] + cmd.split(" "),
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                print(
                    f"Staging: [red]Switching {vm_name} failed with code {e.returncode}!",
                )
                print("STDOUT:", e.stdout.decode("utf-8"))
                print("STDERR:", e.stderr.decode("utf-8"))
                return
            progress.update(task, advance=1)
