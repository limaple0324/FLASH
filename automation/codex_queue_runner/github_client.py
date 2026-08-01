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
    max_pages: int = 50

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.api_url}{path}", data=json.dumps(body).encode("utf-8") if body is not None else None, method=method, headers=headers)
        try:
            with urlopen(request, timeout=20) as response: data, response_headers = response.read(), dict(response.headers.items())
        except (HTTPError, URLError) as exc: raise QueueRunError(f"GitHub REST {method} {path} 失敗: {exc}") from exc
        return (json.loads(data.decode("utf-8")) if data else {}), response_headers

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, body)[0]

    def _paged(self, path: str) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []; next_path: str | None = path
        for _ in range(self.max_pages):
            if not next_path: return all_items
            page, headers = self._request("GET", next_path)
            if not isinstance(page, list): raise QueueRunError("GitHub 分頁回應格式不正確")
            all_items.extend(page)
            link = headers.get("Link", ""); next_path = None
            for item in link.split(","):
                if 'rel="next"' in item and "<" in item and ">" in item:
                    url = item[item.index("<") + 1:item.index(">")]; next_path = url.replace(self.api_url, "", 1); break
        raise QueueRunError("GitHub 留言分頁超過上限")

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paged(f"/repos/{self.repository}/issues/{number}/comments?per_page=100")
    def list_pull_request_files(self, number: int) -> list[dict[str, Any]]:
        return self._paged(f"/repos/{self.repository}/pulls/{number}/files?per_page=100")
    def post_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._json("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})
    def get_issue(self, number: int) -> dict[str, Any]: return self._json("GET", f"/repos/{self.repository}/issues/{number}")
    def get_pull_request(self, number: int) -> dict[str, Any]: return self._json("GET", f"/repos/{self.repository}/pulls/{number}")
    def get_workflow_run(self, number: str) -> dict[str, Any]: return self._json("GET", f"/repos/{self.repository}/actions/runs/{quote(str(number), safe='')}")
    def get_commit(self, sha: str) -> dict[str, Any]: return self._json("GET", f"/repos/{self.repository}/commits/{quote(sha, safe='')}")
    def get_branch_sha(self, branch: str) -> str:
        return str(self._json("GET", f"/repos/{self.repository}/git/ref/{quote('heads/' + branch, safe='/')}")["object"]["sha"])
    def write_source_pr(self, number: int, body: str) -> dict[str, Any]: return self.post_issue_comment(number, body)
    def write_blocker(self, body: str) -> dict[str, Any]: return self.post_issue_comment(18, body)
    def dispatch_next(self, queue_id: str) -> dict[str, Any]:
        return self._json("POST", f"/repos/{self.repository}/dispatches", {"event_type": "codex_queue_next", "client_payload": {"queue_id": queue_id}})
