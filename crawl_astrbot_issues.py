#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

try:
    from tqdm import tqdm  # type: ignore[import-not-found]
except ImportError:
    tqdm = None


API_BASE = "https://api.github.com"
ISSUES_ENDPOINT = "/repos/AstrBotDevs/AstrBot/issues"
DEFAULT_QUERY = {
    "state": "open",
    "labels": "plugin-publish",
    "per_page": "100",
}
DEBUG = False


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {message}", file=sys.stderr)


def ensure_unreviewed_prefix(display_name: str) -> str:
    value = str(display_name or "").strip()
    if not value:
        return "未审核："
    if value.startswith("未审核："):
        return value
    return f"未审核：{value}"


def iter_with_progress(items: List[Dict[str, Any]], enable_progress: bool):
    if enable_progress and tqdm is not None:
        return tqdm(items, desc="Processing issues", unit="issue")
    if enable_progress and tqdm is None:
        print("[WARN] tqdm not installed, fallback to plain iteration", file=sys.stderr)
    return items


def github_get_json(url: str, token: Optional[str]) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "astrbot-plugin-crawler",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, headers=headers)
    try:
        debug_log(f"GET {url}")
        with request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
            debug_log(f"GET {url} -> HTTP {resp.status}, {len(payload)} bytes")
            return json.loads(payload)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"GitHub API request failed: {url} -> {exc.code} {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error when requesting {url}: {exc}") from exc


def fetch_open_plugin_publish_issues(token: Optional[str]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    page = 1

    while True:
        query = dict(DEFAULT_QUERY)
        query["page"] = str(page)
        url = f"{API_BASE}{ISSUES_ENDPOINT}?{parse.urlencode(query)}"
        data = github_get_json(url, token)
        if not isinstance(data, list) or not data:
            debug_log(f"issues page {page}: empty or invalid response, stop paging")
            break

        debug_log(f"issues page {page}: received {len(data)} items")

        for item in data:
            if isinstance(item, dict) and "pull_request" not in item:
                issues.append(item)

        debug_log(f"issues page {page}: accumulated issue count {len(issues)}")

        if len(data) < int(DEFAULT_QUERY["per_page"]):
            debug_log(f"issues page {page}: less than per_page, stop paging")
            break
        page += 1

    debug_log(f"final issue count: {len(issues)}")
    return issues


def normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-：:]+", "", key.strip().lower())


def parse_list_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip('"\'') for part in inner.split(",") if part.strip()]

    parts = re.split(r"[,，/|、]", text)
    return [p.strip() for p in parts if p.strip()]


