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
        """Tell the manager when the last git poll ran and when the next one is.
        next_iso None means a check is in progress (the portal shows a spinner)."""
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
    message = out["choices"][0]["message"]
    content = message.get("content")
    if isinstance(content, list):  # some servers return content blocks
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    content = content or ""
    call = native_tool_call(message.get("tool_calls"))
    if call and parse_tool_call(content) is None:
        # Some models (seen with Mistral) answer with the API's built-in
        # tool_calls even though no tools were declared. Turn the first one
        # into the text format, so the conversation stays in one format.
        content = (content.strip() + "\n\n" if content.strip() else "") + "```json\n" + json.dumps(call) + "\n```"
    return content


def native_tool_call(tool_calls):
    """The first OpenAI-style tool call as {"tool": name, **args}, or None."""
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                continue
        if fn.get("name") and isinstance(args, dict):
            return {"tool": fn["name"], **args}
    return None


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


TRIGGERS = ("review_requests", "mentions", "assigned_issues", "assigned_prs")

# Task kind recorded for each trigger (assigned issues and PRs are told apart
# by the task source, e.g. "github:o/r#issues:3" vs "...#pull_requests:4").
TRIGGER_KIND = {
    "review_requests": "review",
    "mentions": "mention",
    "assigned_issues": "assigned",
    "assigned_prs": "assigned",
}


def agent_triggers(agent):
    """The triggers the agent responds to; all of them when unset."""
    return set(agent.get("triggers") or TRIGGERS)


def _github_search(agent, mgr, repo, qualifier, kind_q, login):
    """Open items of repo matching qualifier:login, falling back to a global
    search filtered to repo when the repo-scoped one fails (422/404 usually
    means the token cannot see the repo that way)."""
    q = f"repo:{repo} {qualifier}:{login} {kind_q}"
    url = gh_base(agent) + "/search/issues?" + urllib.parse.urlencode({"q": q, "per_page": 30})
    try:
        return (gh_req(agent, "GET", url) or {}).get("items", [])
    except Exception as e:
        log("repo-scoped query failed:", q, e)
        if mgr:
            mgr.emit("log", f"repo-scoped search failed for {repo} ({q}): {e}; trying a global search")
    gq = f"{qualifier}:{login} {kind_q}"
    gurl = gh_base(agent) + "/search/issues?" + urllib.parse.urlencode({"q": gq, "per_page": 50})
    try:
        gdata = gh_req(agent, "GET", gurl) or {}
    except Exception as e2:
        log("global query failed:", gq, e2)
        if mgr:
            mgr.emit("error", f"github poll failed ({gq}): {e2}")
        return []
    return [it for it in gdata.get("items", [])
            if (it.get("repository_url") or "").endswith("/repos/" + repo)]


# GitHub search qualifiers per trigger.
GITHUB_QUERIES = (
    ("mentions", "is:issue", "mentions"),
    ("mentions", "is:pull-request", "mentions"),
    ("assignee", "is:issue", "assigned_issues"),
    ("assignee", "is:pull-request", "assigned_prs"),
    ("reviewer", "is:pull-request", "review_requests"),
)


def find_git_items(agent, mgr=None):
    """Open items of the watched repos matching the agent's triggers, each
    tagged with _repo, _kind and _trigger. One bad repo/query does not abort
    the rest; errors are reported."""
    login = normalize_login(agent.get("gitLogin"))
    triggers = agent_triggers(agent)
    items = []
    for raw_repo in agent.get("gitRepos") or []:
        repo = normalize_repo(raw_repo)
        if "/" not in repo:
            if mgr:
                mgr.emit("error", f"skipping malformed repo {raw_repo!r} (want owner/repo)")
            continue
        if agent.get("gitProvider") == "github":
            for qualifier, kind_q, trigger in GITHUB_QUERIES:
                if trigger not in triggers:
                    continue
                for it in _github_search(agent, mgr, repo, qualifier, kind_q, login):
                    if it.get("state") != "open":
                        continue
                    it["_kind"] = "pull_requests" if "pull_request" in it else "issues"
                    it["_trigger"] = trigger
                    it["_repo"] = repo
                    items.append(it)
            if triggers & {"review_requests", "assigned_prs"}:
                # Search indexing lags; also list open PRs directly and match
                # requested reviewers / assignees client-side (deterministic).
                try:
                    prs = gh_req(agent, "GET", f"{gh_base(agent)}/repos/{repo}/pulls?state=open&per_page=50") or []
                    for p in prs:
                        reviewers = {r.get("login") for r in p.get("requested_reviewers", [])}
                        assignees = {a.get("login") for a in p.get("assignees", [])}
                        for trigger, hit in (("review_requests", login in reviewers),
                                             ("assigned_prs", login in assignees)):
                            if hit and trigger in triggers:
                                items.append(dict(p, _kind="pull_requests", _repo=repo, _trigger=trigger))
                except Exception as e:
                    log("pulls list failed:", repo, e)
                    if mgr:
                        mgr.emit("error", f"github pulls list failed for {repo}: {e}")
        elif agent.get("gitProvider") == "gitlab":
            proj = urllib.parse.quote(repo, safe="")
            for kind, path, trigger in (
                ("issues", f"/api/v4/projects/{proj}/issues?assignee_username={login}&state=opened", "assigned_issues"),
                ("merge_requests", f"/api/v4/projects/{proj}/merge_requests?assignee_username={login}&state=opened", "assigned_prs"),
                ("merge_requests", f"/api/v4/projects/{proj}/merge_requests?reviewer_username={login}&state=opened", "review_requests"),
                ("issues", f"/api/v4/projects/{proj}/search?scope=issues&search=" + urllib.parse.quote("@" + login), "mentions"),
            ):
                if trigger not in triggers:
                    continue
                try:
                    for it in gl_req(agent, "GET", path) or []:
                        if it.get("state") not in (None, "opened"):
                            continue
                        it["_kind"] = kind
                        it["_trigger"] = trigger
                        it["_repo"] = repo
                        items.append(it)
                except Exception as e:
                    log("gitlab fetch failed:", path, e)
                    if mgr:
                        mgr.emit("error", f"gitlab poll failed for {repo} ({path}): {e}")
    # dedupe by (repo, kind, number, trigger): the same PR can be both a
    # review request and an assigned PR, which are different jobs. Prefer the
    # copy from the PR listing: search results have no head SHA, which
    # review requests are keyed by.
    out = {}
    for it in items:
        num = it.get("number") or it.get("iid")
        key = (it.get("_repo", ""), it.get("_kind"), num, it.get("_trigger"))
        if key not in out or ("head" in it and "head" not in out[key]):
            out[key] = it
    return list(out.values())


