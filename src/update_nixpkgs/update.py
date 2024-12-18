#!/usr/bin/env python3

"""
Workflow:
* get executed in a given interval (e.g. daily - channel bumps are happening approximately every other day)
* pull all given releases into fc fork (rebase strategy)
    * post error on merge conflict
* if new:
    * push to integration branch (nixpkgs-auto-update/fc-XX.XX-dev/YYYY-MM-DD)
    * update fc-nixos & create PR
On Merge (fc-nixos):
    * merge updated nixpkgs into nixos-XX.XX branch
    * delete old integration branches in nixpkgs and fc-nixos

Manual merges work by pushing manually in the nixpkgs integration branch and running the GHA manually.

"""
import datetime
import logging
import os
import sys
from dataclasses import dataclass
from os import path
from pathlib import Path
from subprocess import check_output

from git import Commit, Repo
from git.exc import GitCommandError
from github import Auth, Github

from update_nixpkgs import FC_NIXOS_REPO, NIXPKGS_REPO
from utils.matrix import MatrixHookshot

NIXOS_VERSION_PATH = "release/nixos-version"
PACKAGE_VERSIONS_PATH = "release/package-versions.json"
VERSIONS_PATH = "release/versions.json"


@dataclass
class NixpkgsRebaseResult:
    upstream_commit: Commit

    # This is the latest commit on the release branch in our fork.
    # If we have multiple consecutive updates, it is not the same as
    # fork_before_rebase since this is the state of the tracking branch before
    # the last rebase. This commit is important to generate the full
    # changelog.
    fork_commit: Commit
    fork_before_rebase: Commit
    fork_after_rebase: Commit


@dataclass
class Remote:
    url: str
    branches: list[str]


def nixpkgs_repository(directory: str, remotes: dict[str, Remote]) -> Repo:
    logging.info("Updating nixpkgs repository.")
    if path.exists(directory):
        repo = Repo(directory)
    else:
        repo = Repo.init(directory, mkdir=True)

    for name, remote in remotes.items():
        logging.info(f"Updating nixpkgs repository remote `{name}`.")
        if name in repo.remotes and repo.remotes[name].url != remote.url:
            repo.delete_remote(repo.remote(name))
        if name not in repo.remotes:
            repo.create_remote(name, remote.url)

        for branch in remote.branches:
            logging.info(
                f"Fetching nixpkgs repository remote `{name}` - branch `{branch}`."
            )
            # Ignore errors. This is intended as the last day integration branch may not exist
            try:
                getattr(repo.remotes, name).fetch(
                    refspec=branch, filter="blob:none"
                )
            except GitCommandError as e:
                logging.debug("Error while fetching branch ", e)
                pass

    return repo


def rebase_nixpkgs(
    nixpkgs_repo: Repo,
    branch_to_rebase: str,
    integration_branch: str,
    last_day_integration_branch: str,
    force: bool,
    matrix_hookshot: MatrixHookshot,
) -> NixpkgsRebaseResult | None:
    logging.info("Trying to rebase nixpkgs repository.")
    if nixpkgs_repo.is_dirty():
        raise Exception("Repository is dirty!")

    if not any(
        f"origin/{integration_branch}" == ref.name for ref in nixpkgs_repo.refs
    ):
        logging.info("Creating new integration branch")
        tracking_branch = nixpkgs_repo.create_head(
            integration_branch, f"origin/{branch_to_rebase}"
        )
        tracking_branch.checkout()
    else:
        logging.info("Checking out existing integration branch")
        nixpkgs_repo.git.checkout(integration_branch)

    latest_upstream = nixpkgs_repo.refs[f"upstream/{branch_to_rebase}"].commit
    common_grounds = nixpkgs_repo.merge_base(
        f"upstream/{branch_to_rebase}", "HEAD"
    )

    if (
        all(
            latest_upstream.hexsha != commit.hexsha for commit in common_grounds
        )
        or force
    ):
        logging.info(
            f"Latest commit of {branch_to_rebase} is '{latest_upstream.hexsha}' which is not part of our fork, rebasing."
        )
        current_state = nixpkgs_repo.head.commit
        try:
            nixpkgs_repo.git.rebase(f"upstream/{branch_to_rebase}")
        except GitCommandError:
            logging.exception("nixpkgs rebase failed")
            matrix_hookshot.send_notification(
                f"""\
update-nixpkgs: ERROR nixpkgs rebase failed for {branch_to_rebase}. Please resolve the conflict manually with the following commands:

```
cd nixpkgs
git fetch upstream
git fetch origin
git checkout -b {integration_branch} origin/{branch_to_rebase}
git rebase upstream/{branch_to_rebase}
git push origin {integration_branch}
```
"""
            )
            sys.exit(1)

        # Check if there are new commits compared to the last day's integration branch.
        if f"origin/{last_day_integration_branch}" in nixpkgs_repo.refs:
            diff_index = nixpkgs_repo.git.diff_index(
                f"origin/{last_day_integration_branch}"
            )

            if diff_index == "":
                logging.info(
                    "No changes compared to the last day's integration branch. Not creating a new PR."
                )
                return None

        nixpkgs_repo.git.push("origin", integration_branch, force=True)

        return NixpkgsRebaseResult(
            upstream_commit=latest_upstream,
            fork_commit=nixpkgs_repo.refs[f"origin/{branch_to_rebase}"].commit,
            fork_before_rebase=current_state,
            fork_after_rebase=nixpkgs_repo.head.commit,
        )

    logging.info("Nothing to do.")


