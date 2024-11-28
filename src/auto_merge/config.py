import tomllib

from pydantic import BaseModel


class PRMergeDayConfig(BaseModel):
    max_risk: int
    min_urgency: int


class GeneralConfig(BaseModel):
    # Our days are virtual to the production merge day and cutoff hour
    production_merge_day: int
    fc_nixos_repo_name: str
    platform_versions: list[str]


class MonitoringReviewConfig(BaseModel):
    names: list[str]
    notification_cutoff_hour: int


class Config(BaseModel):
    pr_merge_days: dict[int, PRMergeDayConfig]
    general: GeneralConfig
    monitoring_review: MonitoringReviewConfig


def load_config() -> Config:
    with open("auto-merge-config.toml", "rb") as f:
        data = tomllib.load(f)
        return Config.model_validate(data)
