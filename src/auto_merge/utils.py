import datetime
import json
import logging
import os
import re
import subprocess
from zoneinfo import ZoneInfo

from dateutil import rrule
from dateutil.relativedelta import relativedelta
from github import Label
from github.PullRequest import PullRequest
from github.Repository import Repository
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.requests import log as requests_logger

from auto_merge.config import Config
from utils.timeout import TimeOut

requests_logger.setLevel(logging.WARNING)


def now_tz():
    return datetime.datetime.now(tz=ZoneInfo("Europe/Berlin"))


def last_production_merge(config: Config) -> datetime.datetime:
    return next_production_merge(config) - datetime.timedelta(weeks=1)


def next_production_merge(config: Config) -> datetime.datetime:
    now = now_tz()
    # In the week of the production merge, just need to add the difference in days
    if now.weekday() < config.general.production_merge_day:
        day = now + datetime.timedelta(
            days=config.general.production_merge_day - now.weekday()
        )
    else:
        day = now + relativedelta(
            days=+1, weekday=+config.general.production_merge_day
        )
    return day.replace(hour=0, minute=0, second=0)


def now_relative_day(config: Config) -> int:
    now = now_tz()
    last_prod_merge_day = last_production_merge(config).replace(hour=0)

    # Calculate workdays between the last production merge and now
    # last production merge day = 0
    workdays = 0
    for dt in rrule.rrule(
        rrule.DAILY, dtstart=last_prod_merge_day, until=now
    ).xafter(last_prod_merge_day):
        if dt.weekday() != 5 and dt.weekday() != 6:
            workdays += 1

    return workdays


def convert_relative_day_to_date(day: int, config: Config) -> datetime.date:
    # We only want to return days in the future
    # These das are relative to the next production merge
    if now_relative_day(config) <= day:
        dt = rrule.rrule(
            rrule.DAILY,
            byweekday=(0, 1, 2, 3, 4),
            dtstart=last_production_merge(config),
        )[day]
        return dt.date()
    dt = rrule.rrule(
        rrule.DAILY,
        byweekday=(0, 1, 2, 3, 4),
        dtstart=next_production_merge(config),
    )[day]
    return dt.date()


def calculate_merge_date(
    risk: int, urgency: int, config: Config
) -> datetime.date:
    now_relative = now_relative_day(config)
    for day, day_config in sorted(
        config.pr_merge_days.items(),
        key=lambda item: ("0" if now_relative <= item[0] else "1")
        + str(item[0]),
    ):
        if day_config.max_risk >= risk and day_config.min_urgency <= urgency:
            return convert_relative_day_to_date(day, config)


def get_label_values_for_pr(labels: list[Label]) -> (int | None, int | None):
    risk = None
    urgency = None
    for label in labels:
        if label.name.startswith("risk:"):
            risk = int(label.name.split("risk:")[1])
        if label.name.startswith("urgency:"):
            urgency = int(label.name.split("urgency:")[1])
    return risk, urgency


def check_pr_mergeable(
    repo: Repository, pr: PullRequest, token: str, config: Config
) -> bool:
    # Wait until PR is no longer in mergeable state `unknown`
    if pr.mergeable_state == "unknown":
        try:
            timeout = TimeOut(30, interval=1)
            while timeout.tick():
                if pr.mergeable_state == "unknown":
                    pr = repo.get_pull(pr.number)
                else:
                    break
        except TimeoutError:
            logging.debug(
                f"PR {pr.number} is still in merge conflict state unknown after timeout."
            )
            pass

    if not pr.mergeable:
        logging.info(
            f"PR {pr.number} has conflicts (state {pr.mergeable_state}). Not mergeable."
        )
        return False

    if pr.draft:
        logging.info(f"PR {pr.number} is marked is draft. Not mergeable.")
        return False

    # Check that this PR is against a dev branch
    if (
        re.match(
            rf"^fc-({'|'.join(config.general.platform_versions)})-dev$",
            pr.base.ref,
        )
        is None
    ):
        logging.info(
            f"PR {pr.number} is not against a allowed dev branch. Not auto mergeable."
        )
        return False

    # Check if the PR has enough approvals
    transport = RequestsHTTPTransport(
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}"},
    )
    client = Client(transport=transport, fetch_schema_from_transport=True)

    query = gql(
        f"""
      query {{
        repository(name: "{repo.name}", owner: "{repo.owner.login}") {{
          pullRequest(number: {pr.number}) {{
            reviewDecision
          }}
        }}
      }}
      """
    )

    result = client.execute(query)
    # reviewDecision considers the policy configured for the repository
    result_pr = result["repository"]["pullRequest"]
    review_decision = result_pr["reviewDecision"]
    if review_decision != "APPROVED":
        logging.info(f"PR {pr.number} has not enough approvals. Not mergeable.")
        return False

    # check if all status checks were successful
    gh_process_env = os.environ.copy()
    gh_process_env["GH_TOKEN"] = token
    workflow_runs_cmd = subprocess.check_output(
        [
            "gh",
            "pr",
            "checks",
            str(pr.number),
            "--json",
            "bucket,name",
            "-R",
            config.general.fc_nixos_repo_name,
        ],
        env=gh_process_env,
    )

    workflow_runs = json.loads(workflow_runs_cmd)

    # Check that all workflow runs except for the current are successful
    for workflow_run in workflow_runs:
        if workflow_run["name"] == "check-auto-mergeability-of-pr":
            continue
        if workflow_run["name"] == "Backport Pull Request":
            continue
        if workflow_run["bucket"] == "skipping":
            continue
        if workflow_run["bucket"] != "pass":
            logging.info(
                f"PR {pr.number} has unsuccessful workflow run {workflow_run['name']} (state {workflow_run['bucket']}). Not mergeable."
            )
            return False
    return True


def create_pr_comment(pr: PullRequest, merge_date: datetime.date):
    msg = f"This PR is ready to merge. Merge scheduled for {merge_date.isoformat()}"
    for comment in pr.get_issue_comments():
        if comment.body == msg:
            return
    pr.create_issue_comment(msg)


def mark_pr_merge_label(pr: PullRequest, mergable: bool):
    LABEL_NAME = "auto-merge scheduled"
    auto_merge_label_set = False
    for label in pr.labels:
        if label.name == LABEL_NAME:
            auto_merge_label_set = True
    if mergable and not auto_merge_label_set:
        pr.add_to_labels(LABEL_NAME)
    elif not mergable and auto_merge_label_set:
        pr.remove_from_labels(LABEL_NAME)
