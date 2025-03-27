import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from rich import get_console, print
from rich.progress import Progress

EDITOR = shlex.split(os.environ.get("EDITOR", "nano"))
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
        page = j.get("next")
        if not page:
            break


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
        print(
            "    > You can manually check for Hydra evals here: "
            + HYDRA_EVALS_URL.format(branch=branch)
        )
        print()
        while not progress.finished:
            eval_id = get_hydra_eval_id_for_commit(branch, commit_hash)
            if eval_id is None:
                time.sleep(10)
                continue
            print(f"Found matching Hydra eval ID: {eval_id}")
            progress.update(task, total=1, advance=1)

    print()
    with Progress(transient=True) as progress:
        task = progress.add_task(
            "[yellow]Waiting for hydra release build to finish...", total=None
        )
        print(
            "    > You can manually check Hydra build status here: "
            + HYDRA_RELEASE_BUILD_URL.format(eval_id=eval_id)
        )
        print()
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


def get_remote_nix_name(machine: str):
    return (
        subprocess.check_output(
            [
                "ssh",
                machine,
                "cat",
                "/run/current-system/nixos-version",
            ]
        )
        .decode("utf-8")
        .strip()
    )


def verify_machines_are_current(prefix, nix_name):
    print()
    print("[bold white]Verifying test machines[/bold white]")
    print()
    print(
        f"    > Expecting the system's nix_name to be [bold white]{nix_name}[/bold white]"
    )
    print()

    known_machines = list()
    with Progress(transient=True) as progress:
        task = progress.add_task(
            "Scanning for known release test machines ...", total=100
        )
        for i in range(100):
            machine = f"{prefix}{i:02d}"
            progress.update(task, description=f"Checking {machine}")
            try:
                socket.getaddrinfo(machine, 80)
                known_machines.append(machine)
                print(
                    f"    > Found machine [bold white]{machine}[/bold white]."
                )
            except socket.gaierror:
                pass
            progress.update(task, advance=1)

    if not known_machines:
        raise RuntimeError(
            "Could not find any test machines. Please check your network."
        )

    print()

    for machine in known_machines:
        print(
            f"Validating whether [bold white]{machine}[/bold white] has switched successfully ..."
        )
        remote_nix_name = get_remote_nix_name(machine)
        if remote_nix_name != nix_name:
            print(
                f"{machine} has [red]not yet switched to the new release[/red] (nixname {nix_name})."
            )
            run_maintenance_switch_on_vm(machine)

        remote_nix_name = get_remote_nix_name(machine)
        if remote_nix_name != nix_name:
            print(
                f"[bold white]{machine}[/bold white] switched [red]not successful[/red]."
            )
            raise RuntimeError(f"Switch on {machine} not successful.")

        print(
            f"[bold white]{machine}[/bold white] switched [green]successfully[/green]."
        )
        print()


def wait_for_vm_reboot(machine: str):
    with Progress(transient=True) as progress:
        task = progress.add_task(f"Waiting for reboot of {machine}", total=None)

        while not progress.finished:
            out = subprocess.run(
                ["ping", "-c", "-1", machine], check=True, capture_output=True
            )
            if out.returncode != 0:
                time.sleep(1)
                continue
            # Just a connection test
            out = subprocess.run(
                ["ssh", machine, "echo"], check=True, capture_output=True
            )
            if out.returncode != 0:
                time.sleep(1)
                continue
            progress.update(task, total=1, advance=1)


def run_maintenance_switch_on_vm(machine: str):
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
                description=f"{machine}: {cmd}",
            )
            try:
                subprocess.run(
                    ["ssh", machine] + cmd.split(" "),
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                stdout = e.stdout.decode("utf-8")
                stderr = e.stderr.decode("utf-8")
                if "maintenance-reboot" in stdout:
                    wait_for_vm_reboot(machine)
                    pass
                print(
                    f"Staging: [red]Switching {machine} failed with code {e.returncode}!",
                )
                print("STDOUT:", stdout)
                print("STDERR:", stderr)
                raise
            progress.update(task, advance=1)
