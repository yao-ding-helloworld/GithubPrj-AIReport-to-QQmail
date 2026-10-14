# -*- coding: utf-8 -*-
"""GitHub Actions 运维：触发 workflow、跟踪运行状态。

本机 `git push` 走不通，但 api.github.com 稳定，所以这里也走 API：
    POST /repos/{o}/{r}/actions/workflows/{id}/dispatches    触发
    GET  /repos/{o}/{r}/actions/runs                        列运行
    GET  /repos/{o}/{r}/actions/runs/{id}/jobs               看每个 step 的结果

用法：
    python tools/gh_actions.py list                 # 列仓库里的 workflow
    python tools/gh_actions.py trigger              # 触发（默认 daily-radar，ref=main）
    python tools/gh_actions.py status               # 看最近几次运行
    python tools/gh_actions.py watch --timeout 480  # 触发后盯着跑完
"""

import datetime
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402

OWNER, REPO = "yao-ding-helloworld", "GithubPrj-AIReport-to-QQmail"
BASE = "/repos/%s/%s" % (OWNER, REPO)
LINES = []

ICON = {"success": "[PASS]", "failure": "[FAIL]", "cancelled": "[stop]",
        "skipped": "[skip]", "in_progress": "[..]", "queued": "[queue]",
        "completed": "[done]", "startup_failure": "[FAIL]"}


def say(text=""):
    LINES.append(str(text))


def flush():
    out = config.ROOT / ".data" / "actions.txt"
    out.write_text("\n".join(LINES), encoding="utf-8")
    print("\n".join(LINES))


def api(method, path, token, payload=None):
    url = path if path.startswith("http") else config.GITHUB_API + path
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
        "Authorization": "Bearer " + token,
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:300]}
    except Exception as exc:                                # noqa: BLE001
        return None, {"error": "%s: %s" % (type(exc).__name__, exc)}


def find_workflow(token, name="daily-radar"):
    status, data = api("GET", "%s/actions/workflows" % BASE, token)
    if status != 200:
        return None, "列 workflow 失败：HTTP %s %s" % (status, data)
    for wf in data.get("workflows", []):
        if name in (wf.get("name") or "") or name in (wf.get("path") or ""):
            return wf, None
    return None, "没找到名字含 %r 的 workflow。仓库里有：%s" % (
        name, ", ".join(w.get("path", "?") for w in data.get("workflows", [])))


def latest_run(token, workflow_id=None):
    query = "?per_page=5"
    path = "%s/actions/runs%s" % (BASE, query)
    if workflow_id:
        path = "%s/actions/workflows/%s/runs%s" % (BASE, workflow_id, query)
    status, data = api("GET", path, token)
    if status != 200:
        return None, "列运行失败：HTTP %s %s" % (status, data)
    runs = data.get("workflow_runs") or []
    return (runs[0] if runs else None), None


def describe_run(run):
    created = run.get("created_at", "")
    try:
        created = datetime.datetime.fromisoformat(
            created.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:                                       # noqa: BLE001
        pass
    status = run.get("status")
    conclusion = run.get("conclusion") or "-"
    return ("#%s %s%s  %s  %s  事件=%s"
            % (run.get("run_number"), ICON.get(status, "[?]"), status,
               ICON.get(conclusion, "") + conclusion if conclusion != "-" else "",
               created, run.get("event")))


def show_jobs(token, run_id):
    status, data = api("GET", "%s/actions/runs/%s/jobs" % (BASE, run_id), token)
    if status != 200:
        say("  (取 jobs 失败 %s)" % status)
        return
    for job in data.get("jobs", []):
        say("  ● %s  %s/%s" % (job.get("name"),
                               job.get("status"), job.get("conclusion") or "-"))
        for step in job.get("steps", []):
            say("      %-8s %s" % (ICON.get(step.get("conclusion") or
                                            step.get("status"), "[?]"),
                                   step.get("name")))


def main(argv):
    env.load_env()
    token = env.github_token()
    if not token:
        say("[FAIL] .env 里没有 GITHUB_TOKEN")
        return flush() or 1

    cmd = argv[1] if len(argv) > 1 else "status"
    say("== GitHub Actions ==")
    say("仓库：%s/%s" % (OWNER, REPO))
    say("")

    wf, error = find_workflow(token)
    if error:
        say("[FAIL] " + error)
        return flush() or 1
    say("workflow：%s（%s，id=%s）" % (wf["name"], wf["path"], wf["id"]))
    say("")

    if cmd == "list":
        status, data = api("GET", "%s/actions/workflows" % BASE, token)
        for item in data.get("workflows", []):
            say("  %-9s %s  (%s)" % (item.get("state"), item.get("path"),
                                     item.get("name")))
        return flush() or 0

    if cmd == "trigger":
        ref = argv[2] if len(argv) > 2 else "main"
        status, data = api("POST", "%s/actions/workflows/%s/dispatches"
                           % (BASE, wf["id"]), token,
                           {"ref": ref, "inputs": {"notify": "true"}})
        if status in (204, 200):
            say("[PASS] 已触发 workflow（ref=%s），排队中…" % ref)
        else:
            say("[FAIL] 触发失败：HTTP %s %s" % (status, data))
            say("       常见原因：Token 缺 workflow scope；或 workflow 文件不在默认分支上")
            return flush() or 1
        time.sleep(12)
        run, error = latest_run(token, wf["id"])
        if run:
            say("最新运行：%s" % describe_run(run))
            say("链接：%s" % run.get("html_url"))
        return flush() or 0

    if cmd == "status":
        status, data = api("GET", "%s/actions/runs?per_page=5" % BASE, token)
        if status != 200:
            say("[FAIL] 列运行失败：HTTP %s %s" % (status, data))
            return flush() or 1
        runs = data.get("workflow_runs") or []
        if not runs:
            say("(还没有任何运行记录)")
            return flush() or 0
        for run in runs:
            say(describe_run(run))
        say("")
        show_jobs(token, runs[0]["id"])
        return flush() or 0

    if cmd == "watch":
        timeout = 480
        for i, arg in enumerate(argv):
            if arg == "--timeout" and i + 1 < len(argv):
                timeout = int(argv[i + 1])
        status, data = api("POST", "%s/actions/workflows/%s/dispatches"
                           % (BASE, wf["id"]), token,
                           {"ref": "main", "inputs": {"notify": "true"}})
        if status not in (204, 200):
            say("[FAIL] 触发失败：HTTP %s %s" % (status, data))
            return flush() or 1
        say("[PASS] 已触发，开始跟踪（上限 %d 秒）…" % timeout)
        say("")

        deadline = time.time() + timeout
        run_id = None
        last_status = None
        while time.time() < deadline:
            time.sleep(20)
            run, _ = latest_run(token, wf["id"])
            if not run:
                continue
            run_id = run["id"]
            key = (run.get("status"), run.get("conclusion"))
            if key != last_status:
                say("%s  %s / %s   （第 %d 秒）"
                    % ("[状态变化]" if last_status else "[首次]",
                       run.get("status"), run.get("conclusion") or "-",
                       int(time.time() - (deadline - timeout))))
                last_status = key
            if run.get("status") == "completed":
                break

        if run_id:
            say("")
            say("--- 各步骤结果 ---")
            show_jobs(token, run_id)
            run, _ = latest_run(token, wf["id"])
            say("")
            say("最终：%s" % describe_run(run))
            say("链接：%s" % run.get("html_url"))
        return flush() or 0

    say("未知子命令：%s（可用 list / trigger / status / watch）" % cmd)
    return flush() or 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
