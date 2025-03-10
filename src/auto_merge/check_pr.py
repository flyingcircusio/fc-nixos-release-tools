from github import Auth, Github

from auto_merge import utils
from auto_merge.config import Config


def check_pr(pr_id: int, github_access_token: str, config: Config):
    gh = Github(auth=Auth.Token(github_access_token))
    repository = gh.get_repo(config.general.fc_nixos_repo_name)

    pr = repository.get_pull(pr_id)
    risk, urgency = utils.get_label_values_for_pr(pr.labels)
    if risk is None or urgency is None:
        # This raises a runtime error, so it shows as a red check indicator in GitHub
        raise RuntimeError(
            "PR doesn't have risk and urgency labels. Not mergeable."
        )
    # check if PR is approved
    mergeable = utils.check_pr_mergeable(
        repository, pr, github_access_token, config
    )
    if mergeable:
        merge_date = utils.calculate_merge_date(risk, urgency, config)
        msg = f"This PR is ready to merge. Merge scheduled for {merge_date.isoformat()}"
        for comment in pr.get_issue_comments():
            if comment.body == msg:
                return
        pr.create_issue_comment(msg)
