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
from typing import Any, Iterable

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
KIMI_CHAT_COMPLETIONS_URL = "https://api.moonshot.ai/v1/chat/completions"
DEFAULT_KIMI_PROMPT_CACHE_PREFIX = "github-daily-digest"
DEFAULT_KIMI_MODEL = "kimi-k2.6"
DEFAULT_KIMI_MAX_TOKENS = 4096

INNOVATION_KEYWORDS = {
    "ai", "agent", "agents", "llm", "rag", "automation", "robot", "robotics",
    "developer", "devtool", "sandbox", "workflow", "data", "infra", "rust",
    "typescript", "python", "model", "coding", "assistant", "mcp",
}
FUN_KEYWORDS = {
    "fun", "game", "terminal", "cli", "visual", "visualization", "creative",
    "awesome", "toy", "demo", "music", "desktop", "shell", "retro", "playground",
}
AIGC_TOPICS = {
    "aigc", "comfyui", "stable-diffusion", "diffusers", "sdxl", "flux", "controlnet",
    "lora", "text-to-image", "text-to-video", "image-generation", "video-generation",
    "image-editing", "video-editing", "generative-ai", "image-model", "video-model",
}
AIGC_PHRASES = (
    "comfyui", "stable diffusion", "diffusers", "sdxl", "flux", "controlnet", "lora",
    "text-to-image", "text to image", "image generation", "generate images", "image editing",
    "text-to-video", "text to video", "video generation", "generate videos", "video editing",
    "image model", "video model", "comfyui workflow", "image workflow", "video workflow",
)
FUN_STRICT_PHRASES = (
    "fun", "game", "terminal", "cli", "visual", "retro", "toy", "playground",
    "music", "desktop pet", "ascii", "shell", "pixel", "animation",
)

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


@dataclass(frozen=True)
class RepoInsight:
    full_name: str
    intro_zh: str = ""
    reason_zh: str = ""


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
    keywords = sorted((tokens & INNOVATION_KEYWORDS) | (tokens & FUN_KEYWORDS) | (tokens & AIGC_TOPICS))
    return tuple(keywords)


def repo_text_parts(repo: Repo | dict[str, Any]) -> tuple[str, set[str]]:
    if isinstance(repo, Repo):
        full_name = repo.full_name
        description = repo.description
        language = repo.language
        topics = {topic.lower() for topic in repo.topics}
    else:
        full_name = str(repo.get("full_name") or "")
        description = str(repo.get("description") or "")
        language = str(repo.get("language") or "")
        topics = {str(topic).lower() for topic in (repo.get("topics") or ())}
    text = f"{full_name} {description} {' '.join(sorted(topics))} {language}".lower()
    return text, topics


def count_phrase_hits(text: str, phrases: Iterable[str]) -> int:
    return sum(1 for phrase in phrases if phrase in text)


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

    popularity = math.log10(stars + 1) * 320 + math.log10(forks + 1) * 110 + period_stars * 8
    recency_boost = max(0, 14 - days_since_push) * 65
    new_repo_boost = max(0, 30 - days_since_create) * 28
    trending_boost = 2200 if "trending-daily" in hint else 1400 if "trending-weekly" in hint else 0
    text, topics = repo_text_parts(item)
    innovation_boost = 320 if topics & INNOVATION_KEYWORDS else 0
    fun_boost = 240 if topics & FUN_KEYWORDS else 0
    aigc_boost = 500 if (topics & AIGC_TOPICS or count_phrase_hits(text, AIGC_PHRASES) >= 1) else 0
    return popularity + recency_boost + new_repo_boost + trending_boost + innovation_boost + fun_boost + aigc_boost


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