def item_ref(agent, repo, it):
    num = it.get("number") or it.get("iid")
    return f"{agent.get('gitProvider')}:{repo}#{it.get('_kind', 'issues')}:{num}"


def item_url(it):
    return it.get("html_url") or it.get("web_url") or ""


def item_title(it):
    return it.get("title") or ""


def item_body(it):
    return (it.get("body") or it.get("description") or "")[:4000]


def item_updated(it):
    return it.get("updated_at") or ""


def recent_comments(agent, it, n=10):
    """Best-effort fetch of the last n comments for context, oldest first.
    On PRs this includes reviews and review comments (with file/line)."""
    try:
        repo, num = it["_repo"], item_number(it)
        out = []
        if agent.get("gitProvider") == "github":
            base = f"{gh_base(agent)}/repos/{repo}"
            for c in gh_list(agent, f"{base}/issues/{num}/comments"):
                out.append((c.get("created_at") or "", (c.get("user") or {}).get("login", "?"), c.get("body") or ""))
            if it.get("_kind") == "pull_requests":
                for r in gh_list(agent, f"{base}/pulls/{num}/reviews"):
                    if r.get("body") or r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
                        out.append((r.get("submitted_at") or "", (r.get("user") or {}).get("login", "?"),
                                    f"[review: {r.get('state', '').lower()}] {r.get('body') or ''}"))
                for c in gh_list(agent, f"{base}/pulls/{num}/comments"):
                    where = f"{c.get('path')}:{c.get('line') or c.get('original_line') or '?'}"
                    out.append((c.get("created_at") or "", (c.get("user") or {}).get("login", "?"),
                                f"[on {where}] {c.get('body') or ''}"))
        elif agent.get("gitProvider") == "gitlab":
            proj = urllib.parse.quote(repo, safe="")
            for c in gl_list(agent, f"/api/v4/projects/{proj}/{it['_kind']}/{num}/notes?sort=asc"):
                if not c.get("system"):
                    out.append((c.get("created_at") or "", (c.get("author") or {}).get("username", "?"), c.get("body") or ""))
        out.sort(key=lambda x: x[0])
        return [(author, body[:1500]) for _, author, body in out[-n:]]
    except Exception as e:
        log("comment fetch failed:", e)
    return []


def gh_list(agent, url, max_pages=10):
    """GET a paginated GitHub list endpoint (oldest first), up to max_pages."""
    out = []
    sep = "&" if "?" in url else "?"
    for page in range(1, max_pages + 1):
        batch = gh_req(agent, "GET", f"{url}{sep}per_page=100&page={page}") or []
        out += batch
        if len(batch) < 100:
            break
    return out


def gl_list(agent, path, max_pages=10):
    """GET a paginated GitLab list endpoint, up to max_pages."""
    out = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        batch = gl_req(agent, "GET", f"{path}{sep}per_page=100&page={page}") or []
        out += batch
        if len(batch) < 100:
            break
    return out


def item_number(it):
    return it.get("number") or it.get("iid")


def same_login(a, b):
    """Git logins are case-insensitive."""
    return bool(a) and normalize_login(a).lower() == normalize_login(b).lower()


def item_author(it):
    return (it.get("user") or {}).get("login") or (it.get("author") or {}).get("username") or ""


def owner_activity(agent, it):
    """The owner's comments (and, on PRs, reviews and review comments) on
    the item, as (created_at, body) pairs. Cached on the item for the pass."""
    if "_owner_activity" not in it:
        it["_owner_activity"] = _fetch_owner_activity(agent, it)
    return it["_owner_activity"]


