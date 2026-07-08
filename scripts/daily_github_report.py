#!/usr/bin/env python3
"""Generate and email a daily GitHub hot-project report.

Runs with Python standard library only. It prefers GitHub Trending (HTML page,
low API pressure) and uses GitHub Search API as an optional supplement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_TRENDING_URL = "https://github.com/trending"
DEFAULT_MAIL_TO = "3087130357@qq.com"
DEFAULT_REPORT_DAYS = 7
HTTP_TIMEOUT_SECONDS = 12
HTTP_RETRIES = 2

INNOVATION_KEYWORDS = {
    "ai", "agent", "agents", "llm", "rag", "automation", "robot", "robotics",
    "developer", "devtool", "sandbox", "workflow", "data", "infra", "rust",
    "typescript", "python", "model", "coding", "assistant", "mcp",
}
FUN_KEYWORDS = {
    "fun", "game", "terminal", "cli", "visual", "visualization", "creative",
    "awesome", "toy", "demo", "music", "video", "ui", "desktop", "shell",
}

TOPIC_LABELS = {
    "ai": "AI",
    "llm": "大模型",
    "agent": "智能体",
    "agents": "智能体",
    "automation": "自动化",
    "developer": "开发工具",
    "devtool": "开发工具",
    "workflow": "工作流",
    "robotics": "机器人",
    "data": "数据工程",
    "fun": "趣味",
    "game": "游戏",
    "terminal": "终端",
    "visualization": "可视化",
    "creative": "创意编程",
    "cli": "命令行",
    "awesome": "资源集合",
}


@dataclass(frozen=True)
class Repo:
    full_name: str
    html_url: str
    description: str
    language: str
    stargazers_count: int
    forks_count: int
    open_issues_count: int
    created_at: str
    updated_at: str
    pushed_at: str
    topics: tuple[str, ...]
    score: float
    section_hint: str = ""
    period_stars: int = 0

    @classmethod
    def from_api(cls, item: dict, score: float, section_hint: str = "") -> "Repo":
        return cls(
            full_name=item.get("full_name") or item.get("name") or "unknown",
            html_url=item.get("html_url") or "",
            description=(item.get("description") or "暂无简介").strip(),
            language=item.get("language") or "Unknown",
            stargazers_count=int(item.get("stargazers_count") or 0),
            forks_count=int(item.get("forks_count") or 0),
            open_issues_count=int(item.get("open_issues_count") or 0),
            created_at=item.get("created_at") or "",
            updated_at=item.get("updated_at") or "",
            pushed_at=item.get("pushed_at") or item.get("updated_at") or "",
            topics=tuple(item.get("topics") or ()),
            score=score,
            section_hint=section_hint,
            period_stars=int(item.get("period_stars") or 0),
        )


def load_dotenv(path: Path) -> None:
    """Load a small .env file without overriding existing environment variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_github_time(value: str) -> dt.datetime:
    if not value:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_count(value: str) -> int:
    raw = value or ""
    match = re.search(r"([0-9][0-9,.]*)(\s*[kK])?", raw)
    if not match:
        return 0
    cleaned = match.group(1).replace(",", "")
    multiplier = 1000 if match.group(2) else 1
    try:
        return int(float(cleaned) * multiplier)
    except ValueError:
        return 0


def extract_keywords(*parts: str) -> tuple[str, ...]:
    text = " ".join(parts).lower()
    tokens = set(re.split(r"[^a-z0-9-]+", text))
    keywords = sorted((tokens & INNOVATION_KEYWORDS) | (tokens & FUN_KEYWORDS))
    return tuple(keywords)


def fetch_url_text(url: str, retries: int = HTTP_RETRIES) -> str:
    headers = {"User-Agent": "daily-github-hot-projects-report"}
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def github_api_get(url: str, token: str | None, retries: int = HTTP_RETRIES) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-github-hot-projects-report",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"GitHub API HTTP {exc.code}: {body[:300]}")
            if exc.code in {403, 429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise last_error
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"GitHub API request failed: {exc}") from exc
    raise RuntimeError(f"GitHub API request failed: {last_error}")


def search_repositories(query: str, token: str | None, *, sort: str = "stars", order: str = "desc", per_page: int = 5) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "sort": sort, "order": order, "per_page": per_page})
    data = github_api_get(f"{GITHUB_SEARCH_URL}?{params}", token)
    return list(data.get("items") or [])


