import datetime
import json
import logging
import tempfile
import zipfile
from io import BytesIO
from os import path
from pathlib import Path

import requests
from git import Repo
from github import Auth, Github

from auto_merge import utils
from auto_merge.config import Config
from utils.matrix import MatrixHookshot


def merge_prs(config: Config, gh: Github, github_access_token: str):
    repo_name = config.general.fc_nixos_repo_name
    repo = gh.get_repo(repo_name)
    today = datetime.date.today()
    for pr in repo.get_pulls(state="open"):
        mergeable = utils.check_pr_mergeable(
            repo, pr, github_access_token, config
        )
        if not mergeable:
            logging.debug(
                f"PR {pr.number} does not fulfill the merge criteria."
            )
            continue
        risk, urgency = utils.get_label_values_for_pr(pr.labels)
        merge_date = utils.calculate_merge_date(risk, urgency, config)
        if merge_date == today:
            logging.info(f"Merging PR {pr.number}.")
            pr.merge(delete_branch=True)


def fc_nixos_repository(directory: str, url: str) -> Repo:
    logging.info("Updating fc-nixos repository.")
    if path.exists(directory):
        repo = Repo(directory)
    else:
        repo = Repo.init(directory, mkdir=True)
        repo.create_remote("origin", url)

    repo.remotes["origin"].fetch()
    return repo


def merge_staging(fc_nixos_repo: Repo, config: Config):
    for platform_version in config.general.platform_versions:
        fc_nixos_repo.git.checkout(f"fc-{platform_version}-staging")
        fc_nixos_repo.git.merge(
            f"origin/fc-{platform_version}-dev", no_edit=True
        )
        fc_nixos_repo.git.push("origin", f"fc-{platform_version}-staging")


def check_monitoring_review_status(
    config: Config, monitoring_review_url: str, matrix_hookshot: MatrixHookshot
) -> bool:
    for column in config.monitoring_review.names:
        req = requests.get(monitoring_review_url + f"/{column}")
        req.raise_for_status()
        monitoring_review = req.json()
        if (
            datetime.datetime.fromisoformat(
                monitoring_review["last_review"]
            ).date()
            < datetime.date.today()
        ):
            logging.warning(
                "Platform monitoring review is not done yet or has release blocker. Not merging!"
            )
            if (
                utils.now_tz().hour
                == config.monitoring_review.notification_cutoff_hour
            ):
                matrix_hookshot.send_notification(
                    "fc-nixos auto-merge: Platform monitoring review is not done yet today. Not merging staging nor any PRs."
                )
                return False

        if monitoring_review["has_platform_release_blocker"]:
            logging.warning(
                "Platform monitoring review is not done yet or has release blocker. Not merging!"
            )
            return False
    return True


def run(
    fc_nixos_dir: str,
    action_run_repo_name: str,
    config: Config,
    github_access_token: str,
    monitoring_review_url: str,
    matrix_hookshot_url: str,
):
    matrix_hookshot = MatrixHookshot(matrix_hookshot_url)
    gh = Github(auth=Auth.Token(github_access_token))
    action_run_repo = gh.get_repo(action_run_repo_name)
    try:
        runs = action_run_repo.get_workflow("auto-merge.yaml").get_runs(
            status="completed"
        )
        download_url = ""
        for artifact in runs[0].get_artifacts():
            if artifact.name == "status-json":
                download_url = artifact.archive_download_url
                break
        with tempfile.TemporaryDirectory() as tmpdir:
            r = requests.get(
                download_url,
                headers={"Authorization": f"Bearer {github_access_token}"},
            )
            r.raise_for_status()
            z = zipfile.ZipFile(BytesIO(r.content))
            z.extractall(tmpdir)
            status = json.load((Path(tmpdir) / "auto-merge-status.json").open())
            logging.info(
                f"Found auto-merge-status.json with contents: {json.dumps(status)}"
            )
    except Exception as e:
        logging.debug(
            "Error happened while fetching auto-merge-status.json: ", exc_info=e
        )
        status = {}
        logging.info("Didn't found auto-merge-status.json in GitHub")

    fc_nixos_url = f"https://x-access-token:{github_access_token}@github.com/{config.general.fc_nixos_repo_name}"
    last_staging_merge = status.get("last_staging_merge")
    if (
        last_staging_merge is None
        or datetime.datetime.fromisoformat(last_staging_merge).date()
        < datetime.date.today()
    ):
        if not check_monitoring_review_status(
            config, monitoring_review_url, matrix_hookshot
        ):
            return
        if (
            datetime.date.today().weekday()
            == config.general.production_merge_day
        ):
            logging.info(
                "Today is production merge. Not merging dev into staging."
            )
        else:
            fc_nixos_repo = fc_nixos_repository(fc_nixos_dir, fc_nixos_url)
            merge_staging(fc_nixos_repo, config)
        status["last_staging_merge"] = datetime.datetime.now().isoformat()

    # Write auto-merge-status.json that gets uploaded by GHA
    (Path.cwd() / "auto-merge-status.json").write_text(json.dumps(status))

    merge_prs(config, gh, github_access_token)