def _fetch_owner_activity(agent, it):
    owner = normalize_login(agent.get("ownerLogin"))
    repo, num = it["_repo"], item_number(it)
    out = []
    if agent.get("gitProvider") == "github":
        base = f"{gh_base(agent)}/repos/{repo}"
        for c in gh_list(agent, f"{base}/issues/{num}/comments"):
            if same_login((c.get("user") or {}).get("login"), owner):
                out.append((c.get("created_at") or "", c.get("body") or ""))
        if it.get("_kind") == "pull_requests":
            for r in gh_list(agent, f"{base}/pulls/{num}/reviews"):
                if same_login((r.get("user") or {}).get("login"), owner) and r.get("submitted_at"):
                    out.append((r["submitted_at"], r.get("body") or ""))
            for c in gh_list(agent, f"{base}/pulls/{num}/comments"):
                if same_login((c.get("user") or {}).get("login"), owner):
                    out.append((c.get("created_at") or "", c.get("body") or ""))
    elif agent.get("gitProvider") == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        for n in gl_list(agent, f"/api/v4/projects/{proj}/{it['_kind']}/{num}/notes?sort=asc"):
            if not n.get("system") and same_login((n.get("author") or {}).get("username"), owner):
                out.append((n.get("created_at") or "", n.get("body") or ""))
    return out


def mentions_login(text, login):
    return re.search(r"(?<![\w-])@" + re.escape(login) + r"(?![\w-])", text or "", re.IGNORECASE) is not None


def last_request_actor(agent, it):
    """Who last assigned the agent to the item, or last requested its review
    (depending on the item's trigger). "" when unknown."""
    login = normalize_login(agent.get("gitLogin")).lower()
    repo, num = it["_repo"], item_number(it)
    review = it.get("_trigger") == "review_requests"
    if agent.get("gitProvider") == "github":
        actor = ""
        for e in gh_list(agent, f"{gh_base(agent)}/repos/{repo}/issues/{num}/events"):
            if review:
                hit = e.get("event") == "review_requested" and \
                    ((e.get("requested_reviewer") or {}).get("login") or "").lower() == login
            else:
                hit = e.get("event") == "assigned" and ((e.get("assignee") or {}).get("login") or "").lower() == login
            if hit:
                # In this API the "actor" of an assigned event is the assignee;
                # who did it is "assigner" (and "review_requester" for review
                # requests). Fall back to actor if GitHub ever drops them.
                who = e.get("review_requester" if review else "assigner") or e.get("actor") or {}
                actor = who.get("login") or ""
        return actor
    if agent.get("gitProvider") == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        phrase = "requested review from" if review else "assigned to"
        actor = ""
        for n in gl_list(agent, f"/api/v4/projects/{proj}/{it['_kind']}/{num}/notes?sort=asc"):
            body = n.get("body") or ""
            if n.get("system") and phrase in body and mentions_login(body.split(phrase, 1)[1], login):
                actor = (n.get("author") or {}).get("username") or ""
        return actor
    return ""


def owner_requested(agent, it, since=""):
    """(ok, reason): whether the owner asked for this work. Assignments and
    review requests must have been made by the owner; a mention must be
    written by the owner (in the description or a comment newer than since)."""
    owner = normalize_login(agent.get("ownerLogin"))
    if not owner:
        return False, "the agent has no owner set"
    trigger = it.get("_trigger")
    if trigger == "mentions":
        if latest_owner_mention(agent, it) > since:
            return True, ""
        return False, f"not mentioned by {owner}"
    actor = last_request_actor(agent, it)
    what = "review requested" if trigger == "review_requests" else "assigned"
    if same_login(actor, owner):
        return True, ""
    return False, f"{what} by {actor or 'someone else'}, not {owner}"


def latest_owner_mention(agent, it):
    """Time of the owner's latest mention of the agent on the item (in its
    description or a comment/review), "" if none. Mentions are keyed by it,
    so each new mention is new work."""
    owner = normalize_login(agent.get("ownerLogin"))
    login = normalize_login(agent.get("gitLogin"))
    times = [ts for ts, body in owner_activity(agent, it) if mentions_login(body, login)]
    if same_login(item_author(it), owner) and mentions_login(it.get("body") or it.get("description"), login):
        times.append(it.get("created_at") or "")
    return max(times, default="")


def feedback_marker(agent, it):
    """Timestamp of the owner's latest comment/review on the item ("" if
    none). Assigned work is keyed by it, so new feedback is new work."""
    return max((ts for ts, _ in owner_activity(agent, it)), default="")


def parse_source(source):
    """'github:owner/repo#issues:12@suffix' -> ('github', 'owner/repo',
    'issues', '12'). The @suffix (head SHA or feedback time) is dropped."""
    provider, rest = source.split(":", 1)
    repo, tail = rest.split("#", 1)
    kind, num = tail.split(":", 1)
    return provider, normalize_repo(repo), kind, num.split("@", 1)[0]


def post_comment(agent, source, body):
    """Post the LLM reply as a comment on the task's source item."""
    provider, repo, kind, num = parse_source(source)
    if provider == "github":
        gh_req(agent, "POST", f"{gh_base(agent)}/repos/{repo}/issues/{num}/comments", {"body": body})
    elif provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        gl_req(agent, "POST", f"/api/v4/projects/{proj}/{kind}/{num}/notes", {"body": body})


def item_noun(it):
    if it.get("_kind") == "merge_requests":
        return "merge request"
    return "pull request" if it.get("_kind") == "pull_requests" else "issue"