def collect_candidates(report_days: int, token: str | None, *, skip_api: bool = False) -> tuple[list[Repo], list[Repo], list[Repo]]:
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=report_days)).date().isoformat()
    broader_since = (now - dt.timedelta(days=max(report_days, 14))).date().isoformat()

    trending_items = fetch_trending_repositories()
    innovation_items: list[tuple[dict, str]] = list(trending_items)
    fun_items: list[tuple[dict, str]] = list(trending_items)
    aigc_items: list[tuple[dict, str]] = list(trending_items)

    if not skip_api:
        innovation_queries = [
            f"created:>={since} stars:>=20",
            f"pushed:>={since} stars:>=80 topic:ai",
            f"pushed:>={since} stars:>=80 topic:automation",
            f"pushed:>={since} stars:>=80 topic:developer-tools",
            f"pushed:>={broader_since} stars:>=250 created:>={broader_since}",
        ]
        fun_queries = [
            f"created:>={since} stars:>=12 topic:fun",
            f"created:>={since} stars:>=12 topic:game",
            f"created:>={since} stars:>=12 topic:terminal",
            f"pushed:>={since} stars:>=40 topic:cli",
        ]
        aigc_queries = [
            f"pushed:>={since} stars:>=40 topic:comfyui",
            f"pushed:>={since} stars:>=40 topic:stable-diffusion",
            f"pushed:>={since} stars:>=40 topic:diffusers",
            f"pushed:>={since} stars:>=40 text-to-image",
            f"pushed:>={since} stars:>=40 text-to-video",
            f"pushed:>={since} stars:>=40 image generation workflow",
            f"pushed:>={since} stars:>=40 video generation workflow",
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

        for query in aigc_queries:
            try:
                aigc_items.extend((item, query) for item in search_repositories(query, token, per_page=5))
            except Exception as exc:
                print(f"WARN: AIGC query failed: {query}: {exc}", file=sys.stderr)

    return dedupe_and_rank(innovation_items, now), dedupe_and_rank(fun_items, now), dedupe_and_rank(aigc_items, now)

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


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def infer_repo_purpose(repo: Repo, section: str) -> str:
    text = f"{repo.full_name} {repo.description} {' '.join(repo.topics)} {repo.language}".lower()
    rules = [
        (("penetration testing", "vulnerabilities"), "用于发现和修复应用安全风险的 AI 渗透测试工具"),
        (("meeting assistant", "transcription"), "用于会议转写、说话人区分和内容总结的本地 AI 会议助手"),
        (("job application", "cover letters", "cvs"), "用于求职投递、简历定制和面试准备的 AI 求职自动化框架"),
        (("system prompts", "prompt leaks"), "用于整理和收集主流 AI 产品系统提示词的资料仓库"),
        (("system prompts", "extracted"), "用于整理和收集主流 AI 产品系统提示词的资料仓库"),
        (("design system", "agent ready"), "用于构建可定制界面组件和设计规范的开源设计系统"),
        (("comfyui",), "用于搭建生图、生视频和多模型工作流的可视化 AIGC 编排工具"),
        (("text-to-image",), "用于根据文本生成图片的 AIGC 项目"),
        (("text-to-video",), "用于根据文本生成视频的 AIGC 项目"),
        (("image generation",), "用于生成或编辑图片内容的 AIGC 项目"),
        (("video generation",), "用于生成视频内容的 AIGC 项目"),
        (("gateway", "providers"), "用于统一接入多个 AI 模型提供商的聚合网关"),
        (("multiplexer", "terminal"), "用于在终端里统一调度或切换多个智能体的工具"),
        (("gui agent", "web interfaces"), "用于通过自然语言控制网页界面的页面智能体"),
        (("browser", "automation"), "用于浏览器自动化或网页操作的工具"),
        (("agent",), "用于自动执行任务或辅助操作的智能体项目"),
        (("assistant",), "用于辅助用户完成特定任务的助手型项目"),
        (("design system",), "用于搭建设计系统和复用界面组件的项目"),
        (("sdk",), "用于集成某类能力或服务的软件开发工具包"),
        (("framework",), "用于搭建应用或工作流的开发框架"),
        (("cli", "terminal"), "用于命令行或终端场景的效率工具"),
    ]
    for keywords, purpose in rules:
        if all(keyword in text for keyword in keywords):
            return purpose
    description = truncate(repo.description, 88)
    if description and description != "暂无简介":
        return description
    if section == "innovation":
        return "这是一个近期热度较高、值得进一步查看 README 和示例的技术类项目"
    return "这是一个近期讨论度较高、适合快速体验和获取灵感的开源项目"



def fallback_intro(repo: Repo, section: str) -> str:
    purpose = infer_repo_purpose(repo, section)
    if repo.period_stars:
        return f"{purpose}。它最近上升较快，本期约新增 {repo.period_stars:,} Stars，适合优先了解它的实际用途和使用场景。"
    return f"{purpose}。如果你想快速判断值不值得看，可以先从 README、示例和最近更新入手。"

def repo_intro(repo: Repo, section: str, repo_insights: dict[str, RepoInsight] | None = None) -> str:
    insight = (repo_insights or {}).get(repo.full_name)
    if insight and insight.intro_zh:
        return insight.intro_zh
    return fallback_intro(repo, section)


def repo_reason_text(repo: Repo, section: str, repo_insights: dict[str, RepoInsight] | None = None) -> str:
    insight = (repo_insights or {}).get(repo.full_name)
    if insight and insight.reason_zh:
        return insight.reason_zh
    return repo_reason(repo, section)


def repo_intro_source(repo: Repo, repo_insights: dict[str, RepoInsight] | None = None) -> str:
    insight = (repo_insights or {}).get(repo.full_name)
    return "Kimi 中文解读" if insight and insight.intro_zh else "规则兜底导读"


def intro_source_summary(repos: list[Repo], repo_insights: dict[str, RepoInsight] | None = None) -> str:
    total = len(repos)
    kimi_count = sum(1 for repo in repos if repo_intro_source(repo, repo_insights) == "Kimi 中文解读")
    fallback_count = max(total - kimi_count, 0)
    if total == 0:
        return "本期没有入选项目"
    if kimi_count == 0:
        return f"本期未使用 Kimi，{fallback_count}/{total} 个项目使用规则兜底导读"
    if fallback_count == 0:
        return f"本期已使用 Kimi 优化 {kimi_count}/{total} 个项目的中文理解"
    return f"本期已使用 Kimi 优化 {kimi_count}/{total} 个项目的中文理解，其余 {fallback_count} 个使用规则兜底导读"

def extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model response")
    return text[start : end + 1]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content or "")


