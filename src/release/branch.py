import logging
import subprocess

import requests
from rich import print
from rich.prompt import Confirm

from .markdown import MarkdownTree
from .state import STAGE, State
from .utils import (
    FC_NIXOS,
    checkout,
    ensure_repo,
    git,
    load_json,
    machine_prefix,
    rev_parse,
    wait_for_successful_hydra_release_build,
)

STEPS = [
    "prepare",
    "diff_release",
    "check_hydra",
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

    def diff_release(self):
        def cherry(upstream: str, head: str):
            res = git(FC_NIXOS, "cherry", upstream, head, "-v")
            print(
                f"Commits in {head}, not in {upstream} ({len(res.splitlines())}):"
            )
            print()
            print(f"git cherry '{upstream}' '{head}' -v")
            print(res)
            print()

        dev_rev = rev_parse(FC_NIXOS, self.branch_dev)
        stag_rev = rev_parse(FC_NIXOS, self.branch_stag)
        prod_rev = rev_parse(FC_NIXOS, self.branch_prod)

        print(
            f"Comparing {self.branch_dev} to {self.branch_prod} {prod_rev}..{dev_rev}"
        )
        print(f"{self.branch_stag} is at {stag_rev}")

        print("")
        print("Merged PRs:")
        print()
        print(f"gh pr list --state=merged -B '{self.branch_dev}'")
        try:
            subprocess.run(
                ["gh", "pr", "list", "--state=merged", "-B", self.branch_dev],
                cwd=FC_NIXOS,
            )
        except FileNotFoundError:
            print("'gh' is not available. Please check merged PRs manually")
        print()

        cherry(self.branch_stag, self.branch_dev)
        cherry(self.branch_prod, self.branch_dev)

        print(f"git diff '{self.branch_prod}' '{self.branch_dev}'")
        print("Press Enter to show full diff")

        input()

        git(FC_NIXOS, "diff", self.branch_prod, self.branch_dev)

        print("Press enter to continue")
        input()

    def check_hydra(self):
        orig_stag_rev = self.branch_state.get(
            "orig_staging_commit", "<unknown rev>"
        )
        prefix = machine_prefix(self.nixos_version)
        build = wait_for_successful_hydra_release_build(
            self.branch_stag, orig_stag_rev
        )
        print("Staging: Hydra release build is successful")
        print(f"Staging: Checking that {prefix}stag00 switched successfully")
        remote_nix_name = (
            subprocess.check_output(
                [
                    "ssh",
                    f"{prefix}stag00",
                    "cat",
                    "/run/current-system/nixos-version",
                ]
            )
            .decode("utf-8")
            .strip()
        )
        if remote_nix_name != build.nix_name:
            print(
                f"Staging: {prefix}stag00 has not yet switched to the new release (nixname {build.nix_name}). Switch manually. [Enter to confirm]"
            )
            input()
        else:
            print(f"Staging: {prefix}stag00 switched successfully")
        print(
            f"Staging: releasetest sensu checks green? Look at https://sensu.rzob.gocept.net/#/clients?q={prefix} [Enter to confirm]"
        )
        input()

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

        print("Press enter to edit the generated changelog fragment")
        input()
        new_fragment.open_in_editor()
        self.branch_state["changelog"] = new_fragment.to_str()

    def push(self):
        print(f"Committed changes ({self.nixos_version}):")
        print("fc-nixos:")
        git(FC_NIXOS, "log", "--graph", "--decorate", "--format=short", "-n3")
        print()
        print(
            "If this looks correct, press Enter to push (or use ^C to abort)."
        )
        input()
        git(
            FC_NIXOS,
            "push",
            "origin",
            self.branch_dev,
            self.branch_stag,
            self.branch_prod,
        )


def add_branch(state: State, nixos_version: str, steps: list[str]):
    if nixos_version in state["branches"] or state["stage"] != STAGE.BRANCH:
        logging.warning(
            f"Branch '{nixos_version}' already added or no longer in 'branch' stage"
        )
        if not Confirm.ask(
            "Do you want to (re-)add this branch? "
            "(This will reset the stage back to 'branch' and may result in duplicate changelog entries)"
        ):
            return
        state["stage"] = STAGE.BRANCH
    release = Release(state, nixos_version)
    logging.info(f"Adding {nixos_version} to {state['release_id']}")
    for step_name in steps:
        logging.info(f"Release step: {step_name}")
        getattr(release, step_name)()


def test_branch(state: State, nixos_version: str):
    if nixos_version not in state["branches"]:
        logging.error(f"Please add '{nixos_version}' before testing it")
        return
    branch_state = state["branches"][nixos_version]
    if "tested" in branch_state:
        logging.error(f"'{nixos_version}' already tested")
        return

    changelog = MarkdownTree.from_str(branch_state.get("changelog", ""))
    prod_commit = branch_state.get("new_production_commit", "<unknown rev>")
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
    print(
        f"Production: On {prefix}prod00, switch to new system. Is it working correctly?"
    )
    print(
        "Check switch output for unexpected service restarts, compare with changelog, impact properly documented? [Enter to edit]"
    )
    input()

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