def render_task_input(agent, repo, it):
    """The item's context for a task: what happened, title, description,
    recent discussion. The playbook and workspace are added when it runs."""
    noun, num = item_noun(it), item_number(it)
    owner = normalize_login(agent.get("ownerLogin"))
    what = {
        "review_requests": f"{owner} requested your review on {noun} #{num}",
        "mentions": f"{owner} mentioned you on {noun} #{num}",
        "assigned_issues": f"{owner} assigned you {noun} #{num}",
        "assigned_prs": f"{owner} assigned you {noun} #{num}",
    }.get(it.get("_trigger"), f"activity on {noun} #{num}")
    lines = [
        f"{what} in {repo} ({item_url(it)}).",
        f"Title: {item_title(it)}",
        "Description:",
        item_body(it) or "(none)",
    ]
    comments = recent_comments(agent, it)
    if comments:
        lines.append("Recent comments (oldest first):")
        for author, cbody in comments:
            lines.append(f"- {author}: {cbody}")
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
    what = ", ".join(t.replace("_", " ") for t in TRIGGERS if t in agent_triggers(agent))
    mgr.emit("log", f"checking {provider} for {what} of @{login} in {', '.join(repos)}")
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now = now_dt.isoformat()
    nxt = (now_dt + datetime.timedelta(seconds=agent.get("pollSeconds", 1800))).isoformat()
    mgr.set_poll(now, None)  # checking: no next time until this pass is over
    try:
        check_git_items(mgr, agent, state, state_path, now)
    finally:
        mgr.set_poll(now, nxt)


def work_ref(agent, it):
    """Dedupe key of one unit of work. Review requests are keyed by head SHA
    (a new push is new work), assigned work by the owner's latest feedback
    (a new comment or review is new work), mentions by the owner's latest
    mention (a new mention is new work)."""
    ref = item_ref(agent, it["_repo"], it)
    trigger = it.get("_trigger")
    if trigger == "review_requests":
        sha = (it.get("head") or {}).get("sha") or it.get("sha") or ""
        return ref + "@" + sha[:12] if sha else ref
    if trigger in ("assigned_issues", "assigned_prs"):
        return ref + "@" + (feedback_marker(agent, it) or "assigned")
    if trigger == "mentions":
        ts = latest_owner_mention(agent, it)
        return ref + "@" + ts if ts else ref
    return ref


def check_git_items(mgr, agent, state, state_path, now):
    """One git pass: turn the owner's new requests into tasks."""
    handled = set(state.get("handled", []))
    ignored = state.get("ignored", {})
    items = find_git_items(agent, mgr)
    if "baseline" not in state:
        # First pass only records what exists; old mentions/assignments are not
        # answered. Review requests are state, not history: they are left
        # unhandled so a pending review is picked up on the next pass.
        state["baseline"] = now
        state["mentions_keyed_by_time"] = True
        n = 0
        for it in items:
            if it.get("_trigger") == "review_requests":
                continue
            handled.add(work_ref(agent, it))
            n += 1
        state["handled"] = sorted(handled)
        save_state(state_path, state)
        mgr.emit("log", f"baseline recorded: {n} existing item(s) will not be answered")
        return
    seen = set()
    for it in items:
        repo, trigger = it["_repo"], it["_trigger"]
        try:
            ref = work_ref(agent, it)
            seen.add(ref)
        except Exception as e:
            mgr.emit("error", f"could not check {item_ref(agent, repo, it)}: {e}")
            continue
        base = item_ref(agent, repo, it)
        if ref in handled or (ref == base + "@assigned" and base in handled):
            continue  # (the second test: handled before assigned work was keyed by feedback)
        if trigger == "mentions" and base in handled and not state.get("mentions_keyed_by_time"):
            # Handled when mentions were keyed by item only: count the
            # current mention as handled, later ones are new work.
            handled.add(ref)
            continue
        if trigger != "review_requests" and item_updated(it) and item_updated(it) <= state["baseline"]:
            handled.add(ref)
            continue
        try:
            ok, why = owner_requested(agent, it, since=state["baseline"])
        except Exception as e:
            mgr.emit("error", f"could not check who asked for {ref}: {e}")
            continue
        if not ok:
            # Not handled: if the owner asks later, it becomes work then.
            if ignored.get(ref) != why:
                ignored[ref] = why
                mgr.emit("log", f"ignoring {ref}: {why}")
            continue
        ignored.pop(ref, None)
        handled.add(ref)
        try:
            kind = TRIGGER_KIND[trigger]
            title = f"[{trigger.replace('_', ' ')}] {item_title(it) or ref} ({repo})"
            mgr.create_task(kind, title, render_task_input(agent, repo, it), ref)
            mgr.emit("action", f"new work picked up: {ref}")
        except Exception as e:
            mgr.emit("error", f"could not create task for {ref}: {e}")
    state["handled"] = sorted(handled)
    state["mentions_keyed_by_time"] = True
    state["ignored"] = {r: why for r, why in ignored.items() if r in seen}  # forget items that went away
    save_state(state_path, state)


def save_state(state_path, state):
    try:
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("state save failed:", e)


# ---------- workspace preparation & playbooks ----------

GUIDANCE_NAMES = ("agents.md", "claude.md", "contributing.md")
GUIDANCE_FILE_CHARS = 8000
GUIDANCE_TOTAL_CHARS = 32000
MAX_LISTED_DOCS = 150


def git(repo_dir, *args, check=True):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=SHELL_TIMEOUT)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout.strip()


def clone_url(agent, repo):
    if agent.get("gitProvider") == "gitlab":
        base = (agent.get("gitBaseURL") or "https://gitlab.com").rstrip("/")
    else:
        u = urllib.parse.urlparse(agent.get("gitBaseURL") or "https://github.com")
        base = f"{u.scheme}://{u.netloc}"
    return f"{base}/{repo}.git"


