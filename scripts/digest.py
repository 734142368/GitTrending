#!/usr/bin/env python3
"""OpenGithubs 开源趋势自动抓取与 AI 中文汇总工具。"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT_DIR / "cache" / "projects.json"
REPORTS_DIR = ROOT_DIR / "reports"

MODE_CONFIG = {
    "daily": {
        "repo": "OpenGithubs/github-daily-rank",
        "label": "日榜",
        "emoji": "📅",
    },
    "weekly": {
        "repo": "OpenGithubs/github-weekly-rank",
        "label": "周榜",
        "emoji": "📊",
    },
    "monthly": {
        "repo": "OpenGithubs/github-monthly-rank",
        "label": "月榜",
        "emoji": "🏆",
    },
}

USER_AGENT = "OpenGithubs-CN-Digest/1.0"


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 30,
) -> str:
    """发送 HTTP 请求并返回 UTF-8 文本。"""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    """构建 GitHub API 请求头，优先使用 GITHUB_TOKEN。"""
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_rank_markdown(mode: str) -> str:
    """拉取 OpenGithubs 对应榜单的 README.md。"""
    repo = MODE_CONFIG[mode]["repo"]
    url = f"https://raw.githubusercontent.com/{repo}/main/README.md"
    return http_request(url, timeout=20)


def parse_rank_markdown(md_text: str) -> tuple[str, list[dict[str, str]]]:
    """解析榜单日期与项目列表。"""
    date_match = re.search(r"##\s*(\d{4})\.(\d{2})\.(\d{2})", md_text)
    if date_match:
        report_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
    else:
        report_date = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")

    row_pattern = re.compile(
        r"^\|\s*(\d+)\s*\|\s*\[([^\]]+)\]\((https://github\.com/[^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
    )
    items: list[dict[str, str]] = []
    for line in md_text.splitlines():
        m = row_pattern.match(line.strip())
        if not m:
            continue
        items.append(
            {
                "rank": m.group(1).strip(),
                "full_name": m.group(2).strip(),
                "url": m.group(3).strip(),
                "stars": m.group(4).strip(),
                "delta": m.group(5).strip().replace("🔺", "").strip(),
            }
        )
    return report_date, items


def fetch_repo_context(full_name: str) -> dict[str, Any]:
    """获取单个 GitHub 仓库的元信息与 README 片段。"""
    meta: dict[str, Any] = {
        "description": "",
        "language": "",
        "topics": [],
        "homepage": "",
        "license": "",
        "readme_excerpt": "",
    }
    try:
        repo_raw = http_request(
            f"https://api.github.com/repos/{full_name}",
            headers=github_headers(),
            timeout=20,
        )
        repo_json = json.loads(repo_raw)
        meta["description"] = repo_json.get("description") or ""
        meta["language"] = repo_json.get("language") or "未知"
        meta["topics"] = repo_json.get("topics") or []
        meta["homepage"] = repo_json.get("homepage") or ""
        lic = repo_json.get("license") or {}
        meta["license"] = lic.get("spdx_id") or lic.get("name") or ""
    except Exception as exc:
        meta["meta_error"] = str(exc)

    try:
        readme_text = http_request(
            f"https://api.github.com/repos/{full_name}/readme",
            headers=github_headers(accept="application/vnd.github.raw+json"),
            timeout=20,
        )
        # 清理 HTML 标签与多余空行，保留前 4500 字符供模型理解核心功能
        cleaned = re.sub(r"<[^>]+>", " ", readme_text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        meta["readme_excerpt"] = cleaned[:4500]
    except Exception as exc:
        meta["readme_error"] = str(exc)

    return meta


def _parse_summary_json(content: str) -> dict[str, Any]:
    """解析并规范化模型返回的 JSON 摘要。"""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    return {
        "one_liner": str(parsed.get("one_liner", "")).strip(),
        "features": [str(x).strip() for x in parsed.get("features", []) if str(x).strip()][:5],
        "use_cases": str(parsed.get("use_cases", "")).strip(),
        "tags": [str(x).strip() for x in parsed.get("tags", []) if str(x).strip()][:4],
    }


def summarize_with_gemini(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """调用 Google Gemini 原生 REST API 生成结构化中文摘要。"""
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp_text = http_request(url, method="POST", headers=headers, data=body, timeout=60)
            resp_json = json.loads(resp_text)
            content = resp_json["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_summary_json(content)
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (429, 503) and attempt < 3:
                time.sleep(5 * attempt)
                continue
            raise
    raise RuntimeError(f"Gemini 请求失败: {last_err}")


def summarize_with_llm(full_name: str, context: dict[str, Any]) -> dict[str, Any]:
    """优先使用 Gemini API，其次兼容 OpenAI 接口生成结构化中文摘要。"""
    system_prompt = (
        "你是一位资深开源技术编辑。请根据提供的 GitHub 项目信息与 README 内容，"
        "用简洁、准确、通俗的中文输出项目解析。必须严格返回合法 JSON，不要包含 Markdown 代码块外多余文字。"
        "JSON 字段定义如下：\n"
        "{\n"
        '  "one_liner": "一句话定位（20-45字，直白说明它是什么、解决什么问题）",\n'
        '  "features": ["核心功能或亮点1", "核心功能或亮点2", "核心功能或亮点3"],\n'
        '  "use_cases": "适用人群与典型使用场景（1-2句话）",\n'
        '  "tags": ["领域标签1", "领域标签2"]\n'
        "}"
    )

    user_prompt = (
        f"项目名称: {full_name}\n"
        f"主要语言: {context.get('language', '未知')}\n"
        f"Topics: {', '.join(context.get('topics', []))}\n"
        f"英文简介: {context.get('description', '')}\n\n"
        f"README 节选:\n{context.get('readme_excerpt', '（无 README 内容）')}"
    )

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        return summarize_with_gemini(gemini_key, system_prompt, user_prompt)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 GEMINI_API_KEY 或 OPENAI_API_KEY 环境变量")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp_text = http_request(
        f"{base_url}/chat/completions",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8"),
        timeout=60,
    )
    resp_json = json.loads(resp_text)
    content = resp_json["choices"][0]["message"]["content"]
    return _parse_summary_json(content)


def load_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_cache_fresh(entry: dict[str, Any], max_age_days: int = 30) -> bool:
    updated_at = entry.get("updated_at")
    if not updated_at:
        return False
    try:
        ts = dt.date.fromisoformat(updated_at)
        return (dt.date.today() - ts).days <= max_age_days
    except ValueError:
        return False


def process_project(
    item: dict[str, str],
    cache: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    """处理单个项目，返回 (合并后的项目数据, 是否命中缓存)。"""
    full_name = item["full_name"]
    cached = cache.get(full_name)
    if cached and is_cache_fresh(cached) and cached.get("summary", {}).get("one_liner"):
        return {**item, **cached, "repeat_hit": True}, True

    context = fetch_repo_context(full_name)
    if dry_run:
        summary = {
            "one_liner": f"[Dry-Run] {context.get('description') or '暂无英文简介'}",
            "features": ["Dry-Run 模式未调用大模型", f"README 抓取长度: {len(context.get('readme_excerpt', ''))} 字符"],
            "use_cases": "用于本地验证抓取与解析流程。",
            "tags": [context.get("language") or "OpenSource"],
        }
    else:
        try:
            summary = summarize_with_llm(full_name, context)
        except Exception as exc:
            summary = {
                "one_liner": f"（AI 总结失败，原文：{context.get('description') or '暂无'}）",
                "features": [f"错误信息: {exc}"],
                "use_cases": "待重试",
                "tags": [context.get("language") or "未知"],
            }

    record = {
        "language": context.get("language") or "未知",
        "license": context.get("license") or "",
        "homepage": context.get("homepage") or "",
        "description": context.get("description") or "",
        "summary": summary,
        "updated_at": dt.date.today().isoformat(),
    }
    return {**item, **record, "repeat_hit": False}, False


def render_markdown(mode: str, report_date: str, projects: list[dict[str, Any]]) -> str:
    """渲染中文汇总 Markdown 报告。"""
    cfg = MODE_CONFIG[mode]
    lines: list[str] = [
        f"# {cfg['emoji']} OpenGithubs {cfg['label']}中文深度汇总（{report_date}）",
        "",
        f"> 数据来源：[OpenGithubs/{cfg['repo'].split('/')[1]}](https://github.com/{cfg['repo']}) ｜ 共收录 **{len(projects)}** 个热门项目",
        "",
        "## 📋 本期速览",
        "",
        "| 排名 | 项目 | 语言 | 总 Star | 增长 | 一句话定位 |",
        "| :---: | :--- | :---: | :---: | :---: | :--- |",
    ]

    for p in projects:
        one_liner = p["summary"]["one_liner"].replace("|", "\\|")
        repeat_badge = " 🔁" if p.get("repeat_hit") else ""
        lines.append(
            f"| {p['rank']} | [{p['full_name']}]({p['url']}){repeat_badge} | {p['language']} | {p['stars']} | 🔺{p['delta']} | {one_liner} |"
        )

    lines.extend(["", "---", "", "## 🔍 项目功能详解", ""])

    for p in projects:
        s = p["summary"]
        tags_str = " ".join(f"`{t}`" for t in s.get("tags", []))
        repeat_note = " · 🔁 连续/近期上榜" if p.get("repeat_hit") else ""
        lines.append(f"### {p['rank']}. [{p['full_name']}]({p['url']})")
        lines.append("")
        lines.append(
            f"- **热度**：⭐ {p['stars']}（本期新增 🔺{p['delta']}）｜ **语言**：`{p['language']}`{repeat_note}"
        )
        if tags_str:
            lines.append(f"- **标签**：{tags_str}")
        if p.get("homepage"):
            lines.append(f"- **主页**：{p['homepage']}")
        lines.append(f"- **一句话定位**：{s['one_liner']}")
        if s.get("features"):
            lines.append("- **核心功能**：")
            for feat in s["features"]:
                lines.append(f"  - {feat}")
        if s.get("use_cases"):
            lines.append(f"- **适用场景**：{s['use_cases']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def update_root_readme() -> None:
    """更新根目录 README.md 的最新报告导航链接。"""
    readme_path = ROOT_DIR / "README.md"
    if not readme_path.exists():
        return
    text = readme_path.read_text(encoding="utf-8")
    start_marker = "<!-- DIGEST_LINKS_START -->"
    end_marker = "<!-- DIGEST_LINKS_END -->"
    if start_marker not in text or end_marker not in text:
        return

    nav_lines = [start_marker, ""]
    for mode, cfg in MODE_CONFIG.items():
        latest_file = REPORTS_DIR / mode / "LATEST.md"
        if latest_file.exists():
            first_line = latest_file.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
            nav_lines.append(f"- {cfg['emoji']} **{cfg['label']}**：[{first_line}](reports/{mode}/LATEST.md)")
        else:
            nav_lines.append(f"- {cfg['emoji']} **{cfg['label']}**：暂未生成")
    nav_lines.extend(["", end_marker])

    new_block = "\n".join(nav_lines)
    updated = re.sub(
        f"{re.escape(start_marker)}.*?{re.escape(end_marker)}",
        new_block,
        text,
        flags=re.DOTALL,
    )
    readme_path.write_text(updated, encoding="utf-8")


def send_webhook(mode: str, report_date: str, projects: list[dict[str, Any]]) -> None:
    """可选：推送简报到飞书 / 钉钉 / 企业微信 / 通用 Webhook。"""
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    cfg = MODE_CONFIG[mode]
    top_lines = [
        f"{p['rank']}. {p['full_name']} (🔺{p['delta']}) - {p['summary']['one_liner']}"
        for p in projects[:5]
    ]
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")
    more_link = (
        f"\n\n完整详解：https://github.com/{repo_slug}/blob/main/reports/{mode}/LATEST.md"
        if repo_slug
        else ""
    )
    body_text = (
        f"{cfg['emoji']} OpenGithubs {cfg['label']}中文汇总（{report_date}）\n\n"
        + "\n".join(top_lines)
        + more_link
    )

    if "feishu.cn" in webhook_url or "larksuite.com" in webhook_url:
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": body_text}}
    elif "oapi.dingtalk.com" in webhook_url:
        payload = {"msgtype": "text", "text": {"content": body_text}}
    elif "qyapi.weixin.qq.com" in webhook_url:
        payload = {"msgtype": "text", "text": {"content": body_text}}
    else:
        payload = {"title": f"OpenGithubs {cfg['label']} ({report_date})", "text": body_text}

    try:
        http_request(
            webhook_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
        print(f"[webhook] 推送成功 ({mode})")
    except Exception as exc:
        print(f"[webhook] 推送失败: {exc}", file=sys.stderr)


def render_email_html(mode: str, report_date: str, projects: list[dict[str, Any]]) -> str:
    """将汇总内容渲染为适合邮箱阅读的 HTML。"""
    cfg = MODE_CONFIG[mode]
    rows_html = []
    details_html = []

    for p in projects:
        s = p["summary"]
        repeat_badge = " 🔁" if p.get("repeat_hit") else ""
        rows_html.append(
            f"<tr>"
            f"<td style='padding:6px 8px;border:1px solid #e5e7eb;text-align:center;'>{html.escape(p['rank'])}</td>"
            f"<td style='padding:6px 8px;border:1px solid #e5e7eb;'><a href='{html.escape(p['url'])}' style='color:#2563eb;text-decoration:none;'>{html.escape(p['full_name'])}</a>{repeat_badge}</td>"
            f"<td style='padding:6px 8px;border:1px solid #e5e7eb;text-align:center;'>{html.escape(p['language'])}</td>"
            f"<td style='padding:6px 8px;border:1px solid #e5e7eb;text-align:center;'>{html.escape(p['stars'])} / 🔺{html.escape(p['delta'])}</td>"
            f"<td style='padding:6px 8px;border:1px solid #e5e7eb;'>{html.escape(s['one_liner'])}</td>"
            f"</tr>"
        )

        feats = "".join(f"<li>{html.escape(f)}</li>" for f in s.get("features", []))
        tags = " ".join(
            f"<span style='display:inline-block;background:#f3f4f6;color:#374151;padding:2px 6px;border-radius:4px;font-size:12px;margin-right:4px;'>{html.escape(t)}</span>"
            for t in s.get("tags", [])
        )
        details_html.append(
            f"<div style='margin-bottom:18px;padding:12px 14px;border:1px solid #e5e7eb;border-radius:8px;'>"
            f"<h3 style='margin:0 0 6px 0;font-size:16px;'>{html.escape(p['rank'])}. <a href='{html.escape(p['url'])}' style='color:#1d4ed8;text-decoration:none;'>{html.escape(p['full_name'])}</a></h3>"
            f"<p style='margin:4px 0;color:#4b5563;font-size:13px;'>⭐ {html.escape(p['stars'])}（本期新增 🔺{html.escape(p['delta'])}）｜ 语言：{html.escape(p['language'])}{repeat_badge}</p>"
            f"<p style='margin:6px 0;'>{tags}</p>"
            f"<p style='margin:6px 0;'><strong>一句话定位：</strong>{html.escape(s['one_liner'])}</p>"
            f"<ul style='margin:6px 0;padding-left:20px;'>{feats}</ul>"
            f"<p style='margin:6px 0 0 0;color:#374151;'><strong>适用场景：</strong>{html.escape(s.get('use_cases', ''))}</p>"
            f"</div>"
        )

    return (
        "<!DOCTYPE html><html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#111827;line-height:1.6;max-width:860px;margin:0 auto;padding:16px;'>"
        f"<h2 style='margin-bottom:8px;'>{cfg['emoji']} OpenGithubs {cfg['label']}中文深度汇总（{html.escape(report_date)}）</h2>"
        f"<p style='color:#6b7280;font-size:14px;'>数据来源：<a href='https://github.com/{cfg['repo']}'>{cfg['repo']}</a> ｜ 共收录 {len(projects)} 个项目</p>"
        "<h3>📋 本期速览</h3>"
        "<table style='border-collapse:collapse;width:100%;font-size:13px;margin-bottom:20px;'>"
        "<thead><tr style='background:#f9fafb;'>"
        "<th style='padding:8px;border:1px solid #e5e7eb;'>排名</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;'>项目</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;'>语言</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;'>Star / 增长</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;'>一句话定位</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>"
        "<h3>🔍 项目功能详解</h3>"
        f"{''.join(details_html)}"
        "</body></html>"
    )


def send_email(mode: str, report_date: str, projects: list[dict[str, Any]], markdown_text: str) -> None:
    """可选：通过 SMTP（默认适配 QQ 邮箱 SSL 465）发送中文汇总邮件。"""
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    if not smtp_user or not smtp_pass:
        return

    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com").strip() or "smtp.qq.com"
    smtp_port = int(os.environ.get("SMTP_PORT", "465").strip() or "465")
    mail_to_raw = os.environ.get("MAIL_TO", "").strip() or smtp_user
    recipients = [addr.strip() for addr in mail_to_raw.split(",") if addr.strip()]

    cfg = MODE_CONFIG[mode]
    subject = f"{cfg['emoji']} OpenGithubs {cfg['label']}中文汇总（{report_date}）"

    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(markdown_text, "plain", "utf-8"))
    msg.attach(MIMEText(render_email_html(mode, report_date, projects), "html", "utf-8"))

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())
        print(f"[email] 邮件发送成功 -> {', '.join(recipients)}")
    except Exception as exc:
        print(f"[email] 邮件发送失败: {exc}", file=sys.stderr)


def run_mode(mode: str, *, limit: int | None, dry_run: bool) -> None:
    cfg = MODE_CONFIG[mode]
    print(f"[{mode}] 正在拉取 {cfg['repo']} ...")
    md_text = fetch_rank_markdown(mode)
    report_date, items = parse_rank_markdown(md_text)
    if not items:
        raise RuntimeError(f"[{mode}] 未能解析出任何榜单项目")

    if limit and limit > 0:
        items = items[:limit]

    print(f"[{mode}] 榜单日期: {report_date}，共 {len(items)} 个项目")
    cache = load_cache()

    results: list[dict[str, Any] | None] = [None] * len(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_map = {
            pool.submit(process_project, item, cache, dry_run=dry_run): idx
            for idx, item in enumerate(items)
        }
        for fut in concurrent.futures.as_completed(future_map):
            idx = future_map[fut]
            data, from_cache = fut.result()
            results[idx] = data
            if not dry_run and not from_cache and data.get("summary", {}).get("one_liner"):
                cache[data["full_name"]] = {
                    "language": data["language"],
                    "license": data["license"],
                    "homepage": data["homepage"],
                    "description": data["description"],
                    "summary": data["summary"],
                    "updated_at": data["updated_at"],
                }

    final_projects = [r for r in results if r is not None]
    if not dry_run:
        save_cache(cache)

    markdown = render_markdown(mode, report_date, final_projects)
    out_dir = REPORTS_DIR / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{report_date}.md").write_text(markdown, encoding="utf-8")
    (out_dir / "LATEST.md").write_text(markdown, encoding="utf-8")
    update_root_readme()

    if not dry_run:
        send_webhook(mode, report_date, final_projects)
        send_email(mode, report_date, final_projects, markdown)

    print(f"[{mode}] 已生成: reports/{mode}/{report_date}.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenGithubs 趋势榜单中文汇总生成器")
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "monthly", "all"],
        default="daily",
        help="选择要抓取的榜单频率",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅处理前 N 个项目（调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="跳过大模型调用与缓存写入，仅验证抓取与解析",
    )
    args = parser.parse_args()

    modes = ["daily", "weekly", "monthly"] if args.mode == "all" else [args.mode]
    for m in modes:
        run_mode(m, limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