def parse_yaml_like_block(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue

        m = re.match(r"^\s*([\w\u4e00-\u9fff\-\s]+?)\s*:\s*(.*?)\s*$", line)
        if not m:
            i += 1
            continue

        raw_key = m.group(1).strip()
        raw_val = m.group(2).strip()

        if raw_val == "":
            items: List[str] = []
            j = i + 1
            while j < len(lines):
                li = lines[j]
                li_m = re.match(r"^\s*-\s*(.+?)\s*$", li)
                if not li_m:
                    break
                item = li_m.group(1).strip().strip('"\'')
                if item:
                    items.append(item)
                j += 1
            if items:
                result[raw_key] = items
                i = j
                continue

        cleaned = raw_val.strip().strip('"\'')
        result[raw_key] = cleaned
        i += 1
    return result


def extract_candidate_blocks(body: str) -> List[str]:
    blocks: List[str] = []
    for m in re.finditer(r"```(?:yaml|yml|json|txt|md)?\s*\n(.*?)```", body, flags=re.S | re.I):
        blocks.append(m.group(1).strip())
    blocks.append(body)
    return blocks


def remap_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    key_map = {
        "name": "name",
        "pluginname": "name",
        "插件名": "name",
        "插件名称": "name",
        "displayname": "display_name",
        "显示名": "display_name",
        "显示名称": "display_name",
        "desc": "desc",
        "description": "desc",
        "描述": "desc",
        "author": "author",
        "作者": "author",
        "repo": "repo",
        "repository": "repo",
        "仓库": "repo",
        "仓库地址": "repo",
        "项目地址": "repo",
        "tags": "tags",
        "tag": "tags",
        "标签": "tags",
        "sociallink": "social_link",
        "social": "social_link",
        "社交链接": "social_link",
        "主页": "social_link",
        "home": "social_link",
    }

    out: Dict[str, Any] = {}
    for raw_key, value in raw.items():
        nk = normalize_key(str(raw_key))
        target = key_map.get(nk)
        if not target:
            continue
        out[target] = value

    if "tags" in out:
        out["tags"] = parse_list_value(out.get("tags"))

    return out


def parse_issue_plugin_info(issue: Dict[str, Any]) -> Dict[str, Any]:
    body = str(issue.get("body") or "")
    title = str(issue.get("title") or "")

    best: Dict[str, Any] = {}
    for block in extract_candidate_blocks(body):
        block = block.strip()
        if not block:
            continue

        candidate: Dict[str, Any] = {}
        if block.startswith("{") and block.endswith("}"):
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict):
                    candidate = remap_fields(parsed)
            except json.JSONDecodeError:
                candidate = {}
        if not candidate:
            candidate = remap_fields(parse_yaml_like_block(block))

        if len(candidate) > len(best):
            best = candidate

    repo_match = re.search(r"https?://github\.com/[\w.-]+/[\w.-]+", body)
    if repo_match and "repo" not in best:
        best["repo"] = repo_match.group(0).rstrip("/)")

    if "display_name" not in best and title:
        best["display_name"] = re.sub(r"^[\[【].*?[\]】]\s*", "", title).strip()

    if "tags" not in best:
        tag_line = re.search(r"(?:标签|tags?)\s*[:：]\s*(.+)", body, flags=re.I)
        best["tags"] = parse_list_value(tag_line.group(1)) if tag_line else []

    repo_owner = None
    repo_name = None
    if best.get("repo"):
        repo_owner, repo_name = parse_github_repo_url(str(best["repo"]))

    if "name" not in best:
        if repo_name:
            best["name"] = repo_name
        else:
            best["name"] = slugify_name(str(best.get("display_name") or title or "unknown-plugin"))

    if "author" not in best and repo_owner:
        best["author"] = repo_owner

    if "social_link" not in best and repo_owner:
        best["social_link"] = f"https://github.com/{repo_owner}"

    if "desc" not in best:
        best["desc"] = ""

    if "display_name" not in best:
        best["display_name"] = best["name"]
    best["display_name"] = ensure_unreviewed_prefix(str(best.get("display_name") or ""))

    if "repo" not in best:
        best["repo"] = ""

    if "tags" not in best:
        best["tags"] = []

    return best