def slugify(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].rstrip("-") or "work"


def issue_branch(agent, num, title):
    return f"{normalize_login(agent.get('gitLogin')).lower()}/{num}-{slugify(title)}"


def fetch_item(agent, repo, kind, num):
    if agent.get("gitProvider") == "github":
        path = "pulls" if kind == "pull_requests" else "issues"
        return gh_req(agent, "GET", f"{gh_base(agent)}/repos/{repo}/{path}/{num}") or {}
    proj = urllib.parse.quote(repo, safe="")
    return gl_req(agent, "GET", f"/api/v4/projects/{proj}/{kind}/{num}") or {}


def open_pr_for_branch(agent, repo, branch):
    """URL of the open PR/MR whose head is branch, or ""."""
    if agent.get("gitProvider") == "github":
        owner = repo.split("/")[0]
        q = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
        prs = gh_req(agent, "GET", f"{gh_base(agent)}/repos/{repo}/pulls?{q}") or []
        return prs[0].get("html_url", "") if prs else ""
    proj = urllib.parse.quote(repo, safe="")
    q = urllib.parse.urlencode({"source_branch": branch, "state": "opened"})
    mrs = gl_req(agent, "GET", f"/api/v4/projects/{proj}/merge_requests?{q}") or []
    return mrs[0].get("web_url", "") if mrs else ""


def checkout_branch(repo_dir, branch, default):
    """Check out branch: the local one if any, else the remote one, else a
    new one from origin/default."""
    if git(repo_dir, "branch", "--list", branch):
        git(repo_dir, "checkout", branch)
        if git(repo_dir, "ls-remote", "--heads", "origin", branch):
            git(repo_dir, "merge", "--ff-only", f"origin/{branch}", check=False)
    elif git(repo_dir, "ls-remote", "--heads", "origin", branch):
        git(repo_dir, "checkout", "-b", branch, "--track", f"origin/{branch}")
    else:
        git(repo_dir, "checkout", "-b", branch, f"origin/{default}")