def build_kimi_repo_prompt(repos: list[Repo], repo_sections: dict[str, str]) -> str:
    payload = []
    for repo in repos:
        payload.append(
            {
                "full_name": repo.full_name,
                "section": repo_sections.get(repo.full_name, "fun"),
                "html_url": repo.html_url,
                "description": repo.description,
                "language": repo.language,
                "stargazers_count": repo.stargazers_count,
                "forks_count": repo.forks_count,
                "period_stars": repo.period_stars,
                "created_at": format_date(repo.created_at),
                "updated_at": format_date(repo.pushed_at or repo.updated_at),
                "topics": list(repo.topics[:8]),
            }
        )
    input_json = json.dumps({"repos": payload}, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        You are editing a Chinese GitHub email digest. Based only on the provided repository metadata, write concise Simplified Chinese introductions for each repository.

        Requirements:
        1. Return exactly one JSON object with the top-level shape {{"repos": [...]}}.
        2. Each item in repos must contain full_name and intro_zh.
        3. Keep full_name exactly the same as the input and cover every repository.
        4. intro_zh must be 1-2 sentences in Simplified Chinese, about 40-90 Chinese characters.
        5. The FIRST sentence of intro_zh must directly explain what the project does in plain Chinese, such as "用于整理系统提示词的资料仓库" or "用于会议转写和总结的本地助手".
        6. Do NOT start intro_zh with language, tech stack, topics, vague category labels like "一个 AI 项目" or "基于 TypeScript", or generic prefaces like "这个项目是做什么的：".
        7. Write naturally in Chinese and introduce the project directly; avoid mechanical lead-ins.
        8. Do not add reason_zh, recommendation fields, or any extra keys.
        9. Do not invent README details, benchmarks, customer stories, author background, or unsupported technical claims.
        10. No Markdown. No extra explanation. JSON object only.

        Example output:
        {{
          "repos": [
            {{
              "full_name": "owner/repo",
              "intro_zh": "用于统一接入多个模型提供商，帮助开发者用一个接口切换不同 AI 服务。近期热度上升较快，适合先看它的接入方式和使用门槛。"
            }}
          ]
        }}

        Input JSON:
        {input_json}
        """
    ).strip()

def generate_repo_insights(repos: list[Repo], repo_sections: dict[str, str]) -> tuple[dict[str, RepoInsight], str]:
    api_key = os.getenv("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        print("INFO: MOONSHOT_API_KEY not set; using fallback Chinese copy.", file=sys.stderr)
        return {}, "Kimi \u672a\u914d\u7f6e API Key\uff08\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"
    model = os.getenv("KIMI_MODEL", DEFAULT_KIMI_MODEL).strip() or DEFAULT_KIMI_MODEL
    max_tokens = int(os.getenv("KIMI_MAX_TOKENS") or str(DEFAULT_KIMI_MAX_TOKENS))
    prompt = build_kimi_repo_prompt(repos, repo_sections)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise Chinese tech newsletter editor. Only use the provided repository metadata. The first sentence must explain what the project does in plain Chinese, not its tech stack. Return one valid JSON object and nothing else.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        os.getenv("KIMI_CHAT_COMPLETIONS_URL", KIMI_CHAT_COMPLETIONS_URL).strip() or KIMI_CHAT_COMPLETIONS_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else str(exc)
        details = normalize_text(details)[:240]
        print(f"WARN: Kimi request failed ({exc.code}): {details}", file=sys.stderr)
        return {}, f"Kimi \u8c03\u7528\u5931\u8d25\uff08HTTP {exc.code}\uff0c\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"
    except Exception as exc:
        print(f"WARN: Kimi request failed: {exc}", file=sys.stderr)
        return {}, f"Kimi \u8c03\u7528\u5931\u8d25\uff08{truncate(str(exc), 60)}\uff0c\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"

    choices = payload.get("choices") or []
    if not choices:
        print("WARN: Kimi returned no choices; using fallback Chinese copy.", file=sys.stderr)
        return {}, "Kimi \u8fd4\u56de\u7a7a\u7ed3\u679c\uff08\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"

    raw_content = message_text((choices[0].get("message") or {}).get("content"))
    if not raw_content:
        print("WARN: Kimi returned empty content; using fallback Chinese copy.", file=sys.stderr)
        return {}, "Kimi \u8fd4\u56de\u7a7a\u5185\u5bb9\uff08\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"

    try:
        parsed = json.loads(extract_json_object(raw_content))
    except Exception as exc:
        print(f"WARN: Kimi JSON parse failed: {exc}", file=sys.stderr)
        return {}, "Kimi \u8fd4\u56de\u5185\u5bb9\u65e0\u6cd5\u89e3\u6790\uff08\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"

    expected = {repo.full_name for repo in repos}
    insights: dict[str, RepoInsight] = {}
    for item in parsed.get("repos") or []:
        if not isinstance(item, dict):
            continue
        full_name = normalize_text(str(item.get("full_name") or ""))
        if full_name not in expected:
            continue
        intro_zh = normalize_text(str(item.get("intro_zh") or ""))
        reason_zh = normalize_text(str(item.get("reason_zh") or ""))
        if not intro_zh and not reason_zh:
            continue
        insights[full_name] = RepoInsight(
            full_name=full_name,
            intro_zh=truncate(intro_zh, 120),
            reason_zh=truncate(reason_zh, 90),
        )

    if not insights:
        print("WARN: Kimi returned no usable repo insights; using fallback Chinese copy.", file=sys.stderr)
        return {}, "Kimi \u672a\u8fd4\u56de\u53ef\u7528\u7ed3\u679c\uff08\u5df2\u56de\u9000\u89c4\u5219\u5bfc\u8bfb\uff09"

    print(f"INFO: Kimi generated Chinese insights for {len(insights)}/{len(repos)} repos.", file=sys.stderr)
    return insights, f"Kimi\uff08{model}\uff09"


def section_meta(section: str) -> dict[str, str]:
    if section == "innovation":
        return {
            "label": "热点项目",
            "accent": "#0969da",
            "soft": "#ddf4ff",
            "border": "#b6e3ff",
        }
    if section == "aigc":
        return {
            "label": "AIGC 项目",
            "accent": "#bc4c00",
            "soft": "#fff1e5",
            "border": "#ffd8b5",
        }
    return {
        "label": "有趣项目",
        "accent": "#8250df",
        "soft": "#fbefff",
        "border": "#e9d8fd",
    }

def purpose_focus(repo: Repo, section: str) -> str:
    purpose = infer_repo_purpose(repo, section)
    match = re.match(r"^用于(.+?)的(?:项目|工具|框架|资料仓库|系统|助手|SDK|开发工具包)$", purpose)
    if match:
        return match.group(1)
    return purpose



def repo_reason(repo: Repo, section: str) -> str:
    focus = purpose_focus(repo, section)
    trend_text = f"近 7 天内新增约 {repo.period_stars:,} Stars，" if repo.period_stars else ""
    if section == "innovation":
        return f"{trend_text}说明「{focus}」这类需求关注度正在上升，值得查看它解决问题的方式和落地效果。"
    return f"{trend_text}它在「{focus}」方向讨论度较高，适合快速体验它的用法、交互方式或实用价值。"



def repo_observations(repo: Repo, section: str) -> list[str]:
    observations: list[str] = []
    if repo.period_stars:
        observations.append(f"Trending 周期内新增约 {repo.period_stars:,} Stars，短期关注度明显。")
    focus = purpose_focus(repo, section)
    observations.append(f"核心场景是「{focus}」，可以先对照 README 和 Demo 看它是否符合你的使用需求。")
    if repo.topics:
        labels = "、".join(topic_display(t) for t in repo.topics[:4])
        observations.append(f"关键词包括 {labels}，能帮你快速判断它更偏向哪类人群或场景。")
    if repo.language and repo.language != "Unknown":
        observations.append(f"主要使用 {repo.language}，如果你打算试用或二次开发，可以提前评估上手成本。")
    if repo.pushed_at or repo.updated_at:
        observations.append(f"最近更新为 {format_date(repo.pushed_at or repo.updated_at)}，可作为判断项目活跃度的参考。")
    if section == "innovation" and len(observations) < 4:
        observations.append("建议重点查看 README、架构说明、示例工程和部署方式。")
    if section == "fun" and len(observations) < 4:
        observations.append("适合从交互设计、创意表达或个人效率角度快速体验。")
    return observations[:4]


def is_innovation(repo: Repo) -> bool:
    text, topics = repo_text_parts(repo)
    innovation_hits = count_phrase_hits(text, ("ai", "agent", "llm", "automation", "sandbox", "developer", "workflow", "rust"))
    return bool(topics & INNOVATION_KEYWORDS) or innovation_hits >= 1


def is_fun(repo: Repo) -> bool:
    text, topics = repo_text_parts(repo)
    if is_aigc(repo):
        return False
    fun_hits = count_phrase_hits(text, FUN_STRICT_PHRASES)
    return bool(topics & FUN_KEYWORDS) or fun_hits >= 1


def is_aigc(repo: Repo) -> bool:
    text, topics = repo_text_parts(repo)
    topic_hits = len(topics & AIGC_TOPICS)
    phrase_hits = count_phrase_hits(text, AIGC_PHRASES)
    return topic_hits >= 1 or phrase_hits >= 2 or ("comfyui" in text and phrase_hits >= 1)


def hot_rank_score(repo: Repo) -> float:
    trending_bonus = 2400 if repo.section_hint == "trending-daily" else 1500 if repo.section_hint == "trending-weekly" else 0
    momentum_bonus = repo.period_stars * 14
    fresh_bonus = 600 if repo.period_stars >= 80 else 300 if repo.period_stars >= 40 else 0
    aigc_penalty = 900 if is_aigc(repo) else 0
    return repo.score + trending_bonus + momentum_bonus + fresh_bonus - aigc_penalty


def fun_rank_score(repo: Repo) -> float:
    text, topics = repo_text_parts(repo)
    phrase_bonus = count_phrase_hits(text, FUN_STRICT_PHRASES) * 260
    topic_bonus = len(topics & FUN_KEYWORDS) * 180
    serious_tool_penalty = 420 if ("infra" in text or "rag" in text or "automation" in text) else 0
    return repo.score + phrase_bonus + topic_bonus - serious_tool_penalty


def aigc_rank_score(repo: Repo) -> float:
    text, topics = repo_text_parts(repo)
    phrase_bonus = count_phrase_hits(text, AIGC_PHRASES) * 320
    topic_bonus = len(topics & AIGC_TOPICS) * 220
    workflow_bonus = 420 if ("workflow" in text or "comfyui" in text) else 0
    return repo.score + phrase_bonus + topic_bonus + workflow_bonus


def select_reports(innovation: list[Repo], fun: list[Repo], aigc: list[Repo]) -> tuple[list[Repo], list[Repo], list[Repo]]:
    def merge_ranked(*groups: list[Repo]) -> list[Repo]:
        best: dict[str, Repo] = {}
        for group in groups:
            for repo in group:
                existing = best.get(repo.full_name)
                if existing is None or repo.score > existing.score:
                    best[repo.full_name] = repo
        return sorted(best.values(), key=lambda r: r.score, reverse=True)

    def pick(pool: list[Repo], count: int, used: set[str], fallbacks: list[list[Repo]], predicate=None) -> list[Repo]:
        selected: list[Repo] = []
        for source in [pool, *fallbacks]:
            for repo in source:
                if repo.full_name in used:
                    continue
                if predicate and not predicate(repo):
                    continue
                selected.append(repo)
                used.add(repo.full_name)
                if len(selected) >= count:
                    return selected
        return selected

    combined_pool = merge_ranked(innovation, fun, aigc)
    innovation_pool = sorted(innovation, key=lambda r: (not is_aigc(r), hot_rank_score(r), is_innovation(r)), reverse=True)
    fun_pool = sorted(fun, key=lambda r: (is_fun(r), fun_rank_score(r)), reverse=True)
    aigc_pool = sorted(aigc, key=lambda r: (is_aigc(r), aigc_rank_score(r)), reverse=True)

    used: set[str] = set()
    aigc_selected = pick(aigc_pool, 2, used, [combined_pool], predicate=is_aigc)
    fun_selected = pick(fun_pool, 2, used, [combined_pool], predicate=is_fun)
    innovation_selected = pick(
        innovation_pool,
        5,
        used,
        [combined_pool],
        predicate=lambda repo: is_innovation(repo) and not is_aigc(repo),
    )

    if len(innovation_selected) < 5:
        innovation_selected = innovation_selected + pick(
            combined_pool,
            5 - len(innovation_selected),
            used,
            [],
            predicate=lambda repo: not is_aigc(repo),
        )

    if len(fun_selected) < 2:
        fun_selected = fun_selected + pick(combined_pool, 2 - len(fun_selected), used, [], predicate=lambda repo: not is_aigc(repo))

    if len(aigc_selected) < 2:
        aigc_selected = aigc_selected + pick(combined_pool, 2 - len(aigc_selected), used, [], predicate=is_aigc)

    return innovation_selected[:5], fun_selected[:2], aigc_selected[:2]


def plain_repo_block(index: int, repo: Repo, section: str, repo_insights: dict[str, RepoInsight] | None = None) -> str:
    intro_text = repo_intro(repo, section, repo_insights)
    return textwrap.dedent(
        f"""
        {index}. {repo.full_name}
           GitHub: {repo.html_url}
           中文简介: {intro_text}
        """
    ).strip()

def html_badge(text: str, *, fg: str = "#57606a", bg: str = "#f6f8fa", border: str = "#d0d7de") -> str:
    return (
        f'<span style="display:inline-block;padding:4px 10px;margin:0 8px 8px 0;border-radius:999px;'
        f'font-size:12px;line-height:1.2;color:{fg};background:{bg};border:1px solid {border};">{html.escape(text)}</span>'
    )


def html_repo_block(index: int, repo: Repo, section: str, repo_insights: dict[str, RepoInsight] | None = None) -> str:
    meta = section_meta(section)
    intro_text = repo_intro(repo, section, repo_insights)
    return f"""
    <tr>
      <td style="padding:0 0 16px;">
        <div style="border:1px solid #d8dee4;border-radius:16px;padding:18px;background:#ffffff;box-shadow:0 1px 0 rgba(27,31,36,0.04);">
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
            <div>{html_badge(f'Stars {repo.stargazers_count:,}')}</div>
          </div>
          <div style="margin-top:12px;padding:14px 16px;border-radius:14px;background:#f8fbff;border:1px solid #dbeafe;color:#0f172a;line-height:1.82;font-size:14px;">
            <div style="font-size:13px;font-weight:700;color:#0550ae;margin-bottom:6px;">中文简介</div>
            <div>{html.escape(intro_text)}</div>
          </div>
        </div>
      </td>
    </tr>
    """

def build_report(
    innovation: list[Repo],
    fun: list[Repo],
    aigc: list[Repo],
    report_days: int,
    source_text: str = "GitHub Trending + GitHub Search API",
    repo_insights: dict[str, RepoInsight] | None = None,
    ai_provider: str = "",
) -> tuple[str, str, str]:
    now_cn = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    today_cn = now_cn.strftime("%Y-%m-%d")
    generated_at = now_cn.strftime("%Y-%m-%d %H:%M")
    subject = f"每日 GitHub 9 项目简报 - {today_cn}"

    all_repos = innovation + fun + aigc
    ai_text = ai_provider or "未启用 AI（使用规则兜底导读）"
    intro_source_text = intro_source_summary(all_repos, repo_insights)
    summary_lines = [
        f"生成时间：{generated_at}（北京时间）",
        f"统计窗口：最近约 {report_days} 天",
        f"数据来源：{source_text}",
        f"AI 中文导读：{ai_text}",
        f"项目理解方式：{intro_source_text}",
        f"项目结构：热点 5 个 + 有趣 2 个 + AIGC 2 个",
        f"语言热点：{top_languages(all_repos)}",
    ]

    summary_repos = [("热点", repo, "innovation") for repo in innovation]
    summary_repos += [("有趣", repo, "fun") for repo in fun]
    summary_repos += [("AIGC", repo, "aigc") for repo in aigc]

    plain_parts = [subject, "", "今日概览", *summary_lines, "", "零、今日 9 个项目清单"]
    for index, (label, repo, section) in enumerate(summary_repos, 1):
        plain_parts.append(f"{index}. [{label}] {repo.full_name}")
        plain_parts.append(f"   中文简介: {repo_intro(repo, section, repo_insights)}")
    plain_parts.extend(["", "一、五个热点项目"])
    plain_parts.extend(plain_repo_block(i, repo, "innovation", repo_insights) for i, repo in enumerate(innovation, 1))
    plain_parts.extend(["", "二、两个有趣的项目"])
    plain_parts.extend(plain_repo_block(i, repo, "fun", repo_insights) for i, repo in enumerate(fun, 1))
    plain_parts.extend(["", "三、两个 AIGC 项目（生图 / 生视频 / AIGC）"])
    plain_parts.extend(plain_repo_block(i, repo, "aigc", repo_insights) for i, repo in enumerate(aigc, 1))
    plain_text = "\n".join(plain_parts)

    innovation_rows = "".join(html_repo_block(i, repo, "innovation", repo_insights) for i, repo in enumerate(innovation, 1))
    fun_rows = "".join(html_repo_block(i, repo, "fun", repo_insights) for i, repo in enumerate(fun, 1))
    aigc_rows = "".join(html_repo_block(i, repo, "aigc", repo_insights) for i, repo in enumerate(aigc, 1))

    summary_items = "".join(
        f"""
        <tr>
          <td style="padding:0 0 12px;">
            <div style="border:1px solid #d8dee4;border-radius:14px;padding:14px 16px;background:#ffffff;">
              <div style="font-size:15px;font-weight:700;color:#0f172a;">{idx}. [{html.escape(label)}] <a href="{html.escape(repo.html_url)}" style="color:#0f172a;text-decoration:none;">{html.escape(repo.full_name)}</a></div>
              <div style="margin-top:8px;color:#334155;line-height:1.8;">{html.escape(repo_intro(repo, section, repo_insights))}</div>
            </div>
          </td>
        </tr>
        """
        for idx, (label, repo, section) in enumerate(summary_repos, 1)
    )

    overview_badges = "".join(
        [
            html_badge(f"近 {report_days} 天", fg="#ffffff", bg="rgba(255,255,255,0.14)", border="rgba(255,255,255,0.22)"),
            html_badge("9 个项目", fg="#ffffff", bg="rgba(255,255,255,0.14)", border="rgba(255,255,255,0.22)"),
            html_badge("热点 5 + 有趣 2 + AIGC 2", fg="#ffffff", bg="rgba(255,255,255,0.14)", border="rgba(255,255,255,0.22)"),
            html_badge(f"语言热点：{top_languages(all_repos)}", fg="#1a7f37", bg="#dafbe1", border="#aceebb"),
            html_badge(f"AI 导读：{ai_provider}" if ai_provider else "AI 导读：未启用", fg="#0550ae", bg="#ddf4ff", border="#b6e3ff"),
            html_badge(intro_source_text, fg="#7c2d12", bg="#ffedd5", border="#fdba74"),
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
          <p style="margin:0 0 14px;font-size:15px;line-height:1.8;color:rgba(255,255,255,0.92);">今天为你整理 9 个值得浏览的 GitHub 项目：5 个热点项目、2 个有趣项目、2 个 AIGC 项目。顶部先看清单，再按分类查看详情。</p>
          <div>{overview_badges}</div>
        </div>

        <div style="background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;margin-top:18px;">
          <h2 style="margin:0 0 14px;font-size:20px;color:#0f172a;">今日概览</h2>
          <div style="padding:12px 14px;background:#f6f8fa;border-radius:12px;color:#1f2328;line-height:1.8;">
            <div><strong>生成时间：</strong>{generated_at}（北京时间）</div>
            <div><strong>统计窗口：</strong>最近约 {report_days} 天</div>
            <div><strong>数据来源：</strong>{html.escape(source_text)}</div>
            <div><strong>AI 中文导读：</strong>{html.escape(ai_text)}</div>
            <div><strong>项目理解方式：</strong>{html.escape(intro_source_text)}</div>
            <div><strong>项目结构：</strong>热点 5 个 + 有趣 2 个 + AIGC 2 个</div>
            <div><strong>语言热点：</strong>{html.escape(top_languages(all_repos))}</div>
          </div>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">零、今日 9 个项目清单</h2>
          <p style="margin:0 0 16px;color:#57606a;line-height:1.8;">先快速看项目名称和中文简介，确认哪些最值得点进去。</p>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{summary_items}</table>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">一、五个热点项目</h2>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{innovation_rows}</table>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">二、两个有趣的项目</h2>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{fun_rows}</table>
        </div>

        <div style="margin-top:18px;background:#ffffff;border:1px solid #d8dee4;border-radius:18px;padding:22px 24px;">
          <h2 style="margin:0 0 14px;font-size:22px;color:#0f172a;">三、两个 AIGC 项目（生图 / 生视频 / AIGC）</h2>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{aigc_rows}</table>
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

    innovation_candidates, fun_candidates, aigc_candidates = collect_candidates(report_days, token, skip_api=skip_api)
    innovation, fun, aigc = select_reports(innovation_candidates, fun_candidates, aigc_candidates)

    if len(innovation) < 5 or len(fun) < 2 or len(aigc) < 2:
        print(f"WARN: expected 5 hot, 2 fun, 2 AIGC projects, got {len(innovation)}, {len(fun)}, {len(aigc)}.", file=sys.stderr)

    source_text = "GitHub Trending" if skip_api else "GitHub Trending + GitHub Search API"
    repo_sections = {repo.full_name: "innovation" for repo in innovation}
    repo_sections.update({repo.full_name: "fun" for repo in fun})
    repo_sections.update({repo.full_name: "aigc" for repo in aigc})
    repo_insights, ai_provider = generate_repo_insights(innovation + fun + aigc, repo_sections)
    subject, plain_text, html_text = build_report(innovation, fun, aigc, report_days, source_text, repo_insights, ai_provider)

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