def is_probable_repo_name(full_name: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
        return False
    owner, repo = full_name.split("/", 1)
    blocked_owners = {
        "about", "account", "collections", "contact", "enterprise", "events", "features",
        "github", "login", "marketplace", "new", "notifications", "organizations", "pricing",
        "readme", "search", "security", "settings", "sponsors", "topics", "trending",
    }
    return owner.lower() not in blocked_owners and repo.lower() not in {"explore", "login", "signup"}


def parse_trending_cards(since: str = "daily") -> list[dict]:
    html_text = fetch_url_text(f"{GITHUB_TRENDING_URL}?{urllib.parse.urlencode({'since': since})}")
    cards = re.findall(r"<article\b.*?</article>", html_text, flags=re.S | re.I)
    repos: list[dict] = []
    seen: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for card in cards:
        link_match = re.search(r'<h2[^>]*>.*?<a[^>]+href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', card, flags=re.S | re.I)
        if not link_match:
            link_match = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', card)
        if not link_match:
            continue

        full_name = html.unescape(link_match.group(1)).strip()
        if not is_probable_repo_name(full_name) or full_name in seen:
            continue
        seen.add(full_name)

        desc_match = re.search(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', card, flags=re.S | re.I)
        language_match = re.search(r'<span[^>]*itemprop="programmingLanguage"[^>]*>(.*?)</span>', card, flags=re.S | re.I)
        star_match = re.search(r'<a[^>]+href="/[^"]+/stargazers"[^>]*>(.*?)</a>', card, flags=re.S | re.I)
        period_match = re.search(r'([0-9][0-9,.]*\s+stars\s+(?:today|this\s+week))', card, flags=re.I)

        description = strip_tags(desc_match.group(1)) if desc_match else "暂无简介"
        language = strip_tags(language_match.group(1)) if language_match else "Unknown"
        total_stars = parse_count(strip_tags(star_match.group(1))) if star_match else 0
        period_stars = parse_count(period_match.group(1)) if period_match else 0
        topics = extract_keywords(full_name, description, language)

        repos.append(
            {
                "full_name": full_name,
                "html_url": f"https://github.com/{full_name}",
                "description": description,
                "language": language,
                "stargazers_count": total_stars,
                "forks_count": 0,
                "open_issues_count": 0,
                "created_at": "",
                "updated_at": now,
                "pushed_at": now,
                "topics": topics,
                "period_stars": period_stars,
            }
        )
    return repos


def fetch_trending_repositories() -> list[tuple[dict, str]]:
    items: list[tuple[dict, str]] = []
    for since, limit in (("daily", 20), ("weekly", 12)):
        try:
            items.extend((item, f"trending-{since}") for item in parse_trending_cards(since)[:limit])
        except Exception as exc:
            print(f"WARN: failed to fetch GitHub Trending {since}: {exc}", file=sys.stderr)
    return items


def repo_score(item: dict, now: dt.datetime, hint: str = "") -> float:
    stars = int(item.get("stargazers_count") or 0)
    period_stars = int(item.get("period_stars") or 0)
    forks = int(item.get("forks_count") or 0)
    pushed = parse_github_time(item.get("pushed_at") or item.get("updated_at") or "")
    created = parse_github_time(item.get("created_at") or "")
    days_since_push = max((now - pushed).total_seconds() / 86400, 0)
    days_since_create = max((now - created).total_seconds() / 86400, 0)

    popularity = math.log10(stars + 1) * 360 + math.log10(forks + 1) * 120 + period_stars * 6
    recency_boost = max(0, 21 - days_since_push) * 45
    new_repo_boost = max(0, 45 - days_since_create) * 30
    trending_boost = 1600 if "trending-daily" in hint else 1000 if "trending-weekly" in hint else 0
    topics = set(item.get("topics") or ())
    innovation_boost = 450 if topics & INNOVATION_KEYWORDS else 0
    fun_boost = 350 if topics & FUN_KEYWORDS else 0
    return popularity + recency_boost + new_repo_boost + trending_boost + innovation_boost + fun_boost


def dedupe_and_rank(items: Iterable[tuple[dict, str]], now: dt.datetime) -> list[Repo]:
    best: dict[str, Repo] = {}
    for item, hint in items:
        full_name = item.get("full_name")
        if not full_name or item.get("archived") or item.get("disabled"):
            continue
        if int(item.get("stargazers_count") or 0) < 5 and not hint.startswith("trending-"):
            continue
        score = repo_score(item, now, hint)
        repo = Repo.from_api(item, score=score, section_hint=hint)
        existing = best.get(full_name)
        if existing is None or repo.score > existing.score:
            best[full_name] = repo
    return sorted(best.values(), key=lambda r: r.score, reverse=True)


def collect_candidates(report_days: int, token: str | None, *, skip_api: bool = False) -> tuple[list[Repo], list[Repo]]:
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=report_days)).date().isoformat()
    broader_since = (now - dt.timedelta(days=max(report_days, 14))).date().isoformat()

    trending_items = fetch_trending_repositories()
    innovation_items: list[tuple[dict, str]] = list(trending_items)
    fun_items: list[tuple[dict, str]] = list(trending_items)

    if not skip_api:
        innovation_queries = [
            f"created:>={since} stars:>=20",
            f"pushed:>={since} stars:>=50 topic:ai",
            f"pushed:>={since} stars:>=50 topic:automation",
            f"pushed:>={broader_since} stars:>=200 created:>={broader_since}",
        ]
        fun_queries = [
            f"created:>={since} stars:>=10 topic:fun",
            f"created:>={since} stars:>=10 topic:game",
            f"pushed:>={since} stars:>=50 topic:cli",
        ]

        for query in innovation_queries:
            try:
                innovation_items.extend((item, query) for item in search_repositories(query, token, per_page=5))
            except Exception as exc:
                print(f"WARN: innovation query failed: {query}: {exc}", file=sys.stderr)

        for query in fun_queries:
            try:
                fun_items.extend((item, query) for item in search_repositories(query, token, per_page=5))
            except Exception as exc:
                print(f"WARN: fun query failed: {query}: {exc}", file=sys.stderr)

    return dedupe_and_rank(innovation_items, now), dedupe_and_rank(fun_items, now)


