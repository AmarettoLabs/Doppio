#!/usr/bin/env python3
"""Generic agent worker, run inside the per-agent container by the manager.

Config is read from AGENT_CONFIG (a JSON file mounted read-only, written by
the manager with mode 0600):

    {"agent": {...full agent config incl. secrets...},
     "managerUrl": "http://manager:8080",
     "managerToken": "..."}

Loop: claim pending tasks from the manager (manual tasks from the portal, or
mention/assigned tasks this worker creates itself), run them through the
configured LLM provider, post the reply back to the git host when the task
came from a mention, and stream every step as an event to the manager so the
portal can show live logs and discussions.
"""
import datetime
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CFG_PATH = os.environ.get("AGENT_CONFIG", "/agent/config.json")
WS = os.environ.get("AGENT_WORKSPACE", "/agent/workspace")
TASK_POLL_SECONDS = 5

DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "mistral": "https://api.mistral.ai/v1",
}


def log(*a):
    print("[doppio]", *a, flush=True)


def normalize_login(s):
    return (s or "").strip().lstrip("@")


def normalize_repo(r):
    """Accept 'owner/repo', URLs ('https://github.com/owner/repo.git',
    'git@host:owner/repo.git') and strip suffixes -> 'owner/repo'."""
    r = (r or "").strip().rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    if "://" in r:  # https://host/owner/repo
        r = r.split("://", 1)[1].split("/", 1)[-1]
    elif r.startswith("git@"):  # git@host:owner/repo
        r = r.split(":", 1)[-1]
    return r.strip("/")


SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
_ssl_ctx = None


def setup_extra_ca():
    """Trust the extra CAs the manager mounted (DOPPIO_EXTRA_CA) on top of the
    system ones, for this worker and for the commands its shell tool runs
    (git, curl, python requests...)."""
    global _ssl_ctx
    extra = os.environ.get("DOPPIO_EXTRA_CA")
    if not extra:
        return
    bundle = "/tmp/doppio-ca-bundle.pem"
    with open(bundle, "w") as out:
        if os.path.exists(SYSTEM_CA):
            with open(SYSTEM_CA) as f:
                out.write(f.read().rstrip("\n") + "\n")
        with open(extra) as f:
            out.write(f.read())
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
        os.environ[k] = bundle
    _ssl_ctx = ssl.create_default_context(cafile=bundle)
    log("trusting extra CA certificates from", extra)


def http_json(method, url, headers, body=None, timeout=30):
    """HTTP request returning parsed JSON; HTTP errors carry the response body."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
    return json.loads(raw) if raw else None


class Manager:
    """Client for the manager's internal API."""

    def __init__(self, cfg):
        self.base = cfg["managerUrl"].rstrip("/")
        self.token = cfg["managerToken"]
        self.agent_id = cfg["agent"]["id"]

    def req(self, method, path, body=None):
        return http_json(
            method,
            self.base + path,
            {"Content-Type": "application/json", "X-Manager-Token": self.token},
            body,
        )

    def emit(self, type_, content, task_id=None):
        try:
            self.req(
                "POST",
                f"/api/internal/agents/{self.agent_id}/events",
                {"type": type_, "content": content, "taskId": task_id or ""},
            )
        except Exception as e:  # never die because the manager is unreachable
            log("emit failed:", e)

    def next_task(self):
        return self.req("GET", f"/api/internal/agents/{self.agent_id}/next-task")

    def create_task(self, kind, title, inp, source=""):
        return self.req(
            "POST",
            f"/api/internal/agents/{self.agent_id}/tasks",
            {"kind": kind, "title": title, "input": inp, "source": source},
        )

    def finish(self, task_id, status, output="", error=""):
        try:
            self.req(
                "POST",
                f"/api/internal/tasks/{task_id}/finish",
                {"status": status, "output": output, "error": error},
            )
        except Exception as e:
            log("finish failed:", e)

    def set_poll(self, last_iso, next_iso):
        """Tell the manager when the last git poll ran and when the next one is."""
        try:
            self.req(
                "POST",
                f"/api/internal/agents/{self.agent_id}/poll",
                {"lastPollAt": last_iso, "nextPollAt": next_iso},
            )
        except Exception as e:
            log("set_poll failed:", e)


