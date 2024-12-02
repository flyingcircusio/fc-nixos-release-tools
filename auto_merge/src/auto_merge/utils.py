import datetime
from logging import info
from zoneinfo import ZoneInfo

from dateutil import rrule
from dateutil.relativedelta import relativedelta
from github import Label
from github.PullRequest import PullRequest
from github.Repository import Repository

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from auto_merge.config import Config


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
    elif (
            now.weekday() == config.general.production_merge_day
            and now.hour < config.general.production_merge_cutoff_hour
    ):
        day = now
    else:
        day = now + relativedelta(
            days=+1, weekday=+config.general.production_merge_day
        )
    return day.replace(hour=12)


def now_relative_day(config: Config) -> int:
    now = now_tz()
    last_prod_merge = last_production_merge(config)

    # Calculate workdays between the last production merge and now, excluding the start day
    workdays = 0
    for dt in rrule.rrule(rrule.DAILY, dtstart=last_prod_merge, until=now).xafter(last_prod_merge):
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

def get_label_values_for_pr(labels: list[Label]) -> (int, int):
    risk = None
    urgency = None
    for label in labels:
        if label.name.startswith("risk:"):
            risk = int(label.name.split("risk:")[1])
        if label.name.startswith("urgency:"):
            urgency = int(label.name.split("urgency:")[1])
    return (risk, urgency)

def check_pr_mergeable(repo: Repository, pr: PullRequest, token: str) -> bool:
    if not pr.mergeable:
        info(f"PR {pr.number} is marked has conflicts. Not mergeable.")
        return False

    if pr.draft:
        info(f"PR {pr.number} is marked is draft. Not mergeable.")
        return False

    # Check if the PR has enough approvals
    transport = RequestsHTTPTransport(url="https://api.github.com/graphql", headers={"Authorization": f"Bearer {token}"})
    client = Client(transport=transport, fetch_schema_from_transport=True)

    query = gql(f"""
      query {{
        repository(name: "{repo.name}", owner: "{repo.owner.login}") {{
          pullRequest(number: {pr.number}) {{
            reviewDecision
            statusCheckRollup {{
              state
            }}
          }}
        }}
      }}
      """)

    result = client.execute(query)
    # reviewDecision considers the policy configured for the repository
    result_pr = result["repository"]["pullRequest"]
    review_decision = result_pr["reviewDecision"]
    if review_decision != "APPROVED":
        info(f"PR {pr.number} has not enough approvals. Not mergeable.")
        return False
    # check if all status checks were successful
    check_status = result_pr['statusCheckRollup']['state']
    # if check_status != "SUCCESS":
    #     info(f"PR {pr.number} has unsuccessful checks. Not mergeable.")
    #     return False
    return True