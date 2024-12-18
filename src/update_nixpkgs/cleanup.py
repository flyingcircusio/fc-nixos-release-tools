"""
This script should be run when an automatic update-nixpkgs PR has been merged.
It will merge the corresponding flyingcircus/nixpkgs PR and cleanup
all old fc-nixos and nixpkgs PRs/branches that haven't been merged.
"""

import datetime
import json
import logging
import os
from dataclasses import dataclass
from logging import info, warning
from pathlib import Path

from git import GitCommandError, Repo
from github import Auth, Github
from github.PullRequest import PullRequest

from update_nixpkgs import FC_NIXOS_REPO, NIXPKGS_REPO
from utils.matrix import MatrixHookshot


@dataclass
class Remote:
    url: str
    branches: list[str]


def nixpkgs_repository(directory: str, remotes: dict[str, Remote]) -> Repo:
    info("Updating nixpkgs repository.")
    if os.path.exists(directory):
        repo = Repo(directory)
    else:
        repo = Repo.init(directory, mkdir=True)

    for name, remote in remotes.items():
        info(f"Updating nixpkgs repository remote `{name}`.")
        if name in repo.remotes and repo.remotes[name].url != remote.url:
            repo.delete_remote(repo.remote(name))
        if name not in repo.remotes:
            repo.create_remote(name, remote.url)

        for branch in remote.branches:
            info(
                f"Fetching nixpkgs repository remote `{name}` - branch `{branch}`."
            )
            getattr(repo.remotes, name).fetch(
                refspec=branch, filter="blob:none"
            )

    return repo


def check_nixpkgs_up_to_date(
    nixpkgs_repo: Repo,
    fc_nixos_dir: str,
    fc_nixos_target_branch: str,
    nixpkgs_target_branch: str,
    integration_branch: str,
    fc_nixos_pr: PullRequest,
    matrix_hookshot: MatrixHookshot,
):
    fc_nixos_repo = Repo(fc_nixos_dir)
    versions_json_path = Path(fc_nixos_dir) / "release" / "versions.json"

    # HEAD = the head of the merged PR (before merge)
    # XXX: This makes an assumption that the PR only contains 1 commit. This should be cleaned up.
    merge_base = fc_nixos_repo.git.rev_parse("HEAD^")
    # The integration branch is directly branched of the target branch, so we can only have one merge base.
    fc_nixos_repo.git.switch(merge_base, detach=True)

    with open(versions_json_path) as f:
        versions = json.load(f)
    previous_versions_rev = versions["nixpkgs"]["rev"]

    current_fc_nixos_commit = nixpkgs_repo.refs[
        f"origin/{nixpkgs_target_branch}"
    ].commit
    result = current_fc_nixos_commit.hexsha == previous_versions_rev
    if not result:
        notification = f"""ERROR Unable to promote nixpkgs daily integration branch `{integration_branch}` to `{nixpkgs_target_branch}`.
Integration branch not up to date. Expected commit `{previous_versions_rev}` as HEAD commit of `flyingcircusio/nixpkgs/{nixpkgs_target_branch}`, but got `{current_fc_nixos_commit.hexsha}`.

Please resolve manually by looking at the changes in nixpkgs between these commits, and then run [the GitHub Action](https://github.com/flyingcircusio/fc-nixos-release-tools/actions/workflows/update-nixpkgs.yaml) manually."""
        fc_nixos_pr.create_issue_comment(notification)
        matrix_hookshot.send_notification(
            f"update-nixpkgs PR [#{fc_nixos_pr.number}](https://github.com/flyingcircusio/fc-nixos/pull/{fc_nixos_pr.number}): {notification}"
        )
        logging.info(
            f"Expected commit `{previous_versions_rev}` as HEAD commit of `flyingcircusio/nixpkgs/{nixpkgs_target_branch}`, but got `{current_fc_nixos_commit.hexsha}`"
        )
    return result


def promote_nixpkgs(
    gh: Github, nixpkgs_repo: Repo, target_branch: str, integration_branch: str
) -> bool:
    """Promote nixpkgs repo target branch (e.g. nixos-24.05) to the integration branch
    via a hard reset.
    First, check that the previous versions.json in fc-nixos is equivalent to the
    Returns: True when successful, False when unsuccessful.
    """
    info("Hard reset nixpkgs target branch to integration branch.")
    if nixpkgs_repo.is_dirty():
        raise Exception("Repository is dirty!")

    nixpkgs_repo.git.checkout(target_branch)

    nixpkgs_repo.git.reset(f"origin/{integration_branch}", hard=True)

    nixpkgs_repo.git.push(force_with_lease=True)
    # Tag result so that the commit is always referenced so that other release tooling can find it.
    nixpkgs_repo.git.tag(integration_branch, message=integration_branch)
    nixpkgs_repo.git.push("origin", tags=True)
    gh.get_repo(NIXPKGS_REPO).get_git_ref(
        f"heads/{integration_branch}"
    ).delete()
    return True


def cleanup_old_prs_and_branches(
    gh: Github, merged_integration_branch: str, platform_branch: str
):
    info("Cleaning up old PRs and branches.")
    fc_nixos_repo = gh.get_repo(FC_NIXOS_REPO)
    nixpkgs_repo = gh.get_repo(NIXPKGS_REPO)
    merged_integration_branch_date = datetime.date.fromisoformat(
        merged_integration_branch.split("/")[2]
    )
    # branches will be closed automatically by GitHub, when the branch is deleted
    for repo in [fc_nixos_repo, nixpkgs_repo]:
        for branch in repo.get_branches():
            if not branch.name.startswith(
                f"nixpkgs-auto-update/{platform_branch}/"
            ):
                continue
            branch_datestr = branch.name.split("/")[2]
            if (
                datetime.date.fromisoformat(branch_datestr)
                < merged_integration_branch_date
            ):
                repo.get_git_ref(f"heads/{branch.name}").delete()


def run(
    merged_pr_id: str,
    nixpkgs_origin_url: str,
    nixpkgs_dir: str,
    fc_nixos_dir: str,
    github_access_token: str,
    matrix_hookshot_url: str,
):
    gh = Github(auth=Auth.Token(github_access_token))
    fc_nixos_pr = gh.get_repo(FC_NIXOS_REPO).get_pull(int(merged_pr_id))
    pr_platform_version = fc_nixos_pr.base.ref.split("-")[1]
    integration_branch = fc_nixos_pr.head.ref
    nixpkgs_target_branch = f"nixos-{pr_platform_version}"
    matrix_hookshot = MatrixHookshot(matrix_hookshot_url)

    remotes = {
        "origin": Remote(
            nixpkgs_origin_url,
            [integration_branch, nixpkgs_target_branch],
        )
    }

    nixpkgs_repo = nixpkgs_repository(nixpkgs_dir, remotes)
    if not check_nixpkgs_up_to_date(
        nixpkgs_repo,
        fc_nixos_dir,
        fc_nixos_pr.base.ref,
        nixpkgs_target_branch,
        integration_branch,
        fc_nixos_pr,
        matrix_hookshot,
    ):
        logging.error(
            "Abort promotion of nixpkgs branch. PR is not up to date."
        )
        return
    if promote_nixpkgs(
        gh, nixpkgs_repo, nixpkgs_target_branch, integration_branch
    ):
        fc_nixos_pr.create_issue_comment(
            f"Promoted this nixpkgs integration branch to the `{nixpkgs_target_branch}` branch successfully."
        )
        cleanup_old_prs_and_branches(
            gh, integration_branch, fc_nixos_pr.base.ref
        )
