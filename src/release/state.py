import json
from collections import defaultdict
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

STATE_FILE = Path("state.json")


class STAGE(StrEnum):
    INIT = "init"
    BRANCH = "branch"
    TAG = "tag"
    DONE = "done"


class BranchState(TypedDict, total=False):
    tested: bool
    orig_staging_commit: str
    new_production_commit: str
    hydra_eval_id: str
    changelog: str


class State(TypedDict, total=False):
    release_id: str
    release_date: str
    stage: STAGE
    branches: dict[str, BranchState]
    changelog_url: str


def new_state() -> State:
    return {"stage": STAGE.INIT, "branches": defaultdict(dict)}


def load_state() -> State:
    if not STATE_FILE.exists():
        return new_state()
    state = json.loads(STATE_FILE.read_text())
    state["branches"] = defaultdict(dict, state["branches"])
    return state


def store_state(state: State):
    STATE_FILE.write_text(json.dumps(state))