def slugify_name(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9_\-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_") or "unknown-plugin"


def parse_github_repo_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        p = parse.urlparse(url)
    except Exception:
        return None, None
    if p.netloc.lower() not in {"github.com", "www.github.com"}:
        return None, None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None, None
    owner = parts[0]
    repo = parts[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def fetch_repo_meta(owner: str, repo: str, token: Optional[str]) -> Dict[str, Any]:
    debug_log(f"fetch repo meta: {owner}/{repo}")
    repo_url = f"{API_BASE}/repos/{owner}/{repo}"
    data = github_get_json(repo_url, token)
    default_branch = data.get("default_branch") or "main"
    stars = int(data.get("stargazers_count") or 0)
    # pushed_at is closer to "last modified" for repository code changes.
    updated_at = data.get("pushed_at") or data.get("updated_at") or ""

    version = fetch_version_from_metadata(owner, repo, default_branch, token)
    logo = find_logo(owner, repo, default_branch, token)

    out = {
        "stars": stars,
        "version": version or "v0.0.0",
        "updated_at": updated_at,
    }
    if logo:
        out["logo"] = logo
    debug_log(
        f"repo meta done: {owner}/{repo}, stars={out['stars']}, version={out['version']}, "
        f"updated_at={out['updated_at']}, logo={'yes' if 'logo' in out else 'no'}"
    )
    return out


def fetch_version_from_metadata(owner: str, repo: str, branch: str, token: Optional[str]) -> Optional[str]:
    candidates = [
        "metadata.yaml",
        "metadata.yml",
        "plugin/metadata.yaml",
        "plugin/metadata.yml",
    ]
    for path in candidates:
        content = fetch_repo_file_content(owner, repo, path, branch, token)
        if not content:
            debug_log(f"version probe miss: {owner}/{repo}:{path}")
            continue
        m = re.search(r"^\s*version\s*:\s*['\"]?([^'\"\n]+)", content, flags=re.M)
        if m:
            debug_log(f"version probe hit: {owner}/{repo}:{path} -> {m.group(1).strip()}")
            return m.group(1).strip()
    debug_log(f"version probe fallback: {owner}/{repo} -> v0.0.0")
    return None


def fetch_repo_file_content(
    owner: str,
    repo: str,
    path: str,
    branch: str,
    token: Optional[str],
) -> Optional[str]:
    url = f"{API_BASE}/repos/{owner}/{repo}/contents/{parse.quote(path)}?ref={parse.quote(branch)}"
    try:
        data = github_get_json(url, token)
    except RuntimeError:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("type") != "file":
        return None
    encoded = data.get("content")
    if not isinstance(encoded, str):
        return None
    try:
        decoded = base64.b64decode(encoded, validate=False)
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return None


def find_logo(owner: str, repo: str, branch: str, token: Optional[str]) -> Optional[str]:
    # Requirement asks only logo.png; if absent, omit this field.
    path = "logo.png"
    url = f"{API_BASE}/repos/{owner}/{repo}/contents/{path}?ref={parse.quote(branch)}"
    try:
        data = github_get_json(url, token)
    except RuntimeError:
        debug_log(f"logo probe miss: {owner}/{repo}:{path}")
        return None
    if isinstance(data, dict) and data.get("type") == "file":
        debug_log(f"logo probe hit: {owner}/{repo}:{path}")
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    debug_log(f"logo probe miss: {owner}/{repo}:{path}")
    return None


def format_plugin_key(name: str) -> str:
    # Keep output keys stable and filesystem/repo-name friendly.
    return slugify_name(name)


def build_output(issues: List[Dict[str, Any]], token: Optional[str], enable_progress: bool) -> Dict[str, Dict[str, Any]]:
    results: List[Tuple[str, Dict[str, Any]]] = []
    seen = set()

    issue_iter = iter_with_progress(issues, enable_progress)

    for idx, issue in enumerate(issue_iter, start=1):
        issue_no = issue.get("number", "?")
        issue_title = str(issue.get("title") or "")
        debug_log(f"issue {idx}/{len(issues)} start: #{issue_no} {issue_title}")
        plugin = parse_issue_plugin_info(issue)
        name = str(plugin.get("name") or "").strip()
        if not name:
            debug_log(f"issue #{issue_no} skipped: empty plugin name")
            continue
        output_key = format_plugin_key(name)
        if output_key in seen:
            debug_log(f"issue #{issue_no} skipped: duplicate key {output_key}")
            continue

        plugin["stars"] = 0
        plugin["version"] = "v0.0.0"
        plugin["updated_at"] = ""

        owner, repo = parse_github_repo_url(str(plugin.get("repo") or ""))
        if owner and repo:
            try:
                repo_meta = fetch_repo_meta(owner, repo, token)
                plugin.update(repo_meta)
            except RuntimeError:
                debug_log(f"issue #{issue_no}: repo meta fetch failed for {owner}/{repo}, use defaults")
                pass
        else:
            debug_log(f"issue #{issue_no}: no valid repo url, use defaults")

        if "logo" in plugin and not plugin["logo"]:
            plugin.pop("logo", None)

        # Keep only requested keys and stable ordering.
        normalized = {
            "name": output_key,
            "display_name": ensure_unreviewed_prefix(str(plugin.get("display_name", ""))),
            "desc": plugin.get("desc", ""),
            "author": plugin.get("author", ""),
            "repo": plugin.get("repo", ""),
            "tags": plugin.get("tags", []),
            "social_link": plugin.get("social_link", ""),
            "stars": int(plugin.get("stars") or 0),
            "version": plugin.get("version") or "v0.0.0",
            "updated_at": plugin.get("updated_at", ""),
        }
        if "logo" in plugin:
            normalized["logo"] = plugin["logo"]

        results.append((output_key, normalized))
        seen.add(output_key)
        debug_log(
            f"issue #{issue_no} done: key={output_key}, stars={normalized['stars']}, "
            f"version={normalized['version']}, logo={'yes' if 'logo' in normalized else 'no'}"
        )

    results.sort(key=lambda x: x[0])
    return {key: value for key, value in results}


def main() -> int:
    global DEBUG
    parser = argparse.ArgumentParser(description="Crawl AstrBot plugin-publish issues via GitHub API")
    parser.add_argument(
        "-o",
        "--output",
        default="plugin_source.json",
        help="Output JSON file path (default: plugin_source.json)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN", ""),
        help="GitHub token, defaults to env GITHUB_TOKEN",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug logs to stderr during crawling",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bar",
    )
    args = parser.parse_args()
    DEBUG = args.debug

    token = args.token.strip() or None

    try:
        debug_log("crawler started")
        issues = fetch_open_plugin_publish_issues(token)
        debug_log(f"parsing {len(issues)} issues")
        payload = build_output(issues, token, enable_progress=not args.no_progress)
        debug_log(f"build output done: {len(payload)} plugins")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Wrote {len(payload)} plugins to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