def truncate(value: str, length: int = 180) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= length:
        return value
    return value[: length - 1].rstrip() + "…"


def format_date(value: str) -> str:
    if not value:
        return "\u672a\u77e5"
    try:
        return parse_github_time(value).strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def topic_display(topic: str) -> str:
    if not topic:
        return ""
    if topic in TOPIC_LABELS:
        return TOPIC_LABELS[topic]
    if len(topic) <= 4:
        return topic.upper()
    return topic.replace("-", " ").title()


def top_languages(repos: Iterable[Repo], limit: int = 3) -> str:
    counts: dict[str, int] = {}
    for repo in repos:
        language = (repo.language or "Unknown").strip()
        if not language or language.lower() == "unknown":
            continue
        counts[language] = counts.get(language, 0) + 1
    if not counts:
        return "\u8bed\u8a00\u5206\u5e03\u8f83\u5206\u6563"
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return "\u3001".join(f"{name}({count})" for name, count in top)


def section_meta(section: str) -> dict[str, str]:
    if section == "innovation":
        return {
            "label": "\u6280\u672f\u521b\u65b0",
            "accent": "#0969da",
            "soft": "#ddf4ff",
            "border": "#b6e3ff",
        }
    return {
        "label": "\u6709\u8da3\u9879\u76ee",
        "accent": "#8250df",
        "soft": "#fbefff",
        "border": "#e9d8fd",
    }


def repo_reason(repo: Repo, section: str) -> str:
    topics = [topic_display(t) for t in repo.topics[:5]]
    topic_text = "\u3001".join(topics) if topics else repo.language
    trend_text = f"\uff0cTrending \u5468\u671f\u5185\u65b0\u589e\u7ea6 {repo.period_stars:,} \u661f" if repo.period_stars else ""
    if section == "innovation":
        return f"\u8fd1\u671f\u70ed\u5ea6\u8f83\u9ad8{trend_text}\uff0c\u65b9\u5411\u8986\u76d6 {topic_text}\uff0c\u503c\u5f97\u5173\u6ce8\u5176\u6280\u672f\u5b9e\u73b0\u3001\u843d\u5730\u8def\u5f84\u4e0e\u5e94\u7528\u6f5c\u529b\u3002"
    return f"\u793e\u533a\u8ba8\u8bba\u5ea6\u8f83\u9ad8{trend_text}\uff0c\u4e3b\u9898\u504f {topic_text}\uff0c\u517c\u5177\u7075\u611f\u4ef7\u503c\u3001\u53ef\u73a9\u6027\u6216\u5b9e\u7528\u6027\u3002"