def repo_guidance(repo_dir):
    """The repo's agent/contributor guidance (AGENTS.md, CLAUDE.md,
    CONTRIBUTING.md at any depth, the root README) and the list of its other
    Markdown files."""
    files = [f for f in git(repo_dir, "ls-files", "-z", check=False).split("\0") if f.lower().endswith(".md")]
    def wanted(f):
        name = os.path.basename(f).lower()
        return name in GUIDANCE_NAMES or f.lower() == "readme.md"
    picked = sorted((f for f in files if wanted(f)), key=lambda f: (f.count("/"), f.lower()))
    parts, total = [], 0
    for f in picked:
        try:
            with open(os.path.join(repo_dir, f), errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if len(text) > GUIDANCE_FILE_CHARS:
            text = text[:GUIDANCE_FILE_CHARS] + f"\n[... truncated; read the rest with read_file if needed]"
        if total + len(text) > GUIDANCE_TOTAL_CHARS:
            parts.append(f"--- {f} (not included: too long, read it with read_file) ---")
            continue
        total += len(text)
        parts.append(f"--- {f} ---\n{text}")
    others = [f for f in files if f not in picked]
    listed = others[:MAX_LISTED_DOCS]
    more = f" (and {len(others) - len(listed)} more)" if len(others) > len(listed) else ""
    out = ["Repository guidance (follow it):", *(parts or ["(no AGENTS.md, CLAUDE.md, CONTRIBUTING.md or README.md)"])]
    if listed:
        out.append(f"Other Markdown files in the repository{more}; read the ones relevant to this task:")
        out.append(", ".join(listed))
    return "\n".join(out)


PLAYBOOKS = {
    "review_requests": """\
Your job: review this pull request. The pull request's head is checked out in {repo_dir}.
1. Read the repository guidance below and the files relevant to the change.
2. Read the diff (`git -C {repo_dir} diff origin/{base}...HEAD`) and check it against the guidance and the description. Run the tests if useful.
3. Do not commit, push or open pull requests.
Your final answer is posted as a comment on the pull request: give your findings and end with whether it passes your review.""",
    "mentions": """\
Your job: answer the owner's latest mention. The repository is checked out in {repo_dir} if you need to look at the code.
Only answer: do not change code, commit, push or open pull requests. If the owner asks for code changes, say that the issue should be assigned for implementation.
Your final answer is posted as a comment there. Be concise and lead with the answer.""",
    "assigned_issues": """\
Your job: implement this issue. The branch `{branch}` is checked out in {repo_dir}.{existing}
1. Read the repository guidance below, the other documentation relevant to the change (see the list of Markdown files) and any file the issue refers to.
2. If the issue is blocked (for example by another open issue or pull request), unclear, or needs a decision from the owner, do not write code: your final answer is your question(s) to the owner.
3. Otherwise make the change on `{branch}`, add or update tests, and run the checks the guidance asks for (tests, linters, formatters). Fix failures before going on.
4. Commit with clear messages and push: `git -C {repo_dir} push -u origin {branch}`. Never push to `{base}`, never force-push, never merge.
5. If there is no pull request for `{branch}` yet, open one with create_pr (repo {repo}, head `{branch}`, base `{base}`) whose body explains the change and ends with `Closes #{num}`.
Your final answer is posted as a comment on the issue: a short summary of what you did and the pull request link, or your questions.""",
    "assigned_prs": """\
Your job: follow up on this pull request. Its branch `{branch}` is checked out in {repo_dir}.{fork}
1. Read the repository guidance below and the files relevant to the change.
2. Address the owner's review comments and feedback in the discussion. If there is none, check that the pull request is complete (tests, the checks the guidance asks for, a description that matches the change) and fix what is missing.
3. If feedback is unclear or needs a decision, ask instead of guessing.
4. Commit with clear messages and push to `{branch}`. Never force-push, never merge.
Your final answer is posted as a comment on the pull request: a short summary of what changed, or your questions.""",
}


def task_trigger(t):
    """The trigger a git task came from, from its kind and source."""
    kind = t.get("kind")
    if kind == "review":
        return "review_requests"
    if kind == "mention":
        return "mentions"
    if kind == "assigned":
        _, _, src_kind, _ = parse_source(t["source"])
        return "assigned_issues" if src_kind == "issues" else "assigned_prs"
    return None


def prepare_task(agent, t):
    """Clone/fetch the task's repository, check out what the trigger needs
    and return the playbook + workspace + guidance text that goes before
    the task input."""
    trigger = task_trigger(t)
    provider, repo, kind, num = parse_source(t["source"])
    repo_dir = os.path.join(WS, "repos", repo.split("/")[-1])
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", clone_url(agent, repo), repo_dir],
                           capture_output=True, text=True, timeout=SHELL_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"git clone {repo}: {r.stderr.strip()[:500]}")
    git(repo_dir, "fetch", "-q", "--prune", "origin")
    git(repo_dir, "remote", "set-head", "origin", "--auto", check=False)
    base = git(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).removeprefix("origin/") or "main"
    if git(repo_dir, "status", "--porcelain"):
        # Leftovers from an earlier task: keep them in a stash, start clean.
        git(repo_dir, "stash", "push", "-u", "-m", f"doppio: leftovers before task {t['id']}")
    pr_ref = f"merge-requests/{num}/head" if provider == "gitlab" else f"pull/{num}/head"
    vars_ = {"repo": repo, "repo_dir": repo_dir, "base": base, "num": num, "branch": "", "existing": "", "fork": ""}
    if trigger == "review_requests" or (trigger == "mentions" and kind != "issues"):
        item = fetch_item(agent, repo, kind, num)
        vars_["base"] = (item.get("base") or {}).get("ref") or item.get("target_branch") or base
        git(repo_dir, "fetch", "-q", "origin", pr_ref)
        git(repo_dir, "checkout", "-q", "--detach", "FETCH_HEAD")
    elif trigger == "assigned_issues":
        item = fetch_item(agent, repo, kind, num)
        branch = issue_branch(agent, num, item.get("title"))
        checkout_branch(repo_dir, branch, base)
        vars_["branch"] = branch
        pr = open_pr_for_branch(agent, repo, branch)
        if pr:
            vars_["existing"] = f" A pull request for it already exists ({pr}): continue the work there."
    elif trigger == "assigned_prs":
        item = fetch_item(agent, repo, kind, num)
        head = item.get("head") or {}
        branch = head.get("ref") or item.get("source_branch") or ""
        head_repo = (head.get("repo") or {}).get("full_name") or repo
        if provider == "gitlab" and item.get("source_project_id") not in (None, item.get("target_project_id")):
            head_repo = "a fork"
        vars_["base"] = (item.get("base") or {}).get("ref") or item.get("target_branch") or base
        if head_repo.lower() != repo.lower():
            git(repo_dir, "fetch", "-q", "origin", pr_ref)
            git(repo_dir, "checkout", "-q", "-B", branch or f"pr-{num}", "FETCH_HEAD")
            vars_["fork"] = (f" It comes from {head_repo}, so you cannot push to it: "
                             "describe the changes needed in your answer instead.")
        else:
            checkout_branch(repo_dir, branch, base)
        vars_["branch"] = branch
    else:  # mentions on an issue
        git(repo_dir, "checkout", "-q", "--detach", f"origin/{base}")
    parts = [PLAYBOOKS[trigger].format(**vars_),
             f"Workspace: {repo_dir} (default branch `{base}`, currently on "
             f"`{git(repo_dir, 'rev-parse', '--abbrev-ref', 'HEAD', check=False)}`)."]
    if trigger != "mentions":
        parts.append(repo_guidance(repo_dir))
    return "\n\n".join(parts)


# ---------- tools ----------

DEFAULT_MAX_TURNS = 25  # used when the agent has no maxTurns set; -1 = unlimited
SHELL_TIMEOUT = 300

TOOL_SPEC = """
You can act with tools. Each turn, reply with EITHER:
- a final plain-text answer (no JSON block), or
- exactly one tool call in a ```json fenced block:
  {"tool":"shell","command":"..."}                     run a shell command in /agent/workspace (git is preconfigured with your credentials; for git tasks the repository is already cloned and checked out under repos/<name>, see the task — do NOT clone again)
  {"tool":"read_file","path":"..."}                    read a file under /agent/workspace
  {"tool":"write_file","path":"...","content":"..."}   write a file under /agent/workspace
  {"tool":"create_pr","repo":"owner/repo","head":"your-branch","base":"main","title":"...","body":"..."}
Rules: one tool call per turn, wait for its result, keep going until the work is done, then give a short plain-text summary. Work on your own branch; never push to main/master. Repos you watch: {repos}.
"""


