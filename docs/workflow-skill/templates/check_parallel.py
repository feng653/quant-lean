#!/usr/bin/env python3
"""Parallel-eligibility guard for the automation pipeline.

Rules:
  R1 serial barrier: a task labeled p:serial may start only when no other
     task is in progress; if any in-progress task is p:serial, no new task
     may start (regardless of domains).
  R2 domain conflict: otherwise a new task may start iff its domain:*
     labels do not intersect with the domain:* labels of any in-progress task.

In-progress tasks = open issues with the `agent` label (excluding the
triggering issue itself, which is the one being evaluated).

Exit 0 = allowed to start. Exit 1 = blocked (comment posted, `agent` label
removed so the task returns to queued state).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "{{OWNER}}/{{REPO}}")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
_raw_issue = os.environ.get("NEW_ISSUE_NUMBER", "").strip()
NEW_ISSUE = int(_raw_issue) if _raw_issue.isdigit() else 0


def gh_api(path: str) -> dict | list:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def post_comment(body: str) -> None:
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues/{NEW_ISSUE}/comments",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    urllib.request.urlopen(req)


def remove_label(label: str) -> None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues/{NEW_ISSUE}/labels/{label}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def main() -> int:
    if not NEW_ISSUE:
        print("no issue context, skip guard")
        return 0

    issues = gh_api(f"/issues?state=open&labels=agent&per_page=100")
    new_labels = []
    in_progress: list[tuple[int, set[str]]] = []
    for issue in issues:
        names = {l["name"] for l in issue["labels"]}
        if issue["number"] == NEW_ISSUE:
            new_labels = names
        else:
            in_progress.append((issue["number"], names))

    new_domains = {l for l in new_labels if l.startswith("domain:")}
    new_serial = "p:serial" in new_labels

    if new_serial and in_progress:
        msg = (
            "⛔ **串行屏障**：本任务带 `p:serial`（全局改动），但以下任务仍在进行中，"
            "按版本顺序要求必须等待它们完成：\n"
            + "、".join(f"#{n}" for n, _ in in_progress)
            + "\n\n已将 `agent` 标签移除，任务回到排队状态。完成后重新加回 `agent` 标签即可开工。"
        )
        post_comment(msg)
        remove_label("agent")
        print(f"BLOCKED (serial barrier): active tasks {[n for n, _ in in_progress]}")
        return 1

    for num, labels in in_progress:
        if "p:serial" in labels:
            msg = (
                f"⛔ **串行屏障**：任务 #{num} 带 `p:serial`（全局改动）正在进行中，"
                "本任务必须等它完成才能开工。\n\n"
                "已将 `agent` 标签移除，任务回到排队状态。"
            )
            post_comment(msg)
            remove_label("agent")
            print(f"BLOCKED (serial barrier from #{num})")
            return 1

    for num, labels in in_progress:
        overlap = new_domains & {l for l in labels if l.startswith("domain:")}
        if overlap:
            names = ", ".join(sorted(overlap))
            msg = (
                f"⛔ **领域冲突**：本任务涉及 `{names}`，与进行中任务 #{num} 重叠。"
                "同一领域同一时刻只允许一个任务开工。\n\n"
                "选项：① 等 #{num} 完成后重加 `agent` 标签；② 缩小本任务范围后调整 `domain:` 标签。\n"
                "已将 `agent` 标签移除，任务回到排队状态。"
            )
            post_comment(msg)
            remove_label("agent")
            print(f"BLOCKED (domain conflict with #{num}: {names})")
            return 1

    print(f"ALLOWED: serial={new_serial} domains={sorted(new_domains)} active={[n for n, _ in in_progress]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
