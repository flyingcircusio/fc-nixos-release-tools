import logging
import socket
import subprocess

import requests
from rich import print
from rich.markdown import Markdown
from rich.progress import Progress
from rich.prompt import Confirm, Prompt

from .markdown import MarkdownTree
from .state import STAGE, State
from .utils import (
    FC_NIXOS,
    HydraReleaseBuild,
    checkout,
    ensure_repo,
    git,
    load_json,
    machine_prefix,
    rev_parse,
    run_maintenance_switch_on_vm,
    verify_machines_are_current,
    wait_for_successful_hydra_release_build,
)

STEPS = [
    "prepare",
    "review_pending_commits",
    "check_hydra",
    "check_releasetest_machines",
    "collect_changelog",
    "merge",
    "backmerge",
    "add_detailed_changelog",
    "push",
]

CHANGELOG = FC_NIXOS / "changelog.d" / "CHANGELOG.md"


def generate_nixpkgs_changelog(old_rev: str, new_rev: str) -> MarkdownTree:
    res = MarkdownTree()
    res[
        "Detailed Changes"
    ] += f"- [platform code](https://github.com/flyingcircusio/fc-nixos/compare/{old_rev}...{new_rev})"

    pversions_path = "release/package-versions.json"
    try:
        old_pversions = load_json(FC_NIXOS, old_rev, pversions_path)
        new_pversions = load_json(FC_NIXOS, new_rev, pversions_path)

        lines = []
        for pkg_name in old_pversions:
            old = old_pversions.get(pkg_name, {}).get("version")
            new = new_pversions.get(pkg_name, {}).get("version")

            if not old and new:
                lines.append(f"{pkg_name}: (old version missing)")
            elif old and not new:
                lines.append(f"{pkg_name}: (new version missing)")
            elif old != new:
                lines.append(f"{pkg_name}: {old} -> {new}")

        if lines:
            res["NixOS XX.XX platform"] += (
                "- Pull upstream NixOS changes, security fixes and package updates:"
                + "".join("\n    - " + m for m in lines)
            )
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{pversions_path}'. Continuing without package versions diff..."
        )

    versions_path = "release/versions.json"
    try:
        old_versions = load_json(FC_NIXOS, old_rev, versions_path)
        new_versions = load_json(FC_NIXOS, new_rev, versions_path)
        old_nixpkgs_rev = old_versions["nixpkgs"]["rev"]
        new_nixpkgs_rev = new_versions["nixpkgs"]["rev"]
        if old_nixpkgs_rev != new_nixpkgs_rev:
            res[
                "Detailed Changes"
            ] += f"- [nixpkgs/upstream changes](https://github.com/flyingcircusio/nixpkgs/compare/{old_nixpkgs_rev}...{new_nixpkgs_rev})"
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{versions_path}' file. Continuing without nixpkgs changelog..."
        )

    return res


class Release:
    def __init__(self, state: State, nixos_version: str):
        self.state = state
        self.release_id = state["release_id"]
        self.nixos_version = nixos_version
        self.branch_state = state["branches"].get(nixos_version, {})
        self.branch_state.pop("tested", None)

        self.branch_dev = f"fc-{self.nixos_version}-dev"
        self.branch_stag = f"fc-{self.nixos_version}-staging"
        self.branch_prod = f"fc-{self.nixos_version}-production"

    def prepare(self):
        ensure_repo(FC_NIXOS, "git@github.com:flyingcircusio/fc-nixos.git")

        checkout(FC_NIXOS, self.branch_dev, reset=True, clean=True)
        checkout(FC_NIXOS, self.branch_stag, reset=True, clean=True)
        checkout(FC_NIXOS, self.branch_prod, reset=True, clean=True)

        self.branch_state["orig_staging_commit"] = rev_parse(
            FC_NIXOS, self.branch_stag
        )

    def has_pending_changes(self):
        try:
            git(
                FC_NIXOS,
                "merge-base",
                "--is-ancestor",
                self.branch_stag,
                self.branch_prod,
            )
        except subprocess.CalledProcessError as e:
            if e.returncode != 1:
                raise
            return True
        return False

    def register(self):
        self.state["branches"][self.nixos_version] = self.branch_state

    def review_pending_commits(self):
        print()
        print("[bold white]Review commits[/bold white]")
        print()
        print(git(FC_NIXOS, "cherry", self.branch_prod, self.branch_stag, "-v"))
        print()

        while not Confirm.ask(
            "Have you spot-checked the commits for general sanity?"
        ):
            pass

    def check_hydra(self):
        print()
        print(
            f"[bold white]Verifying clean Hydra build for [green]{self.branch_stag}[/green][/bold white]"
        )
        print()
        orig_stag_rev = self.branch_state.get("orig_staging_commit")
        self.build = wait_for_successful_hydra_release_build(
            self.branch_stag, orig_stag_rev
        )
        print("Hydra has a [green]successful[/green] build.")

    def check_releasetest_machines(self):
        prefix = machine_prefix(self.nixos_version)
        verify_machines_are_current(f"{prefix}stag", self.build.nix_name)

    def check_sensu(self):
        prefix = machine_prefix(self.nixos_version)
        print(
            f"Staging: releasetest sensu checks green? Look at https://sensu.rzob.gocept.net/#/clients?q={prefix} [Enter to confirm]"
        )
        while not Confirm.ask("Is sensu green?"):
            pass

    def collect_changelog(self):
        checkout(FC_NIXOS, self.branch_stag)
        if not CHANGELOG.parent.exists():
            logging.warning(
                f"Could not find '{str(CHANGELOG.parent)}'. Skipping changelog generation..."
            )
            return

        new_fragment = MarkdownTree.collect(
            filter(CHANGELOG.__ne__, CHANGELOG.parent.rglob("*.md")), FC_NIXOS
        )

        old_changelog = MarkdownTree.from_str(
            self.branch_state.get("changelog", "")
        )
        old_changelog["Detailed Changes"] = ""
        self.branch_state["changelog"] = (old_changelog | new_fragment).to_str()

        new_fragment.strip()
        new_fragment.add_header(f"Release {self.release_id}")
        new_changelog = new_fragment.to_str()
        if CHANGELOG.exists():
            new_changelog += "\n" + CHANGELOG.read_text()
        CHANGELOG.write_text(new_changelog)

        try:
            git(FC_NIXOS, "add", str(CHANGELOG.relative_to(FC_NIXOS)))
            git(FC_NIXOS, "commit", "-m", "Collect changelog fragments")
        except subprocess.CalledProcessError:
            logging.error(
                "Failed to commit Changelog. Commit it manually and continue after the `collect_changelog` stage"
            )
            raise

    def merge(self):
        checkout(FC_NIXOS, self.branch_prod)
        msg = (
            f"Merge branch '{self.branch_stag}' into "
            f"'{self.branch_prod}' for release {self.release_id}"
        )
        git(FC_NIXOS, "merge", "-m", msg, self.branch_stag)
        self.branch_state["new_production_commit"] = rev_parse(
            FC_NIXOS, self.branch_prod
        )

    def backmerge(self):
        checkout(FC_NIXOS, self.branch_dev)
        msg = f"Backmerge branch '{self.branch_prod}' into '{self.branch_dev}'' for release {self.release_id}"
        git(FC_NIXOS, "merge", "-m", msg, self.branch_prod)

    def add_detailed_changelog(self):
        old_rev = rev_parse(FC_NIXOS, "origin/" + self.branch_prod)
        new_rev = rev_parse(FC_NIXOS, self.branch_prod)

        new_fragment = MarkdownTree.from_str(
            self.branch_state.get("changelog", "")
        )
        new_fragment |= generate_nixpkgs_changelog(old_rev, new_rev)

        print(Markdown(new_fragment.to_str()))
        print()

        while (
            Prompt.ask(
                "Do you want to [green]edit[/green] the fragment or [green]continue[/green]?",
                choices=["edit", "continue"],
            )
            == "edit"
        ):
            new_fragment.open_in_editor()
            print(Markdown(new_fragment.to_str()))
            print()

        self.branch_state["changelog"] = new_fragment.to_str()

    def push(self):
        remote = git(FC_NIXOS, "remote", "get-url", "--push", "origin")
        print(f"[bold white]Pushing changes to [green]{remote}[/green] ...")

        git(
            FC_NIXOS,
            "push",
            "origin",
            self.branch_dev,
            self.branch_stag,
            self.branch_prod,
        )