def update_fc_nixos(
    fc_nixos_dir: str,
    target_branch: str,
    integration_branch: str,
    previous_hex_sha: str,
    new_hex_sha: str,
):
    logging.info("Update fc-nixos.")
    original_workdir = Path.cwd()
    os.chdir(original_workdir / fc_nixos_dir)
    repo = Repo(Path.cwd())
    if not any(integration_branch == head.name for head in repo.heads):
        tracking_branch = repo.create_head(
            integration_branch, f"origin/{target_branch}"
        )
        tracking_branch.checkout()
    else:
        repo.git.checkout(integration_branch)

    check_output(
        [
            "nix",
            "flake",
            "lock",
            "--override-input",
            "nixpkgs",
            f"github:{NIXPKGS_REPO}/{new_hex_sha}",
        ]
    )
    check_output(["nix", "run", ".#buildVersionsJson"]).decode("utf-8")
    check_output(["nix", "run", ".#buildPackageVersionsJson"]).decode("utf-8")

    repo.git.add(
        [
            "flake.lock",
            VERSIONS_PATH,
            PACKAGE_VERSIONS_PATH,
        ]
    )
    repo.git.commit(message=f"Auto update nixpkgs to {new_hex_sha}")
    repo.git.push("origin", integration_branch, force=True)
    os.chdir(original_workdir)


def create_fc_nixos_pr(
    platform_version: str,
    target_branch: str,
    integration_branch: str,
    github_access_token: str,
    now: str,
):
    logging.info("Create PR in fc-nixos.")
    gh = Github(auth=Auth.Token(github_access_token))
    fc_nixos_repo = gh.get_repo(FC_NIXOS_REPO)
    # XXX: Currently deactivated, as there is a bug in the GH REST API so that no result is returned
    # If there is an open PR for this integration branch, don't create a new one.
    # if fc_nixos_repo.get_pulls(base=target_branch,head=f"flyingcircusio:{integration_branch}", state="open").totalCount > 0:
    #     return
    fc_nixos_repo.create_pull(
        base=target_branch,
        head=integration_branch,
        title=f"[{platform_version}] Automated nixpkgs update {now}",
        body=f"""\
@flyingcircusio/release-managers

View nixpkgs update branch: [{integration_branch}](https://github.com/{NIXPKGS_REPO}/tree/{integration_branch})

Review Checklist:

- [ ] Hydra is green
- [ ] Package update versions look reasonable

When manual changes are required: Push to the nixpkgs update branch, and run [the GitHub Action](https://github.com/flyingcircusio/fc-nixos-release-tools/actions/workflows/update-nixpkgs.yaml) manually.
""",
    )


def run(
    platform_versions: list[str],
    nixpkgs_upstream_url: str,
    nixpkgs_origin_url: str,
    fc_nixos_dir: str,
    nixpkgs_dir: str,
    force: bool,
    github_access_token: str,
    matrix_hookshot_url: str,
):
    matrix_hookshot = MatrixHookshot(matrix_hookshot_url)
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    for platform_version in platform_versions:
        logging.info(f"Updating platform {platform_version}")
        nixpkgs_target_branch = f"nixos-{platform_version}"
        fc_nixos_target_branch = f"fc-{platform_version}-dev"
        integration_branch = (
            f"nixpkgs-auto-update/{fc_nixos_target_branch}/{today}"
        )
        last_day_integration_branch = (
            f"nixpkgs-auto-update/{fc_nixos_target_branch}/{yesterday}"
        )

        remotes = {
            "upstream": Remote(nixpkgs_upstream_url, [nixpkgs_target_branch]),
            "origin": Remote(
                nixpkgs_origin_url,
                [
                    nixpkgs_target_branch,
                    integration_branch,
                    last_day_integration_branch,
                ],
            ),
        }
        nixpkgs_repo = nixpkgs_repository(nixpkgs_dir, remotes)
        if result := rebase_nixpkgs(
            nixpkgs_repo,
            nixpkgs_target_branch,
            integration_branch,
            last_day_integration_branch,
            force,
            matrix_hookshot,
        ):
            logging.info(
                f"Updated 'nixpkgs' to '{result.fork_after_rebase.hexsha}'"
            )
            update_fc_nixos(
                fc_nixos_dir,
                fc_nixos_target_branch,
                integration_branch,
                result.fork_commit.hexsha,
                result.fork_after_rebase.hexsha,
            )
            create_fc_nixos_pr(
                platform_version,
                fc_nixos_target_branch,
                integration_branch,
                github_access_token,
                today,
            )