def parse_tool_call(text):
    """Return (name, args) of the first valid JSON tool call, or None.
    Returns ('__invalid__', reason) if a json fence is present but holds no
    usable object. The object is decoded from right after the opening
    fence, so a missing closing fence (some models stop right before it)
    or braces inside the command don't matter."""
    decoder = json.JSONDecoder()
    first_error = None
    for m in re.finditer(r"```json[ \t]*\n?", text):
        rest = text[m.end():].lstrip()
        try:
            obj, _ = decoder.raw_decode(rest)
        except json.JSONDecodeError as e:
            first_error = first_error or str(e)
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            return obj["tool"], obj
        first_error = first_error or "the json block has no \"tool\" field"
    if first_error:
        return "__invalid__", first_error
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


# ---------- engines ----------
#
# An engine runs one task: it gets a TaskRun (prompt, repository, limits) and
# returns an EngineResult. The git side stays in run_task for every engine:
# workspace preparation before, posting the answer and the retry policy after.

DEFAULT_TASK_MINUTES = 30

# Task outcomes: "done"; "failed" (it ran but didn't succeed: turn limit,
# unusable replies); "error" (something around it broke: the model API, the
# network, the workspace, the time limit) and "limit" (the account's usage
# limit), both retried at a later git check (see after_git_task).
RETRIED_OUTCOMES = ("error", "limit")


class TaskTimeout(Exception):
    pass


class EngineResult:
    def __init__(self, outcome, answer="", error=""):
        self.outcome = outcome
        self.answer = answer
        self.error = error

    def __repr__(self):
        return f"EngineResult({self.outcome!r}, answer={self.answer[:40]!r}, error={self.error!r})"


class TaskRun:
    """What an engine gets for one task."""

    def __init__(self, mgr, agent, system, task, prompt, workdir, minutes=None):
        self.mgr = mgr
        self.agent = agent
        self.system = system  # persona + standing task (+ tool spec for the Doppio loop)
        self.task = task
        self.prompt = prompt  # playbook, workspace, guidance and item context
        self.workdir = workdir  # the task's repository, or the workspace
        self.minutes = minutes or agent.get("taskMinutes") or DEFAULT_TASK_MINUTES
        self.deadline = time.monotonic() + self.minutes * 60
        max_turns = agent.get("maxTurns") or DEFAULT_MAX_TURNS
        self.max_turns = None if max_turns < 0 else max_turns  # None: until the context limit

    def emit(self, type_, content):
        self.mgr.emit(type_, content, task_id=self.task["id"])

    def remaining(self):
        return self.deadline - time.monotonic()

    def check_time(self):
        if self.remaining() <= 0:
            raise TaskTimeout(f"time limit ({self.minutes} min) reached")


