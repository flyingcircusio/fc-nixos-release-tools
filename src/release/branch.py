"""Manage the release workflow for a single branch."""

import logging
import subprocess

import requests
from rich import print
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

from .git import FC_NIXOS
from .markdown import MarkdownTree
from .release import Release
from .utils import (
    machine_prefix,
    trigger_rolling_release_update,
    verify_machines_are_current,
    wait_for_successful_hydra_release_build,
)

CHANGELOG = FC_NIXOS.path / "changelog.d" / "CHANGELOG.md"


def generate_nixpkgs_changelog(old_rev: str, new_rev: str) -> MarkdownTree:
    res = MarkdownTree()
    res["Detailed Changes"] += (
        f"- [platform code](https://github.com/flyingcircusio/fc-nixos/"
        f"compare/{old_rev}...{new_rev})"
    )

    pversions_path = "release/package-versions.json"
    try:
        old_pversions = FC_NIXOS.show(old_rev, pversions_path)
        new_pversions = FC_NIXOS.show(new_rev, pversions_path)

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
                "- Pull upstream NixOS changes, security fixes, and "
                "package updates:" + "".join("\n    - " + m for m in lines)
            )
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{pversions_path}'. Continuing without package "
            f"versions diff..."
        )

    versions_path = "release/versions.json"
    try:
        old_versions = FC_NIXOS.show(old_rev, versions_path)
        new_versions = FC_NIXOS.show(new_rev, versions_path)
        old_nixpkgs_rev = old_versions["nixpkgs"]["rev"]
        new_nixpkgs_rev = new_versions["nixpkgs"]["rev"]
        if old_nixpkgs_rev != new_nixpkgs_rev:
            res["Detailed Changes"] += (
                f"- [nixpkgs/upstream changes](https://github.com/"
                f"flyingcircusio/nixpkgs/compare/"
                f"{old_nixpkgs_rev}...{new_nixpkgs_rev})"
            )
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{versions_path}' file. Continuing without "
            f"nixpkgs changelog..."
        )

    return res