def llm_call(agent, system, messages):
    """One chat call to the agent's provider. Returns the assistant text."""
    provider = agent["provider"]
    model = agent["model"]
    key = agent.get("apiToken") or ""
    base = (agent.get("apiBase") or DEFAULT_BASE.get(provider, "")).rstrip("/")
    if provider == "anthropic":
        url = base + "/v1/messages"
        body = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    else:  # openai, mistral: OpenAI-compatible chat completions
        url = base + "/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    out = http_json("POST", url, headers, body, timeout=180)
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    content = out["choices"][0]["message"].get("content")
    if isinstance(content, list):  # some servers return content blocks
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


# ---------- git providers ----------


def gh_base(agent):
    """api.github.com, or a GitHub Enterprise base (gitBaseURL[/api/v3])."""
    base = (agent.get("gitBaseURL") or "").rstrip("/")
    if not base:
        return "https://api.github.com"
    if "/api/v3" not in base and "api.github.com" not in base:
        base += "/api/v3"
    return base


def gh_req(agent, method, url, body=None):
    return http_json(
        method,
        url,
        {
            "Authorization": "Bearer " + (agent.get("gitToken") or ""),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        body,
    )


def gl_req(agent, method, path, body=None):
    base = (agent.get("gitBaseURL") or "https://gitlab.com").rstrip("/")
    return http_json(
        method,
        base + path,
        {"PRIVATE-TOKEN": agent.get("gitToken") or "", "Content-Type": "application/json"},
        body,
    )


def _tag(data, repo):
    out = []
    for it in (data or {}).get("items", []):
        it["_kind"] = "pull_requests" if "pull_request" in it else "issues"
        it["_repo"] = repo
        out.append(it)
    return out


def find_git_items(agent, mgr=None):
    """Mentions of the agent's login and items assigned to it, per repo.
    One bad repo/query does not abort the rest; errors are reported."""
    login = normalize_login(agent.get("gitLogin"))
    items = []
    for raw_repo in agent.get("gitRepos") or []:
        repo = normalize_repo(raw_repo)
        if "/" not in repo:
            if mgr:
                mgr.emit("error", f"skipping malformed repo {raw_repo!r} (want owner/repo)")
            continue
        if agent.get("gitProvider") == "github":
            for qualifier in ("mentions", "assignee", "reviewer"):
                # GitHub's search validator requires is:issue or is:pull-request;
                # closed items are filtered client-side via item["state"].
                found, failed = [], False
                for kind_q in ("is:issue", "is:pull-request"):
                    q = f"repo:{repo} {qualifier}:{login} {kind_q}"
                    url = gh_base(agent) + "/search/issues?" + urllib.parse.urlencode({"q": q, "per_page": 30})
                    try:
                        found += (gh_req(agent, "GET", url) or {}).get("items", [])
                    except Exception as e:
                        failed = True
                        log("repo-scoped query failed:", q, e)
                        if mgr:
                            mgr.emit("log", f"repo-scoped search failed for {repo} ({q}): {e}")
                if failed and not found:
                    # 422/404 usually means the token cannot see the repo: fall back
                    # to global searches and keep only items of this repo.
                    if mgr:
                        mgr.emit("log", f"falling back to global {qualifier} search, filtered to {repo}")
                    for kind_q in ("is:issue", "is:pull-request"):
                        gq = f"{qualifier}:{login} {kind_q}"
                        gurl = gh_base(agent) + "/search/issues?" + urllib.parse.urlencode({"q": gq, "per_page": 50})
                        try:
                            gdata = gh_req(agent, "GET", gurl) or {}
                        except Exception as e2:
                            log("global query failed:", gq, e2)
                            if mgr:
                                mgr.emit("error", f"github poll failed ({gq}): {e2}")
                            continue
                        found += [it for it in gdata.get("items", [])
                                  if (it.get("repository_url") or "").endswith("/repos/" + repo)]
                for it in found:
                    if it.get("state") != "open":
                        continue
                    it["_kind"] = "pull_requests" if "pull_request" in it else "issues"
                    it["_qualifier"] = qualifier
                    it["_repo"] = repo
                    items.append(it)
            # Search indexing lags; also list open PRs directly and match
            # requested reviewers / assignees client-side (deterministic).
            try:
                prs = gh_req(agent, "GET", f"{gh_base(agent)}/repos/{repo}/pulls?state=open&per_page=50") or []
                for p in prs:
                    reviewers = {r.get("login") for r in p.get("requested_reviewers", [])}
                    assignees = {a.get("login") for a in p.get("assignees", [])}
                    if login in reviewers or login in assignees:
                        p["_kind"] = "pull_requests"
                        p["_repo"] = repo
                        p["_qualifier"] = "reviewer" if login in reviewers else "assignee"
                        items.append(p)
            except Exception as e:
                log("pulls list failed:", repo, e)
                if mgr:
                    mgr.emit("error", f"github pulls list failed for {repo}: {e}")
        elif agent.get("gitProvider") == "gitlab":
            proj = urllib.parse.quote(repo, safe="")
            for kind, path, qualifier in (
                ("issues", f"/api/v4/projects/{proj}/issues?assignee_username={login}&state=opened", "assignee"),
                ("merge_requests", f"/api/v4/projects/{proj}/merge_requests?assignee_username={login}&state=opened", "assignee"),
                ("merge_requests", f"/api/v4/projects/{proj}/merge_requests?reviewer_username={login}&state=opened", "reviewer"),
                ("issues", f"/api/v4/projects/{proj}/search?scope=issues&search=" + urllib.parse.quote("@" + login), "mentions"),
            ):
                try:
                    for it in gl_req(agent, "GET", path) or []:
                        it["_kind"] = kind
                        it["_qualifier"] = qualifier
                        it["_repo"] = repo
                        items.append(it)
                except Exception as e:
                    log("gitlab fetch failed:", path, e)
                    if mgr:
                        mgr.emit("error", f"gitlab poll failed for {repo} ({path}): {e}")
    # dedupe by (repo, kind, number)
    seen, out = set(), []
    for it in items:
        num = it.get("number") or it.get("iid")
        key = (it.get("_repo", ""), it.get("_kind"), num)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def item_ref(agent, repo, it):
    num = it.get("number") or it.get("iid")
    return f"{agent.get('gitProvider')}:{repo}#{it.get('_kind', 'issues')}:{num}"


def item_url(it):
    return it.get("html_url") or it.get("web_url") or ""


def item_title(it):
    return it.get("title") or ""


def item_body(it):
    return (it.get("body") or "")[:4000]


def item_updated(it):
    return it.get("updated_at") or ""


def recent_comments(agent, it):
    """Best-effort fetch of the last comments for context."""
    try:
        if agent.get("gitProvider") == "github":
            cs = gh_req(agent, "GET", it["comments_url"] + "?per_page=10") or []
            return [(c.get("user", {}).get("login", "?"), (c.get("body") or "")[:1000]) for c in cs]
        if agent.get("gitProvider") == "gitlab":
            proj = urllib.parse.quote(str(it.get("project_id", "")), safe="")
            kind = it.get("_kind", "issues")
            num = it.get("iid")
            notes = gl_req(agent, "GET", f"/api/v4/projects/{proj}/{kind}/{num}/notes?per_page=10&sort=desc") or []
            return [(n.get("author", {}).get("username", "?"), (n.get("body") or "")[:1000]) for n in notes]
    except Exception as e:
        log("comment fetch failed:", e)
    return []


def post_comment(agent, source, body):
    """Post the LLM reply as a comment. source: 'github:repo#issues:12' or
    'gitlab:repo#merge_requests:3'."""
    provider, rest = source.split(":", 1)
    repo, kind, num = normalize_repo(rest.split("#")[0]), rest.split("#")[1].split(":")[0], rest.split(":")[-1]
    num = num.split("@")[0]  # strip the @sha suffix used for review dedupe
    if provider == "github":
        gh_req(agent, "POST", f"{gh_base(agent)}/repos/{repo}/issues/{num}/comments", {"body": body})
    elif provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        gl_req(agent, "POST", f"/api/v4/projects/{proj}/{kind}/{num}/notes", {"body": body})


QUALIFIER_TEXT = {
    "mentions": "mentioned",
    "assignee": "assigned",
    "reviewer": "requested as a reviewer",
}


def render_mention_input(agent, repo, it):
    comments = recent_comments(agent, it)
    how = QUALIFIER_TEXT.get(it.get("_qualifier", "mentions"), "mentioned")
    extra = " Review the changes and post your review as a comment." if it.get("_qualifier") == "reviewer" else ""
    lines = [
        f"You were {how} on {repo} ({item_url(it)}).{extra}",
        f"Title: {item_title(it)}",
        "Description:",
        item_body(it) or "(none)",
    ]
    head = it.get("head") or {}
    if head.get("ref"):
        lines.append(f"PR head branch: `{head['ref']}` (sha {str(head.get('sha'))[:12]}). "
                     f"Check it out with: git -C repos/{repo.split('/')[-1].lower()} fetch origin && git -C repos/{repo.split('/')[-1].lower()} checkout {head['ref']}")
    if comments:
        lines.append("Recent comments:")
        for author, cbody in comments[-5:]:
            lines.append(f"- {author}: {cbody}")
    lines.append(
        f"\nWrite the reply as {agent['gitLogin']}. Your reply will be posted as a comment on that item. "
        "Be concise and lead with the answer."
    )
    return "\n".join(lines)


def poll_git(mgr, agent, state, state_path):
    if not agent.get("gitToken") or not agent.get("gitLogin"):
        return
    provider = agent.get("gitProvider")
    repos = [normalize_repo(r) for r in (agent.get("gitRepos") or [])]
    repos = [r for r in repos if "/" in r]
    if not repos:
        return
    login = normalize_login(agent["gitLogin"])
    mgr.emit("log", f"polling {provider} for mentions of @{login} / assignments in {', '.join(repos)}")
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now = now_dt.isoformat()
    nxt = (now_dt + datetime.timedelta(seconds=agent.get("pollSeconds", 1800))).isoformat()
    mgr.set_poll(now, nxt)
    handled = set(state.get("handled", []))
    items = find_git_items(agent, mgr)
    if "baseline" not in state:
        # First pass only records what exists; old mentions/assignments are not
        # answered. Review requests are state, not history: they are left
        # unhandled so a pending review is picked up on the next pass.
        state["baseline"] = now
        n = 0
        for it in items:
            if it.get("_qualifier") == "reviewer":
                continue
            handled.add(item_ref(agent, it["_repo"], it))
            n += 1
        state["handled"] = sorted(handled)
        save_state(state_path, state)
        mgr.emit("log", f"baseline recorded: {n} existing item(s) will not be answered")
        return
    for it in items:
        repo = it["_repo"]
        ref = item_ref(agent, repo, it)
        if it.get("_qualifier") == "reviewer":
            # Key review work by head SHA: a new push to a reviewed PR is new work.
            sha = (it.get("head") or {}).get("sha") or it.get("sha") or ""
            if sha:
                ref += "@" + sha[:12]
        if ref in handled:
            continue
        if it.get("_qualifier") != "reviewer" and item_updated(it) and item_updated(it) <= state["baseline"]:
            handled.add(ref)
            continue
        handled.add(ref)
        try:
            q = it.get("_qualifier", "mentions")
            kind = {"mentions": "mention", "assignee": "assigned", "reviewer": "review"}.get(q, "mention")
            title = f"[{kind}] {item_title(it) or ref} ({repo})"
            mgr.create_task(kind, title, render_mention_input(agent, repo, it), ref)
            mgr.emit("action", f"new work picked up: {ref}")
        except Exception as e:
            mgr.emit("error", f"could not create task for {ref}: {e}")
    state["handled"] = sorted(handled)
    save_state(state_path, state)


def save_state(state_path, state):
    try:
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("state save failed:", e)


# ---------- tools ----------

DEFAULT_MAX_TURNS = 25  # used when the agent has no maxTurns set; -1 = unlimited
SHELL_TIMEOUT = 300

TOOL_SPEC = """
You can act with tools. Each turn, reply with EITHER:
- a final plain-text answer (no JSON block), or
- exactly one tool call in a ```json fenced block:
  {"tool":"shell","command":"..."}                     run a shell command in /agent/workspace (git is preconfigured with your credentials; if /agent/workspace/repos/<name> already exists, use `git -C repos/<name> fetch origin && git checkout <branch>` — do NOT clone again)
  {"tool":"read_file","path":"..."}                    read a file under /agent/workspace
  {"tool":"write_file","path":"...","content":"..."}   write a file under /agent/workspace
  {"tool":"create_pr","repo":"owner/repo","head":"your-branch","base":"main","title":"...","body":"..."}
Rules: one tool call per turn, wait for its result, keep going until the work is done, then give a short plain-text summary. Work on your own branch; never push to main/master. Repos you watch: {repos}.
"""


def parse_tool_call(text):
    """Return (name, args) of the first valid JSON tool call, or None.
    Returns ('__invalid__', None) if a json fence is present but unparseable."""
    for m in re.finditer(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
        except Exception as e:
            return "__invalid__", str(e)
        if isinstance(obj, dict) and obj.get("tool"):
            return obj["tool"], obj
    if "```json" in text:
        return "__invalid__", "no parseable object in the json fence"
    return None


def setup_git(agent):
    """Configure git identity + credential store inside the workspace."""
    if not agent.get("gitToken"):
        return
    login = normalize_login(agent.get("gitLogin")) or "agent"
    email = (agent.get("emails") or [f"{login}@users.noreply.github.com"])[0]
    os.makedirs(WS, exist_ok=True)
    cred = os.path.join(WS, ".git-credentials")
    tok = agent["gitToken"]
    entries = []
    if agent.get("gitProvider") == "github":
        entries.append(f"https://x-access-token:{tok}@github.com")
    elif agent.get("gitProvider") == "gitlab":
        host = urllib.parse.urlparse(agent.get("gitBaseURL") or "https://gitlab.com").netloc
        entries.append(f"https://oauth2:{tok}@{host}")
    if entries:
        with open(cred, "w") as f:
            f.write("\n".join(entries) + "\n")
        os.chmod(cred, 0o600)
    for k, v in (("user.name", login), ("user.email", email),
                 ("credential.helper", f"store --file={cred}"),
                 ("init.defaultBranch", "main"), ("push.default", "current")):
        subprocess.run(["git", "config", "--global", k, v], check=False)


def ws_path(p):
    """Resolve p inside the workspace; reject escapes."""
    full = os.path.realpath(os.path.join(WS, p))
    if not (full == WS or full.startswith(WS + os.sep)):
        raise RuntimeError(f"path outside workspace: {p}")
    return full


def execute_tool(agent, name, args):
    if name == "shell":
        cmd = args.get("command", "")
        if not cmd.strip():
            return "empty command"
        env = dict(os.environ)
        env["GIT_TOKEN"] = agent.get("gitToken") or ""
        try:
            r = subprocess.run(cmd, shell=True, cwd=WS, env=env,
                               capture_output=True, text=True, timeout=SHELL_TIMEOUT)
            out = (r.stdout + r.stderr).strip()
            return f"exit={r.returncode}\n{out[:4000]}"
        except subprocess.TimeoutExpired:
            return f"timeout after {SHELL_TIMEOUT}s"
    if name == "read_file":
        with open(ws_path(args.get("path", ""))) as f:
            return f.read()[:8000]
    if name == "write_file":
        p = ws_path(args.get("path", ""))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(args.get("content", ""))
        return f"wrote {len(args.get('content', ''))} bytes to {args.get('path')}"
    if name == "create_pr":
        repo = normalize_repo(args.get("repo", ""))
        head, base = args.get("head", ""), args.get("base", "main")
        title, body = args.get("title", ""), args.get("body", "")
        if agent.get("gitProvider") == "github":
            out = gh_req(agent, "POST", f"{gh_base(agent)}/repos/{repo}/pulls",
                         {"title": title, "head": head, "base": base, "body": body})
            return f"PR created: {out.get('html_url', out)}"
        proj = urllib.parse.quote(repo, safe="")
        out = gl_req(agent, "POST", f"/api/v4/projects/{proj}/merge_requests",
                     {"source_branch": head, "target_branch": base, "title": title, "description": body})
        return f"MR created: {out.get('web_url', out)}"
    raise RuntimeError(f"unknown tool: {name}")


def run_task(mgr, agent, system, t):
    tid = t["id"]
    mgr.emit("status", f"running task: {t['title']}", task_id=tid)
    mgr.emit("user", t["input"], task_id=tid)
    messages = [{"role": "user", "content": t["input"]}]
    final = ""
    max_turns = agent.get("maxTurns") or DEFAULT_MAX_TURNS
    unlimited = max_turns < 0  # -1: run until the model's context limit
    turn = 0
    bad_json = 0
    try:
        while True:
            turn += 1
            if not unlimited and turn > max_turns:
                mgr.emit("error", f"max turns ({max_turns}) reached", task_id=tid)
                mgr.finish(tid, "failed", error=f"max turns ({max_turns}) reached")
                return
            reply = llm_call(agent, system, messages)
            mgr.emit("assistant", reply, task_id=tid)
            call = parse_tool_call(reply)
            if call is None and reply.strip():
                final = reply
                break
            if call is None and not reply.strip():
                # An empty reply is not a final answer: ask the model to answer.
                bad_json += 1
                if bad_json >= 3:
                    mgr.emit("error", "3 empty/invalid replies in a row", task_id=tid)
                    mgr.finish(tid, "failed", error="model kept returning empty replies")
                    return
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 "Your last reply was empty. Give a plain-text final answer now."})
                continue
            name, args = call
            if name == "__invalid__":
                # The model tried a tool call but the JSON was malformed: do not
                # treat it as a final answer; ask for a retry.
                bad_json += 1
                if bad_json >= 3:
                    mgr.emit("error", f"3 invalid tool calls in a row: {args}", task_id=tid)
                    mgr.finish(tid, "failed", error=f"invalid tool call JSON: {args}")
                    return
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 f"Your tool call JSON was invalid ({args}). Reply with exactly one valid "
                                 "```json fenced tool call, or a plain-text final answer if done."})
                continue
            bad_json = 0
            mgr.emit("action", f"$ {name} {json.dumps(args)[:300]}", task_id=tid)
            try:
                result = execute_tool(agent, name, args)
            except Exception as e:
                result = f"tool error: {type(e).__name__}: {e}"
            mgr.emit("log", f"→ {result[:1500]}", task_id=tid)
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"})
        if t.get("source"):
            if final.strip():
                try:
                    post_comment(agent, t["source"], final)
                    mgr.emit("action", f"posted reply on {t['source']}", task_id=tid)
                except Exception as e:
                    mgr.emit("error", f"could not post reply on {t['source']}: {e}", task_id=tid)
            else:
                mgr.emit("error", f"model returned an empty reply; nothing posted on {t['source']}", task_id=tid)
        mgr.finish(tid, "done", output=final)
    except Exception as e:
        mgr.emit("error", f"{type(e).__name__}: {e}", task_id=tid)
        mgr.finish(tid, "failed", error=str(e))


def main():
    setup_extra_ca()
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    agent = cfg["agent"]
    mgr = Manager(cfg)
    os.makedirs(WS, exist_ok=True)
    state_path = os.path.join(WS, "worker-state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    repos = ", ".join(normalize_repo(r) for r in (agent.get("gitRepos") or [])) or "(none configured)"
    system = (agent["persona"] + "\n\nStanding task: " + (agent.get("task") or "")
              + "\n\n" + TOOL_SPEC.replace("{repos}", repos))
    setup_git(agent)
    log("worker starting:", agent["name"], f"({agent['provider']}/{agent['model']})")
    mgr.emit("status", f"worker started: {agent['name']} ({agent['provider']}/{agent['model']})")
    last_git = 0.0
    while True:
        try:
            t = mgr.next_task()
            if t:
                run_task(mgr, agent, system, t)
            if time.time() - last_git >= agent.get("pollSeconds", 1800):
                last_git = time.time()
                poll_git(mgr, agent, state, state_path)
        except Exception as e:
            mgr.emit("error", f"loop error: {type(e).__name__}: {e}")
            time.sleep(10)
            continue
        time.sleep(TASK_POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("fatal:", e)
        sys.exit(1)
