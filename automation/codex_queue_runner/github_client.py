from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import QueueRunError


@dataclass
class GitHubRestClient:
    repository: str
    token: str
    api_url: str = "https://api.github.com"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.api_url}{path}", data=json.dumps(body).encode("utf-8") if body else None, method=method, headers=headers)
        try:
            with urlopen(request, timeout=20) as response: data = response.read()
        except (HTTPError, URLError) as exc:
            raise QueueRunError(f"GitHub REST {method} {path} 失敗: {exc}") from exc
        return json.loads(data.decode("utf-8")) if data else {}

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/repos/{self.repository}/issues/{number}/comments?per_page=100")
    def post_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})
    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repository}/issues/{number}")
    def get_pull_request(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repository}/pulls/{number}")
    def get_branch_sha(self, branch: str) -> str:
        value = self._request("GET", f"/repos/{self.repository}/git/ref/{quote('heads/' + branch, safe='/')}")
        return str(value["object"]["sha"])
    def write_source_pr(self, number: int, body: str) -> dict[str, Any]:
        return self.post_issue_comment(number, body)
    def write_blocker(self, body: str) -> dict[str, Any]:
        return self.post_issue_comment(18, body)
    def dispatch_next(self, queue_id: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.repository}/dispatches", {"event_type": "codex_queue_next", "client_payload": {"queue_id": queue_id}})