def run_process(run, argv, stdin_text="", env=None, on_line=None):
    """Run an engine's process in the task's directory, feeding stdin_text and
    handing each stdout line to on_line. The process (and its children) is
    killed at the task's time limit, raising TaskTimeout. Returns
    (exit code, last lines of stderr)."""
    import signal
    import threading
    proc = subprocess.Popen(argv, cwd=run.workdir, env=env, text=True, start_new_session=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_tail = []

    def read_err():
        for line in proc.stderr:
            err_tail.append(line.rstrip("\n"))
            del err_tail[:-20]

    def read_out():
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\n"))

    readers = [threading.Thread(target=f, daemon=True) for f in (read_out, read_err)]
    for r in readers:
        r.start()
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    try:
        code = proc.wait(timeout=max(0.1, run.remaining()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise TaskTimeout(f"time limit ({run.minutes} min) reached; {argv[0]} killed")
    for r in readers:
        r.join(timeout=5)
    return code, "\n".join(err_tail)


def doppio_engine(run):
    """Doppio's own tool loop over the agent's model API."""
    messages = [{"role": "user", "content": run.prompt}]
    turn = 0
    bad_json = 0
    while True:
        run.check_time()
        turn += 1
        if run.max_turns is not None and turn > run.max_turns:
            return EngineResult("failed", error=f"max turns ({run.max_turns}) reached")
        reply = llm_call(run.agent, run.system, messages)
        run.emit("assistant", reply)
        call = parse_tool_call(reply)
        if call is None and reply.strip():
            return EngineResult("done", answer=reply)
        if call is None:
            # An empty reply is not a final answer: ask the model to answer.
            bad_json += 1
            if bad_json >= 3:
                return EngineResult("failed", error="model kept returning empty replies")
            # Providers reject empty assistant messages: keep a placeholder.
            messages.append({"role": "assistant", "content": "(empty reply)"})
            messages.append({"role": "user", "content":
                             "Your last reply was empty. Call a tool, or give your plain-text final answer."})
            continue
        name, args = call
        if name == "__invalid__":
            # The model tried a tool call but the JSON was malformed: do not
            # treat it as a final answer; ask for a retry.
            bad_json += 1
            if bad_json >= 3:
                return EngineResult("failed", error=f"3 invalid tool calls in a row: {args}")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             f"Your tool call JSON was invalid ({args}). Reply with exactly one valid "
                             "```json fenced tool call, or a plain-text final answer if done."})
            continue
        bad_json = 0
        run.emit("action", f"$ {name} {json.dumps(args)[:300]}")
        try:
            result = execute_tool(run.agent, name, args)
        except Exception as e:
            result = f"tool error: {type(e).__name__}: {e}"
        run.emit("log", f"→ {result[:1500]}")
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"})


ENGINES = {"doppio": doppio_engine}


def task_workdir(t):
    """The task's repository directory (see prepare_task), or the workspace."""
    if t.get("source") and task_trigger(t):
        try:
            repo = parse_source(t["source"])[1]
            d = os.path.join(WS, "repos", repo.split("/")[-1])
            if os.path.isdir(d):
                return d
        except Exception:
            pass
    return WS


def run_task(mgr, agent, system, t):
    """Run one task with the agent's engine, then post its answer on the
    git item it came from. Returns the outcome: "done", "failed", "error"
    or "limit" (see RETRIED_OUTCOMES)."""
    tid = t["id"]
    mgr.emit("status", f"running task: {t['title']}", task_id=tid)
    engine_name = agent.get("engine") or "doppio"
    engine = ENGINES.get(engine_name)
    if engine is None:
        msg = f"engine {engine_name!r} is not available in this worker"
        mgr.emit("error", msg, task_id=tid)
        mgr.finish(tid, "failed", error=msg)
        return "failed"
    prompt = t["input"]
    if t.get("source") and task_trigger(t):
        try:
            prompt = prepare_task(agent, t) + "\n\n" + prompt
        except Exception as e:
            mgr.emit("error", f"could not prepare the workspace: {type(e).__name__}: {e}", task_id=tid)
            mgr.finish(tid, "failed", error=f"workspace preparation failed: {e}")
            return "error"
    mgr.emit("user", prompt, task_id=tid)
    run = TaskRun(mgr, agent, system, t, prompt, task_workdir(t))
    try:
        res = engine(run)
    except TaskTimeout as e:
        res = EngineResult("error", error=str(e))
    except Exception as e:
        res = EngineResult("error", error=f"{type(e).__name__}: {e}")
    if res.outcome != "done":
        mgr.emit("error", res.error or f"task {res.outcome}", task_id=tid)
        mgr.finish(tid, "failed", error=res.error or res.outcome)
        return res.outcome
    if t.get("source"):
        if res.answer.strip():
            try:
                post_comment(agent, t["source"], res.answer)
                mgr.emit("action", f"posted reply on {t['source']}", task_id=tid)
            except Exception as e:
                mgr.emit("error", f"could not post reply on {t['source']}: {e}", task_id=tid)
        else:
            mgr.emit("error", f"empty answer; nothing posted on {t['source']}", task_id=tid)
    mgr.finish(tid, "done", output=res.answer)
    return "done"


MAX_TASK_ATTEMPTS = 3


def after_git_task(mgr, state, state_path, t, outcome):
    """Retry policy for tasks created from git activity. A task that broke
    because of the model API, the network, the workspace or a usage limit
    ("error", "limit") is
    forgotten so the next git check creates it again, up to
    MAX_TASK_ATTEMPTS attempts; restarting the agent grants another round
    (see reset_given_up). Tasks that ran and failed are not retried."""
    src = t.get("source")
    if not src:
        return
    retries = state.setdefault("retries", {})
    if outcome not in RETRIED_OUTCOMES:
        if retries.pop(src, None) is not None:
            save_state(state_path, state)
        return
    n = retries.get(src, 0) + 1
    retries[src] = n
    if n < MAX_TASK_ATTEMPTS:
        state["handled"] = [h for h in state.get("handled", []) if h != src]
        mgr.emit("log", f"will retry {src} at the next git check (attempt {n + 1} of {MAX_TASK_ATTEMPTS})",
                 task_id=t["id"])
    else:
        mgr.emit("error", f"giving up on {src} after {n} attempts; restart the agent to try again",
                 task_id=t["id"])
    save_state(state_path, state)


def reset_given_up(state):
    """On startup, give work abandoned after MAX_TASK_ATTEMPTS another round:
    a restart usually follows fixing what broke (a token, the model URL).
    Returns the refs that will be retried."""
    retries = state.get("retries", {})
    again = [ref for ref, n in retries.items() if n >= MAX_TASK_ATTEMPTS]
    for ref in again:
        retries[ref] = 0
    state["handled"] = [h for h in state.get("handled", []) if h not in again]
    return again


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
    engine = agent.get("engine") or "doppio"
    via = f"{agent['provider']}/{agent['model']}" + ("" if engine == "doppio" else f" via {engine}")
    log("worker starting:", agent["name"], f"({via})")
    mgr.emit("status", f"worker started: {agent['name']} ({via})")
    again = reset_given_up(state)
    if again:
        save_state(state_path, state)
        mgr.emit("log", f"retrying work given up before the restart: {', '.join(again)}")
    last_git = 0.0
    poll_every = agent.get("pollSeconds", 1800)
    while True:
        try:
            t = mgr.next_task()
            if t:
                after_git_task(mgr, state, state_path, t, run_task(mgr, agent, system, t))
            if time.time() - last_git >= poll_every:
                last_git = time.time()
                poll_git(mgr, agent, state, state_path)
        except Exception as e:
            mgr.emit("error", f"loop error: {type(e).__name__}: {e}")
            time.sleep(10)
            continue
        # Wake up in time for the next git check, so it starts when the
        # portal's countdown runs out rather than up to TASK_POLL_SECONDS late.
        time.sleep(max(0.2, min(TASK_POLL_SECONDS, last_git + poll_every - time.time())))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("fatal:", e)
        sys.exit(1)
