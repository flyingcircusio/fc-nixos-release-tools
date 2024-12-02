import os

from github import Github, Auth
import auto_merge.utils
from auto_merge.config import Config

def check_pr(repo_name: str, pr_id: int, config: Config):
    token = os.environ["GH_TOKEN"]
    gh = Github(auth=Auth.Token(os.environ["GH_TOKEN"]))
    repository = gh.get_repo(repo_name)

    # check if PR is approved
    pr = repository.get_pull(pr_id)
    mergeable = auto_merge.utils.check_pr_mergeable(repository, pr, token)
    if mergeable:
        risk,urgency = auto_merge.utils.get_label_values_for_pr(pr.labels)
        merge_date = auto_merge.utils.calculate_merge_date(risk, urgency, config)
        msg = f"This PR is ready to merge. Merge scheduled for {merge_date.isoformat()}"
        for comment in pr.get_issue_comments():
            if comment.body == msg:
                return
        pr.create_issue_comment(f"This PR is ready to merge. Merge scheduled for {merge_date.isoformat()}")