def repo_observations(repo: Repo, section: str) -> list[str]:
    observations: list[str] = []
    if repo.period_stars:
        observations.append(f"Trending \u5468\u671f\u5185\u65b0\u589e\u7ea6 {repo.period_stars:,} Stars\uff0c\u77ed\u671f\u5173\u6ce8\u5ea6\u660e\u663e\u3002")
    if repo.language and repo.language != "Unknown":
        observations.append(f"\u4e3b\u8981\u6280\u672f\u6808\u4e3a {repo.language}\uff0c\u53ef\u4ee5\u5feb\u901f\u5224\u65ad\u662f\u5426\u4fbf\u4e8e\u4e0a\u624b\u6216\u4e8c\u6b21\u5f00\u53d1\u3002")
    if repo.topics:
        labels = "\u3001".join(topic_display(t) for t in repo.topics[:4])
        observations.append(f"\u5173\u952e\u8bcd\u96c6\u4e2d\u5728 {labels}\uff0c\u9879\u76ee\u5b9a\u4f4d\u6bd4\u8f83\u6e05\u6670\u3002")
    if repo.pushed_at or repo.updated_at:
        observations.append(f"\u6700\u8fd1\u66f4\u65b0\u4e3a {format_date(repo.pushed_at or repo.updated_at)}\uff0c\u4f9b\u540e\u7eed\u8bc4\u4f30\u9879\u76ee\u6d3b\u8dc3\u5ea6\u3002")
    if section == "innovation" and len(observations) < 4:
        observations.append("\u5efa\u8bae\u91cd\u70b9\u67e5\u770b README\u3001\u67b6\u6784\u8bf4\u660e\u3001\u793a\u4f8b\u5de5\u7a0b\u548c\u90e8\u7f72\u65b9\u5f0f\u3002")
    if section == "fun" and len(observations) < 4:
        observations.append("\u9002\u5408\u4ece\u4ea4\u4e92\u8bbe\u8ba1\u3001\u521b\u610f\u8868\u8fbe\u6216\u4e2a\u4eba\u6548\u7387\u89d2\u5ea6\u5feb\u901f\u4f53\u9a8c\u3002")
    return observations[:4]


def is_innovation(repo: Repo) -> bool:
    text = f"{repo.full_name} {repo.description} {repo.language}".lower()
    return bool(set(repo.topics) & INNOVATION_KEYWORDS) or any(k in text for k in ("ai", "agent", "llm", "automation", "sandbox", "developer", "workflow", "rust"))


def is_fun(repo: Repo) -> bool:
    text = f"{repo.full_name} {repo.description} {repo.language}".lower()
    return bool(set(repo.topics) & FUN_KEYWORDS) or any(k in text for k in ("game", "fun", "terminal", "cli", "visual", "awesome", "desktop", "ui"))


def select_reports(innovation: list[Repo], fun: list[Repo]) -> tuple[list[Repo], list[Repo]]:
    innovation_pool = sorted(innovation, key=lambda r: (is_innovation(r), r.score), reverse=True)
    innovation_selected = innovation_pool[:5]
    used = {repo.full_name for repo in innovation_selected}

    fun_pool = sorted((repo for repo in fun if repo.full_name not in used), key=lambda r: (is_fun(r), r.score), reverse=True)
    fun_selected = fun_pool[:3]
    used.update(repo.full_name for repo in fun_selected)

    if len(innovation_selected) < 5:
        for repo in innovation + fun:
            if repo.full_name not in used:
                innovation_selected.append(repo)
                used.add(repo.full_name)
            if len(innovation_selected) >= 5:
                break
    if len(fun_selected) < 3:
        for repo in fun + innovation:
            if repo.full_name not in used:
                fun_selected.append(repo)
                used.add(repo.full_name)
            if len(fun_selected) >= 3:
                break
    return innovation_selected[:5], fun_selected[:3]


