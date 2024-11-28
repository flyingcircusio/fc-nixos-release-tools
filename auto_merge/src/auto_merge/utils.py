import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dateutil import rrule
from dateutil.relativedelta import relativedelta


@dataclass
class PRMergeDayConfig:
    max_risk: int
    min_urgency: int


@dataclass
class GeneralConfig:
    # Our days are virtual to the production merge day and cutoff hour
    production_merge_day: int
    production_merge_cutoff_hour: int


@dataclass
class Config:
    pr_merge_days: dict[int, PRMergeDayConfig]
    general: GeneralConfig


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
    daydiff = now.weekday() - last_prod_merge.weekday()
    return (
        ((now - last_prod_merge).days - daydiff) // 7 * 5
        + min(daydiff, 5)
        - (max(now.weekday() - 4, 0) % 5)
    )


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
