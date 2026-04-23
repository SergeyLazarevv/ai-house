"""GitLab MCP server: read-only repo tools for the code agent."""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("gitlab")


def _config_error() -> str | None:
    url = (os.getenv("GITLAB_URL") or "").strip().rstrip("/")
    token = (os.getenv("GITLAB_TOKEN") or "").strip()
    if not url:
        return "GitLab не настроен: укажите GITLAB_URL."
    if not token:
        return "GitLab не настроен: укажите GITLAB_TOKEN."
    return None


def _base_url() -> str:
    return (os.getenv("GITLAB_URL") or "https://gitlab.com").strip().rstrip("/")


def _headers() -> dict[str, str]:
    token = (os.getenv("GITLAB_TOKEN") or "").strip()
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


def _compact_json(data: Any, limit: int | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if limit and len(text) > limit:
        return text[: limit - 40] + "\n… [обрезано]"
    return text


def _project_identifier(project: str | int) -> str:
    text = str(project).strip()
    return quote(text, safe="")


async def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(
        base_url=_base_url(),
        timeout=httpx.Timeout(60.0),
        headers=_headers(),
    ) as client:
        r = await client.get(path, params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def gitlab_list_projects(search: str | None = None, membership: bool = True, per_page: int = 20) -> str:
    err = _config_error()
    if err:
        return err
    params = {
        "simple": True,
        "per_page": max(1, min(100, int(per_page or 20))),
        "order_by": "last_activity_at",
        "sort": "desc",
    }
    if membership:
        params["membership"] = True
    if search:
        params["search"] = search.strip()
    data = await _request("/api/v4/projects", params=params)
    if not isinstance(data, list):
        return _compact_json({"projects": [], "count": 0}, limit=40_000)
    rows = [
        {
            "id": item.get("id"),
            "path_with_namespace": item.get("path_with_namespace"),
            "name": item.get("name"),
            "default_branch": item.get("default_branch"),
            "web_url": item.get("web_url"),
            "description": item.get("description"),
        }
        for item in data
        if isinstance(item, dict)
    ]
    return _compact_json({"projects": rows, "count": len(rows)}, limit=40_000)


@mcp.tool()
async def gitlab_get_file(project: str, file_path: str, ref: str | None = None) -> str:
    err = _config_error()
    if err:
        return err
    project_id = _project_identifier(project)
    path = (file_path or "").strip().lstrip("/")
    if not path:
        return "GitLab: укажите file_path."
    ref_name = (ref or "").strip()
    if not ref_name:
        data = await _request(f"/api/v4/projects/{project_id}")
        if isinstance(data, dict):
            ref_name = str(data.get("default_branch") or "main")
        else:
            ref_name = "main"
    encoded_file = quote(path, safe="")
    async with httpx.AsyncClient(
        base_url=_base_url(),
        timeout=httpx.Timeout(60.0),
        headers=_headers(),
    ) as client:
        r = await client.get(
            f"/api/v4/projects/{project_id}/repository/files/{encoded_file}",
            params={"ref": ref_name},
        )
        if r.status_code == 404:
            return f"GitLab: файл не найден: {project}:{path}@{ref_name}"
        r.raise_for_status()
        data = r.json()
    payload = {
        "project": project,
        "file_path": path,
        "ref": ref_name,
        "blob_id": data.get("blob_id"),
        "content_sha256": data.get("content_sha256"),
        "file_name": data.get("file_name"),
        "size": data.get("size"),
        "encoding": data.get("encoding"),
    }
    raw_content = data.get("content")
    if isinstance(raw_content, str) and str(data.get("encoding") or "").lower() == "base64":
        try:
            payload["content"] = base64.b64decode(raw_content).decode("utf-8", errors="replace")
        except Exception:
            payload["content"] = raw_content
    else:
        payload["content"] = raw_content
    return _compact_json(payload, limit=80_000)


if __name__ == "__main__":
    mcp.run()