class Branch:
    """A release branch, like 21.05, 24.05, 24.11, ...

    This covers multiple git branches, like fc-21.05-dev, fc-21.05-staging,
    etc...

    """

    def __init__(self, release: Release, nixos_version: str):
        self.release = release
        self.release_id = release["id"]
        self.nixos_version = nixos_version
        self.state = release["branches"].get(nixos_version, {})

        self.branch_dev = f"fc-{self.nixos_version}-dev"
        self.branch_stag = f"fc-{self.nixos_version}-staging"
        self.branch_prod = f"fc-{self.nixos_version}-production"

    def apply(self):
        if self.state.get("new_production_commit"):
            print("[red]This version has already been merged.[/red]")
            if not Confirm.ask(
                "Do you want to merge this branch again? "
                "[red](This will reset the stage back to 'merge' and may result in duplicate changelog entries)[/red]"
            ):
                return
        for step_name in [x.startswith("step_") for x in dir(self)]:
            print()
            getattr(self, step_name)()

    def has_pending_changes(self):
        return FC_NIXOS.is_ancestor(self.branch_stag, self.branch_prod)

    def register(self):
        self.release["branches"][self.nixos_version] = self.state

    def step_prepare(self):
        FC_NIXOS.ensure_repo()

        FC_NIXOS.checkout(self.branch_dev, reset=True, clean=True)
        FC_NIXOS.checkout(self.branch_stag, reset=True, clean=True)
        FC_NIXOS.checkout(self.branch_prod, reset=True, clean=True)

        self.state["orig_staging_commit"] = FC_NIXOS.rev_parse(self.branch_stag)

    def step_review_pending_commits(self):
        print("[bold white]Review commits[/bold white]")
        print()
        print(FC_NIXOS._git("cherry", self.branch_prod, self.branch_stag, "-v"))
        print()

        while not Confirm.ask(
            "Have you spot-checked the commits for general sanity?"
        ):
            pass

    def step_check_hydra(self):
        print(
            f"[bold white]Verifying clean Hydra build for [green]{self.branch_stag}[/green][/bold white]"
        )
        print()
        orig_stag_rev = self.state.get("orig_staging_commit")
        self.staging_build = wait_for_successful_hydra_release_build(
            self.branch_stag, orig_stag_rev
        )
        print("Hydra has a [green]successful[/green] build.")

    def step_check_releasetest_machines(self):
        prefix = machine_prefix(self.nixos_version)
        verify_machines_are_current(
            f"{prefix}stag", self.staging_build.nix_name
        )

    def step_collect_changelog(self):
        FC_NIXOS.checkout(self.branch_stag)
        if not CHANGELOG.parent.exists():
            logging.warning(
                f"Could not find '{str(CHANGELOG.parent)}'. Skipping changelog generation..."
            )
            return

        new_fragment = MarkdownTree.collect(
            filter(CHANGELOG.__ne__, CHANGELOG.parent.rglob("*.md")), FC_NIXOS
        )

        old_changelog = MarkdownTree.from_str(self.state.get("changelog", ""))
        old_changelog["Detailed Changes"] = ""
        self.state["changelog"] = (old_changelog | new_fragment).to_str()

        new_fragment.strip()
        new_fragment.add_header(f"Release {self.release_id}")
        new_changelog = new_fragment.to_str()
        if CHANGELOG.exists():
            new_changelog += "\n" + CHANGELOG.read_text()
        CHANGELOG.write_text(new_changelog)

        try:
            FC_NIXOS._git("add", str(CHANGELOG.relative_to(FC_NIXOS)))
            FC_NIXOS._git("commit", "-m", "Collect changelog fragments")
        except subprocess.CalledProcessError:
            logging.error(
                "Failed to commit Changelog. Commit it manually and continue after the `collect_changelog` stage"
            )
            raise

    def step_check_sensu(self):
        prefix = machine_prefix(self.nixos_version)
        print(
            f"Staging: releasetest sensu checks green? Look at https://sensu.rzob.gocept.net/#/clients?q={prefix} [Enter to confirm]"
        )
        while not Confirm.ask("Is sensu green?"):
            pass

    def step_merge(self):
        FC_NIXOS.checkout(self.branch_prod)
        msg = (
            f"Merge branch '{self.branch_stag}' into "
            f"'{self.branch_prod}' for release {self.release_id}"
        )
        FC_NIXOS._git("merge", "-m", msg, self.branch_stag)
        self.state["new_production_commit"] = FC_NIXOS.rev_parse(
            FC_NIXOS, self.branch_prod
        )

    def step_backmerge(self):
        FC_NIXOS.checkout(self.branch_dev)
        msg = f"Backmerge branch '{self.branch_prod}' into '{self.branch_dev}'' for release {self.release_id}"
        FC_NIXOS._git("merge", "-m", msg, self.branch_prod)

    def step_add_nixpkgs_changelog(self):
        old_rev = FC_NIXOS.rev_parse("origin/" + self.branch_prod)
        new_rev = FC_NIXOS.rev_parse(self.branch_prod)

        new_fragment = MarkdownTree.from_str(self.release.get("changelog", ""))
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

        self.state["changelog"] = new_fragment.to_str()

    def step_push(self):
        remote = FC_NIXOS._git("remote", "get-url", "--push", "origin")
        print(f"[bold white]Pushing changes to [green]{remote}[/green] ...")

        FC_NIXOS._git(
            "push",
            "origin",
            self.branch_dev,
            self.branch_stag,
            self.branch_prod,
        )

    # Hydra now starts building the production branch

    def step_check_hydra_production(self):
        print(
            f"[bold white]Verifying clean Hydra production build for [green]{self.branch_prod}[/green][/bold white]"
        )
        print()
        prod_rev = self.state.get("new_production_commit")
        self.production_build = wait_for_successful_hydra_release_build(
            self.branch_prod, prod_rev
        )
        self.state["hydra_eval_id"] = self.production_build.eval_id
        print(
            f"Hydra has a [green]successful[/green] build: [cyan]{self.production_build.eval_id}[/cyan]"
        )

    def step_create_directory_release(self):
        print("Create directory release for {self.branch_prod}")
        print()
        print(" > https://directory.fcio.net/environments")

        trigger_rolling_release_update()

        print()
        print("Release name: [cyan]{self.release['id']}[/cyan]")
        print("Hydra eval: [cyan]{self.state['hydra_eval_id']}[/cyan]")
        print("Valid from (UTC): [cyan]{self.release['date']} 7:00 PM[/cyan]")
        print()

        while not Confirm.ask("Did you add the release?"):
            pass

    def step_update_central_changelog(self):
        metadata_url = f"https://my.flyingcircus.io/releases/metadata/fc-{self.nixos_version}-production/{self.release['id']}"
        changelog = MarkdownTree.from_str(self.release.get("changelog", ""))
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

    def step_verify_production_machines(self):
        prefix = machine_prefix(self.nixos_version)
        verify_machines_are_current(
            f"{prefix}prod", self.production_build.nix_name
        )
        print(
            "Check maintenance log, check switch output for unexpected service restarts, compare with changelog, impact properly documented? [Enter to edit]"
        )
        while not Confirm.ask("Ready to continue?"):
            pass

    changelog.open_in_editor()
    state["changelog"] = changelog.to_str()

    state["tested"] = True
