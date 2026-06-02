"""Open a GitLab merge request via the ``glab`` CLI, with a REST fallback.

The branch must already be pushed. Auth comes from env (GITLAB_TOKEN, and
GITLAB_HOST for self-managed); credentials are never logged or put in URLs.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from . import proc
from .config import Config
from .report import today

log = logging.getLogger("mvn_upgrader.gitlab_mr")

_URL_RE = re.compile(r"https?://\S+/-/merge_requests/\d+")


def _title() -> str:
    return f"Dependency upgrades {today()}"


def build_glab_command(
    cfg: Config, branch: str, description_file: Optional[Path], use_file: bool
) -> list[str]:
    cmd = [
        "glab", "mr", "create",
        "--source-branch", branch,
        "--target-branch", cfg.git.base_branch,
        "--title", _title(),
        "--yes",
    ]
    if cfg.gitlab.project:
        cmd += ["-R", cfg.gitlab.project]
    if description_file and use_file:
        cmd += ["--description-file", str(description_file)]
    return cmd


def _parse_mr_url(text: str) -> Optional[str]:
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def create_mr_via_glab(
    cfg: Config, branch: str, description_file: Optional[Path], runner=proc.run
) -> Optional[str]:
    cmd = build_glab_command(cfg, branch, description_file, use_file=True)
    res = runner(cmd, cwd=cfg.repo, redact=cfg.secret_values())
    if not res.ok and description_file is not None:
        # Older glab may lack --description-file: retry with inline --description.
        body = description_file.read_text(encoding="utf-8")
        cmd = build_glab_command(cfg, branch, None, use_file=False)
        cmd += ["--description", body]
        res = runner(cmd, cwd=cfg.repo, redact=cfg.secret_values())
    if not res.ok:
        log.warning("glab mr create failed (rc=%s)", res.returncode)
        return None
    return _parse_mr_url(res.combined)


def create_mr_via_rest(
    cfg: Config, branch: str, description: str, session=None
) -> Optional[str]:
    token = cfg.gitlab_token
    if not token or not cfg.gitlab.project:
        log.warning("GitLab REST fallback needs GITLAB_TOKEN and gitlab.project")
        return None
    host = cfg.gitlab_host or "https://gitlab.com"
    host = host if host.startswith("http") else f"https://{host}"
    project_enc = urllib.parse.quote(cfg.gitlab.project, safe="")
    url = f"{host.rstrip('/')}/api/v4/projects/{project_enc}/merge_requests"
    payload = {
        "source_branch": branch,
        "target_branch": cfg.git.base_branch,
        "title": _title(),
        "description": description,
    }
    if session is None:
        import requests

        session = requests.Session()
    resp = session.post(
        url, headers={"PRIVATE-TOKEN": token}, json=payload, timeout=30
    )
    if getattr(resp, "status_code", 500) >= 300:
        log.warning("GitLab REST mr create failed (HTTP %s)", resp.status_code)
        return None
    data = resp.json()
    return data.get("web_url")


def create_mr(
    cfg: Config, *, branch: str, description_file: Path, runner=proc.run, session=None
) -> Optional[str]:
    """Create the MR, preferring glab and falling back to the REST API."""
    if proc.which("glab"):
        url = create_mr_via_glab(cfg, branch, description_file, runner=runner)
        if url:
            print(f"merge request: {url}")
            return url
        log.info("glab path did not yield an MR URL; trying REST fallback")

    body = description_file.read_text(encoding="utf-8") if description_file.is_file() else ""
    url = create_mr_via_rest(cfg, branch, body, session=session)
    if url:
        print(f"merge request: {url}")
    return url
