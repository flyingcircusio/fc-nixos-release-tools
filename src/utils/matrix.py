import requests


class MatrixHookshot:
    def __init__(self, hookshot_url):
        self.hookshot_url = hookshot_url

    def send_notification(self, message: str):
        requests.put(self.hookshot_url, json={
            "text": message
        }).raise_for_status()