def plain_repo_block(index: int, repo: Repo, section: str) -> str:
    topics = ", ".join(topic_display(t) for t in repo.topics[:8]) if repo.topics else "\u65e0"
    period = f" | \u672c\u671f\u65b0\u589e Stars: {repo.period_stars:,}" if repo.period_stars else ""
    observations = "\n".join(f"   - {item}" for item in repo_observations(repo, section))
    return textwrap.dedent(
        f"""
        {index}. {repo.full_name}
           GitHub: {repo.html_url}
           \u7b80\u4ecb: {truncate(repo.description)}
           \u8bed\u8a00: {repo.language} | Stars: {repo.stargazers_count:,} | Forks: {repo.forks_count:,}{period}
           \u521b\u5efa: {format_date(repo.created_at)} | \u6700\u8fd1\u66f4\u65b0: {format_date(repo.pushed_at or repo.updated_at)}
           Topics/\u5173\u952e\u8bcd: {topics}
           \u63a8\u8350\u7406\u7531: {repo_reason(repo, section)}
           \u5feb\u901f\u89c2\u5bdf:
        {observations}
        """
    ).strip()


def html_badge(text: str, *, fg: str = "#57606a", bg: str = "#f6f8fa", border: str = "#d0d7de") -> str:
    return (
        f'<span style="display:inline-block;padding:4px 10px;margin:0 8px 8px 0;border-radius:999px;'
        f'font-size:12px;line-height:1.2;color:{fg};background:{bg};border:1px solid {border};">{html.escape(text)}</span>'
    )


def html_repo_block(index: int, repo: Repo, section: str) -> str:
    meta = section_meta(section)
    topics = "".join(html_badge(topic_display(t), fg=meta["accent"], bg=meta["soft"], border=meta["border"]) for t in repo.topics[:6])
    if not topics:
        topics = html_badge("\u6682\u65e0\u5173\u952e\u8bcd")
    stat_badges = "".join(
        [
            html_badge(f"\u8bed\u8a00 {repo.language}"),
            html_badge(f"Stars {repo.stargazers_count:,}"),
            html_badge(f"Forks {repo.forks_count:,}"),
            html_badge(f"\u6700\u8fd1\u66f4\u65b0 {format_date(repo.pushed_at or repo.updated_at)}"),
        ]
    )
    if repo.period_stars:
        stat_badges += html_badge(f"\u672c\u671f\u65b0\u589e {repo.period_stars:,} \u661f", fg="#1a7f37", bg="#dafbe1", border="#aceebb")
    observation_items = "".join(
        f'<li style="margin:0 0 6px;">{html.escape(item)}</li>' for item in repo_observations(repo, section)
    )
    return f"""
    <tr>
      <td style="padding:0 0 16px;">
        <div style="border:1px solid #d8dee4;border-radius:16px;padding:18px 18px 14px;background:#ffffff;box-shadow:0 1px 0 rgba(27,31,36,0.04);">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
            <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
              <span style="display:inline-block;min-width:34px;height:34px;line-height:34px;text-align:center;border-radius:999px;background:{meta['soft']};color:{meta['accent']};font-weight:700;">{index}</span>
              <div>
                <div style="font-size:18px;font-weight:700;line-height:1.35;">
                  <a href="{html.escape(repo.html_url)}" style="color:#0f172a;text-decoration:none;">{html.escape(repo.full_name)}</a>
                </div>
                <div style="margin-top:4px;">{html_badge(meta['label'], fg=meta['accent'], bg=meta['soft'], border=meta['border'])}</div>
              </div>
            </div>
          </div>
          <div style="margin-top:12px;color:#24292f;line-height:1.75;font-size:14px;">{html.escape(truncate(repo.description, 220))}</div>
          <div style="margin-top:14px;">{stat_badges}</div>
          <div style="margin-top:6px;">{topics}</div>
          <div style="margin-top:14px;padding:12px 14px;border-radius:12px;background:#f6f8fa;color:#1f2328;line-height:1.72;">
            <strong>\u63a8\u8350\u7406\u7531\uff1a</strong>{html.escape(repo_reason(repo, section))}
          </div>
          <div style="margin-top:12px;padding:12px 14px;border-radius:12px;background:#fafbfc;border:1px dashed #d0d7de;">
            <div style="font-size:13px;font-weight:700;color:#57606a;margin-bottom:8px;">\u5feb\u901f\u89c2\u5bdf</div>
            <ul style="margin:0;padding-left:18px;color:#24292f;line-height:1.68;">{observation_items}</ul>
          </div>
        </div>
      </td>
    </tr>
    """