def merge_production(state: State, nixos_version: str, steps: list[str]):
    if state["branches"][nixos_version].get("new_production_commit"):
        print("[red]This version has already been merged.[/red]")
        if not Confirm.ask(
            "Do you want to merge this branch again? "
            "[red](This will reset the stage back to 'merge' and may result in duplicate changelog entries)[/red]"
        ):
            return
        state["stage"] = STAGE.MERGE
    release = Release(state, nixos_version)
    for step_name in steps:
        getattr(release, step_name)()


def release_production(state: State, nixos_version: str):
    if nixos_version not in state["branches"]:
        logging.error(f"Please add '{nixos_version}' before testing it")
        return
    branch_state = state["branches"][nixos_version]
    if "tested" in branch_state:
        logging.error(f"'{nixos_version}' already tested")
        return

    changelog = MarkdownTree.from_str(branch_state.get("changelog", ""))
    prod_commit = branch_state["new_production_commit"]
    build = wait_for_successful_hydra_release_build(
        f"fc-{nixos_version}-production", prod_commit
    )
    branch_state["hydra_eval_id"] = build.eval_id
    print(
        f"Production: directory: create release '{state['release_id']}' for {nixos_version}-production using hydra eval ID {build.eval_id}, valid from {state['release_date']} 21:00"
    )
    print(
        "(releasetest VMs will already use this as the *next* release) [Enter to confirm]"
    )
    input()

    metadata_url = f"https://my.flyingcircus.io/releases/metadata/fc-{nixos_version}-production/{state['release_id']}"
    changelog["Detailed Changes"] += f"- [metadata]({metadata_url})"
    try:
        r = requests.get(metadata_url, timeout=5)
        r.raise_for_status()
        channel_url = r.json()["channel_url"]
        changelog["Detailed Changes"] += f"- [channel url]({channel_url})"
        logging.info("Added channel url fragment")
    except (requests.RequestException, KeyError):
        logging.warning(
            "Failed to retrieve channel url. Please add it manually in the next step"
        )

    prefix = machine_prefix(nixos_version)
    verify_machines_are_current(f"{prefix}prod", build.nix_name)

    print(
        "Check maintenance log, check switch output for unexpected service restarts, compare with changelog, impact properly documented? [Enter to edit]"
    )
    while not Confirm.ask("Ready to continue?"):
        pass

    changelog.open_in_editor()
    branch_state["changelog"] = changelog.to_str()

    branch_state["tested"] = True


def tag_branch(state: State):
    ensure_repo(FC_NIXOS, "git@github.com:flyingcircusio/fc-nixos.git")
    print(
        "activate 'keep' for the Hydra job flyingcircus:fc-*-production:release [Enter]"
    )
    input()
    for nixos_version in state["branches"].keys():
        git(
            FC_NIXOS,
            "tag",
            f"fc/r{state['release_id']}/{nixos_version}",
            f"fc-{nixos_version}-production",
        )

    git(FC_NIXOS, "push", "--tags")
    state["stage"] = STAGE.DONE