def build_report(innovation: list[Repo], fun: list[Repo], report_days: int, source_text: str = "GitHub Trending + GitHub Search API") -> tuple[str, str, str]:
    now_cn = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    today_cn = now_cn.strftime("%Y-%m-%d")
    generated_at = now_cn.strftime("%Y-%m-%d %H:%M")
    subject = f"\u6bcf\u65e5 GitHub \u70ed\u70b9\u9879\u76ee\u7b80\u62a5 - {today_cn}"

    all_repos = innovation + fun
    summary_lines = [
        f"\u751f\u6210\u65f6\u95f4\uff1a{generated_at}\uff08\u5317\u4eac\u65f6\u95f4\uff09",
        f"\u7edf\u8ba1\u7a97\u53e3\uff1a\u6700\u8fd1\u7ea6 {report_days} \u5929",
        f"\u6570\u636e\u6765\u6e90\uff1a{source_text}",
        f"\u8bed\u8a00\u70ed\u70b9\uff1a{top_languages(all_repos)}",
    ]

    plain_parts = [subject, "", "\u4eca\u65e5\u6982\u89c8", *summary_lines, "", "\u4e00\u3001\u6280\u672f\u521b\u65b0\u9879\u76ee Top 5"]
    plain_parts.extend(plain_repo_block(i, repo, "innovation") for i, repo in enumerate(innovation, 1))
    plain_parts.extend(["", "\u4e8c\u3001\u6709\u8da3\u9879\u76ee Top 3"])
    plain_parts.extend(plain_repo_block(i, repo, "fun") for i, repo in enumerate(fun, 1))
    plain_parts.extend(
        [
            "",
            "\u4e09\u3001\u9605\u8bfb\u5efa\u8bae",
            "- \u4eca\u5929\u5165\u9009\u7684\u4ed3\u5e93\u66f4\u504f\u5411\u8fd1\u671f\u6d3b\u8dc3\u66f4\u65b0\u3001\u5728 Trending \u6216\u641c\u7d22\u70ed\u5ea6\u4e0a\u6709\u660e\u663e\u8868\u73b0\u7684\u9879\u76ee\u3002",
            "- \u6280\u672f\u521b\u65b0\u7c7b\u4ed3\u5e93\u9002\u5408\u91cd\u70b9\u67e5\u770b README\u3001\u67b6\u6784\u8bf4\u660e\u3001Demo \u548c\u793e\u533a\u7ef4\u62a4\u60c5\u51b5\u3002",
            "- \u6709\u8da3\u9879\u76ee\u9002\u5408\u5feb\u901f\u4f53\u9a8c\u4ea4\u4e92\u8bbe\u8ba1\u3001\u7ec8\u7aef\u73a9\u6cd5\u3001\u6548\u7387\u5de5\u5177\u6216\u521b\u610f\u8868\u8fbe\u3002",
        ]
    )
    plain_text = "\n\n".join(plain_parts)

    innovation_rows = "".join(html_repo_block(i, repo, "innovation") for i, repo in enumerate(innovation, 1))
    fun_rows = "".join(html_repo_block(i, repo, "fun") for i, repo in enumerate(fun, 1))
    overview_badges = "".join(
        [
            html_badge("5 \u4e2a\u6280\u672f\u521b\u65b0\u9879\u76ee", fg="#0969da", bg="#ddf4ff", border="#b6e3ff"),
            html_badge("3 \u4e2a\u6709\u8da3\u9879\u76ee", fg="#8250df", bg="#fbefff", border="#e9d8fd"),
            html_badge(f"\u6700\u8fd1 {report_days} \u5929", fg="#1f2328", bg="#f6f8fa", border="#d0d7de"),
            html_badge(f"\u8bed\u8a00\u70ed\u70b9\uff1a{top_languages(all_repos)}", fg="#1a7f37", bg="#dafbe1", border="#aceebb"),
        ]
    )
    html_text = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <title>{html.escape(subject)}</title>
    </head>
    <body style="margin:0;padding:0;background:#eef2f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#24292f;">
      <div style="max-width:820px;margin:0 auto;padding:24px 16px 40px;">
        <div style="background:linear-gradient(135deg,#0f172a 0%,#1d4ed8 100%);border-radius:22px;padding:28px 28px 24px;color:#ffffff;box-shadow:0 10px 30px rgba(15,23,42,0.18);">
          <div style="font-size:13px;opacity:0.88;letter-spacing:0.02em;">GitHub Daily Digest</div>
          <h1 style="margin:10px 0 10px;font-size:28px;line-height:1.25;">{html.escape(subject)}</h1>
          <p style="margin:0 0 14px;font-size:15px;line-height:1.8;color:rgba(255,255,255,0.92);">\u4e3a\u4f60\u7b5b\u9009\u8fd1\u671f\u503c\u5f97\u5173\u6ce8\u7684 GitHub \u70ed\u70b9\u4ed3\u5e93\uff0c\u517c\u987e\u6280\u672f\u521b\u65b0\u4ef7\u503c\u4e0e\u6709\u8da3\u53ef\u73a9\u7684\u9879\u76ee\u7075\u611f\uff0c\u65b9\u4fbf\u6bcf\u5929\u7528 3 \u5230 5 \u5206\u949f\u5b8c\u6210\u9ad8\u8d28\u91cf\u6d4f\u89c8\u3002</p>
          <div>{overview_badges}</div>
        </div>

        <div style="background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;margin-top:18px;">
          <h2 style="margin:0 0 14px;font-size:20px;color:#0f172a;">\u4eca\u65e5\u6982\u89c8</h2>
          <div style="padding:12px 14px;background:#f6f8fa;border-radius:12px;color:#1f2328;line-height:1.8;">
            <div><strong>\u751f\u6210\u65f6\u95f4\uff1a</strong>{generated_at}\uff08\u5317\u4eac\u65f6\u95f4\uff09</div>
            <div><strong>\u7edf\u8ba1\u7a97\u53e3\uff1a</strong>\u6700\u8fd1\u7ea6 {report_days} \u5929</div>
            <div><strong>\u6570\u636e\u6765\u6e90\uff1a</strong>{html.escape(source_text)}</div>
            <div><strong>\u8bed\u8a00\u70ed\u70b9\uff1a</strong>{html.escape(top_languages(all_repos))}</div>
          </div>
          <ul style="margin:16px 0 0;padding-left:20px;line-height:1.8;color:#24292f;">
            <li>\u5efa\u8bae\u5148\u770b\u6bcf\u4e2a\u9879\u76ee\u7684\u201c\u63a8\u8350\u7406\u7531\u201d\u548c\u201c\u5feb\u901f\u89c2\u5bdf\u201d\uff0c\u518d\u51b3\u5b9a\u662f\u5426\u8fdb\u5165\u4ed3\u5e93\u6df1\u8bfb README\u3002</li>
            <li>\u5982\u679c\u770b\u5230\u611f\u5174\u8da3\u7684\u9879\u76ee\uff0c\u5efa\u8bae\u987a\u624b\u6536\u85cf\u6216\u8bb0\u5f55\u5230\u4f60\u7684\u77e5\u8bc6\u5e93\uff0c\u65b9\u4fbf\u540e\u7eed\u8ddf\u8fdb\u3002</li>
          </ul>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">\u4e00\u3001\u6280\u672f\u521b\u65b0\u9879\u76ee Top 5</h2>
          <p style="margin:0 0 16px;color:#57606a;line-height:1.8;">\u4f18\u5148\u5173\u6ce8\u8fd1\u671f\u70ed\u5ea6\u9ad8\u3001\u65b9\u5411\u660e\u786e\u3001\u6280\u672f\u5b9e\u73b0\u503c\u5f97\u8ddf\u8fdb\u7684\u4ed3\u5e93\uff0c\u9002\u5408\u7528\u4e8e\u9009\u9898\u3001\u7814\u7a76\u3001\u539f\u578b\u9a8c\u8bc1\u6216\u5de5\u7a0b\u53c2\u8003\u3002</p>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{innovation_rows}</table>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">\u4e8c\u3001\u6709\u8da3\u9879\u76ee Top 3</h2>
          <p style="margin:0 0 16px;color:#57606a;line-height:1.8;">\u8fd9\u4e9b\u9879\u76ee\u66f4\u504f\u521b\u610f\u3001\u6548\u7387\u3001\u7ec8\u7aef\u73a9\u6cd5\u6216\u4ea4\u4e92\u4f53\u9a8c\uff0c\u9002\u5408\u5feb\u901f\u6253\u5f00\u770b\u770b\uff0c\u83b7\u5f97\u7075\u611f\u6216\u76f4\u63a5\u4e0a\u624b\u4f53\u9a8c\u3002</p>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{fun_rows}</table>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:20px;color:#0f172a;">\u4e09\u3001\u9605\u8bfb\u5efa\u8bae</h2>
          <ul style="margin:0;padding-left:20px;line-height:1.9;color:#24292f;">
            <li>\u5148\u770b\u4ed3\u5e93\u9996\u9875\u7684 README\u3001License\u3001\u6700\u8fd1 Commit\u3001Issue/PR \u6d3b\u8dc3\u5ea6\uff0c\u518d\u5224\u65ad\u662f\u5426\u503c\u5f97\u6301\u7eed\u8ddf\u8e2a\u3002</li>
            <li>\u6280\u672f\u521b\u65b0\u7c7b\u4ed3\u5e93\u5efa\u8bae\u91cd\u70b9\u770b Demo\u3001\u67b6\u6784\u8bf4\u660e\u3001\u5b89\u88c5\u95e8\u69db\u3001\u4e8c\u6b21\u5f00\u53d1\u7a7a\u95f4\u4e0e\u793e\u533a\u54cd\u5e94\u901f\u5ea6\u3002</li>
            <li>\u6709\u8da3\u9879\u76ee\u5efa\u8bae\u91cd\u70b9\u5173\u6ce8\u4ea4\u4e92\u4f53\u9a8c\u3001\u9002\u7528\u573a\u666f\u3001\u53ef\u6269\u5c55\u73a9\u6cd5\uff0c\u4ee5\u53ca\u662f\u5426\u9002\u5408\u6536\u85cf\u6216\u8f6c\u53d1\u3002</li>
          </ul>
        </div>
      </div>
    </body>
    </html>
    """
    return subject, plain_text, html_text

def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_email(subject: str, plain_text: str, html_text: str) -> None:
    smtp_host = require_env("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT") or "465")
    smtp_username = require_env("SMTP_USERNAME")
    smtp_password = require_env("SMTP_PASSWORD")
    mail_from = os.getenv("MAIL_FROM", smtp_username).strip() or smtp_username
    mail_to = os.getenv("MAIL_TO", DEFAULT_MAIL_TO).strip() or DEFAULT_MAIL_TO
    use_ssl = env_bool("SMTP_USE_SSL", smtp_port == 465)
    starttls = env_bool("SMTP_STARTTLS", not use_ssl)

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = mail_to
    message.attach(MIMEText(plain_text, "plain", "utf-8"))
    message.attach(MIMEText(html_text, "html", "utf-8"))

    recipients = [addr.strip() for addr in mail_to.split(",") if addr.strip()]
    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
            server.login(smtp_username, smtp_password)
            server.sendmail(mail_from, recipients, message.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if starttls:
                server.starttls(context=context)
            server.login(smtp_username, smtp_password)
            server.sendmail(mail_from, recipients, message.as_string())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a daily GitHub hot-project email report.")
    parser.add_argument("--dry-run", action="store_true", help="Print the generated report instead of sending email.")
    parser.add_argument("--save-html", type=Path, help="Optional path to save the generated HTML report.")
    parser.add_argument("--skip-api", action="store_true", help="Use GitHub Trending only; useful for local tests without GitHub token.")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    report_days = int(os.getenv("REPORT_DAYS", str(DEFAULT_REPORT_DAYS)))
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or None
    skip_api = args.skip_api or env_bool("SKIP_GITHUB_API", False)

    innovation_candidates, fun_candidates = collect_candidates(report_days, token, skip_api=skip_api)
    innovation, fun = select_reports(innovation_candidates, fun_candidates)

    if len(innovation) < 5 or len(fun) < 3:
        print(f"WARN: expected 5 innovation and 3 fun projects, got {len(innovation)} and {len(fun)}.", file=sys.stderr)

    source_text = "GitHub Trending" if skip_api else "GitHub Trending + GitHub Search API"
    subject, plain_text, html_text = build_report(innovation, fun, report_days, source_text)

    if args.save_html:
        args.save_html.parent.mkdir(parents=True, exist_ok=True)
        args.save_html.write_text(html_text, encoding="utf-8")
        print(f"Saved HTML report to {args.save_html}")

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(plain_text)
        return 0

    send_email(subject, plain_text, html_text)
    print(f"Email sent: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
