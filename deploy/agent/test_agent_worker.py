"""Tests for the agent worker. Run: python -m unittest discover -s deploy/agent"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

WS_DIR = tempfile.mkdtemp(prefix="doppio-ws-")
# git stash/commit need an identity (the worker sets one up in the container).
for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
             ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ.setdefault(k, v)
os.environ["AGENT_WORKSPACE"] = WS_DIR
sys.path.insert(0, os.path.dirname(__file__))
import agent_worker as w  # noqa: E402

API = "https://api.github.com"
REPO = "o/r"


def agent(**kw):
    a = {"gitProvider": "github", "gitLogin": "bot", "ownerLogin": "boss", "gitToken": "t",
         "gitRepos": [REPO], "pollSeconds": 60}
    a.update(kw)
    return a


class FakeGitHub:
    """Answers gh_req from a {url-regex: response} table; records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, agent, method, url, body=None):
        self.calls.append((method, url, body))
        path = url.replace(API, "")
        for pat, resp in self.routes.items():
            if re.fullmatch(pat, path):
                return resp(path) if callable(resp) else resp
        if "page=" in path:
            return []
        raise AssertionError(f"unexpected request {method} {url}")


def search_route(results):
    """Route /search/issues to results[(qualifier, kind_q)]."""
    def resp(path):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["q"][0]
        m = re.match(r"repo:\S+ (\w+):\S+ (is:\S+)", q)
        return {"items": [dict(i) for i in results.get((m.group(1), m.group(2)), [])]}
    return resp


class Patched(unittest.TestCase):
    def use(self, routes):
        fake = FakeGitHub(routes)
        orig = w.gh_req
        w.gh_req = fake
        self.addCleanup(setattr, w, "gh_req", orig)
        return fake


class MgrStub:
    def __init__(self):
        self.events, self.tasks = [], []

    def emit(self, type_, content, task_id=None):
        self.events.append((type_, content))

    def create_task(self, kind, title, inp, source=""):
        self.tasks.append({"kind": kind, "title": title, "input": inp, "source": source})


ISSUE = {"number": 7, "state": "open", "title": "Add CI", "body": "please", "html_url": "u7",
         "updated_at": "2026-11-05T10:00:00Z", "created_at": "2026-11-05T09:00:00Z", "user": {"login": "boss"}}
PR = {"number": 8, "state": "open", "title": "Fix", "body": "", "html_url": "u8", "pull_request": {},
      "updated_at": "2026-11-05T10:00:00Z", "created_at": "2026-11-05T09:00:00Z", "user": {"login": "boss"},
      "head": {"sha": "abcdef1234567890", "ref": "fix"}}


class FindItemsTest(Patched):
    def test_only_enabled_triggers_are_searched(self):
        fake = self.use({
            r"/search/issues\?.*": search_route({("assignee", "is:issue"): [ISSUE]}),
            r"/repos/o/r/pulls\?.*": [],
        })
        items = w.find_git_items(agent(triggers=["assigned_issues"]))
        self.assertEqual([(i["number"], i["_trigger"]) for i in items], [(7, "assigned_issues")])
        queries = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("q", [""])[0] for _, u, _ in fake.calls]
        self.assertEqual(queries, ["repo:o/r assignee:bot is:issue"])  # no other searches, no PR listing

    def test_pr_listing_splits_review_and_assignment(self):
        pr = dict(PR, requested_reviewers=[{"login": "bot"}], assignees=[{"login": "bot"}])
        self.use({r"/search/issues\?.*": search_route({}), r"/repos/o/r/pulls\?.*": [pr]})
        items = w.find_git_items(agent(triggers=["review_requests", "assigned_prs"]))
        self.assertEqual(sorted(i["_trigger"] for i in items), ["assigned_prs", "review_requests"])

    def test_review_request_keeps_the_head_sha_from_the_pr_listing(self):
        found = dict(PR)
        del found["head"]  # search results have no head
        listed = dict(PR, requested_reviewers=[{"login": "bot"}], assignees=[])
        self.use({r"/search/issues\?.*": search_route({("reviewer", "is:pull-request"): [found]}),
                  r"/repos/o/r/pulls\?.*": [listed]})
        items = w.find_git_items(agent(triggers=["review_requests"]))
        self.assertEqual(len(items), 1)
        self.assertEqual(w.work_ref(agent(), items[0]), "github:o/r#pull_requests:8@abcdef123456")

    def test_unset_triggers_mean_all(self):
        self.assertEqual(w.agent_triggers(agent()), set(w.TRIGGERS))


def events(*evs):
    return lambda path: list(evs) if "page=1" in path else []


class OwnerGateTest(Patched):
    def it(self, base, trigger):
        return dict(base, _repo=REPO, _kind="pull_requests" if "pull_request" in base else "issues", _trigger=trigger)

    def test_assignment_by_owner_only(self):
        # Real GitHub shape: actor is the assignee, assigner is who assigned.
        assigned = lambda who: {"event": "assigned", "assignee": {"login": "bot"},
                                "actor": {"login": "bot"}, "assigner": {"login": who}}
        self.use({r"/repos/o/r/issues/7/events.*": events(assigned("peer"), assigned("Boss"))})
        self.assertEqual(w.owner_requested(agent(), self.it(ISSUE, "assigned_issues")), (True, ""))
        self.use({r"/repos/o/r/issues/7/events.*": events(assigned("boss"), assigned("peer"))})
        ok, why = w.owner_requested(agent(), self.it(ISSUE, "assigned_issues"))
        self.assertFalse(ok)
        self.assertIn("assigned by peer", why)

    def test_review_request_by_owner(self):
        req = lambda who: {"event": "review_requested", "requested_reviewer": {"login": "bot"},
                           "actor": {"login": who}, "review_requester": {"login": who}}
        self.use({r"/repos/o/r/issues/8/events.*": events(req("peer"))})
        ok, why = w.owner_requested(agent(), self.it(PR, "review_requests"))
        self.assertFalse(ok)
        self.assertIn("review requested by peer", why)
        self.use({r"/repos/o/r/issues/8/events.*": events(req("boss"))})
        self.assertTrue(w.owner_requested(agent(), self.it(PR, "review_requests"))[0])

    def test_mentions_must_be_written_by_owner(self):
        comment = lambda who, body, ts="2026-11-05T09:30:00Z": {"user": {"login": who}, "body": body, "created_at": ts}
        issue = dict(ISSUE, body="no mention here")
        self.use({r"/repos/o/r/issues/7/comments.*": events(comment("peer", "@bot do it"))})
        self.assertFalse(w.owner_requested(agent(), self.it(issue, "mentions"), since="2026-11-01")[0])
        self.use({r"/repos/o/r/issues/7/comments.*": events(comment("boss", "hey @bot?"))})
        self.assertTrue(w.owner_requested(agent(), self.it(issue, "mentions"), since="2026-11-01")[0])
        # older than the baseline: not a new request
        self.assertFalse(w.owner_requested(agent(), self.it(issue, "mentions"), since="2026-11-06")[0])
        # "@botany" is not a mention of @bot
        self.use({r"/repos/o/r/issues/7/comments.*": events(comment("boss", "see @botany"))})
        self.assertFalse(w.owner_requested(agent(), self.it(issue, "mentions"), since="2026-11-01")[0])

    def test_no_owner_accepts_nothing(self):
        self.assertEqual(w.owner_requested(agent(ownerLogin=""), self.it(ISSUE, "assigned_issues")),
                         (False, "the agent has no owner set"))


class CheckItemsTest(Patched):
    def test_owner_work_becomes_task_and_others_are_logged_once(self):
        mine = dict(ISSUE)
        theirs = dict(ISSUE, number=9, title="Other")
        assigned = lambda who: {"event": "assigned", "assignee": {"login": "bot"},
                                "actor": {"login": "bot"}, "assigner": {"login": who}}
        self.use({
            r"/search/issues\?.*": search_route({("assignee", "is:issue"): [mine, theirs]}),
            r"/repos/o/r/issues/7/events.*": events(assigned("boss")),
            r"/repos/o/r/issues/9/events.*": events(assigned("peer")),
            r"/repos/o/r/issues/\d+/comments.*": [],
        })
        mgr, state = MgrStub(), {"baseline": "2026-11-01T00:00:00Z"}
        path = os.path.join(WS_DIR, "state.json")
        a = agent(triggers=["assigned_issues"])
        w.check_git_items(mgr, a, state, path, "now")
        w.check_git_items(mgr, a, state, path, "now")  # second pass: nothing new
        self.assertEqual([(t["kind"], t["source"]) for t in mgr.tasks],
                         [("assigned", "github:o/r#issues:7@assigned")])
        self.assertIn("2026-11-01", state["baseline"])
        ignored = [c for t, c in mgr.events if t == "log" and c.startswith("ignoring")]
        self.assertEqual(len(ignored), 1)
        self.assertIn("#issues:9", ignored[0])

    def test_new_owner_feedback_on_assigned_work_is_new_work(self):
        it = dict(ISSUE, _repo=REPO, _kind="issues", _trigger="assigned_issues")
        self.use({r"/repos/o/r/issues/7/comments.*": events(
            {"user": {"login": "boss"}, "body": "answer", "created_at": "2026-11-05T11:00:00Z"},
            {"user": {"login": "bot"}, "body": "question", "created_at": "2026-11-05T12:00:00Z"})})
        self.assertEqual(w.work_ref(agent(), it), "github:o/r#issues:7@2026-11-05T11:00:00Z")

    def test_review_refs_are_keyed_by_head_sha(self):
        it = dict(PR, _repo=REPO, _kind="pull_requests", _trigger="review_requests")
        self.assertEqual(w.work_ref(agent(), it), "github:o/r#pull_requests:8@abcdef123456")


class SourceTest(unittest.TestCase):
    def test_parse_source_keeps_colons_in_suffix(self):
        self.assertEqual(w.parse_source("github:o/r#issues:12@2026-11-05T11:00:00Z"),
                         ("github", "o/r", "issues", "12"))

    def test_task_trigger(self):
        self.assertEqual(w.task_trigger({"kind": "assigned", "source": "github:o/r#issues:1"}), "assigned_issues")
        self.assertEqual(w.task_trigger({"kind": "assigned", "source": "github:o/r#pull_requests:1"}), "assigned_prs")
        self.assertEqual(w.task_trigger({"kind": "review", "source": "github:o/r#pull_requests:1"}), "review_requests")
        self.assertIsNone(w.task_trigger({"kind": "manual"}))

    def test_issue_branch(self):
        self.assertEqual(w.issue_branch(agent(gitLogin="@Bot"), 10, "CI checks for migrations!"),
                         "bot/10-ci-checks-for-migrations")


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class PrepareTaskTest(Patched):
    """prepare_task against a real (local, bare) origin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doppio-origin-")
        self.origin = os.path.join(self.tmp, "r.git")
        seed = os.path.join(self.tmp, "seed")
        sh("git", "init", "-q", "-b", "main", seed)
        for name, text in {"README.md": "# r\n", "AGENTS.md": "Run make test.\n",
                           "docs/design.md": "design\n", "pkg/CONTRIBUTING.md": "pkg rules\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(seed, name)), exist_ok=True)
            with open(os.path.join(seed, name), "w") as f:
                f.write(text)
        sh("git", "add", "-A", cwd=seed)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=seed)
        sh("git", "clone", "-q", "--bare", seed, self.origin)
        # A PR head, the way GitHub exposes it.
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "pr", cwd=seed)
        sh("git", "push", "-q", self.origin, "HEAD:refs/pull/8/head", cwd=seed)
        orig = w.clone_url
        w.clone_url = lambda agent, repo: self.origin
        self.addCleanup(setattr, w, "clone_url", orig)
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        if os.path.isdir(repo_dir):
            sh("rm", "-rf", repo_dir)

    def test_assigned_issue_gets_branch_playbook_and_guidance(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        text = w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7@assigned"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        self.assertEqual(w.git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD"), "bot/7-add-ci")
        self.assertIn("Your job: implement this issue", text)
        self.assertIn("Closes #7", text)
        self.assertIn("base `main`", text)
        self.assertIn("--- AGENTS.md ---\nRun make test.", text)
        self.assertIn("--- README.md ---", text)
        self.assertIn("--- pkg/CONTRIBUTING.md ---", text)
        self.assertIn("docs/design.md", text)  # listed, not inlined
        self.assertNotIn("--- docs/design.md", text)

    def test_review_checks_out_pr_head_and_stashes_leftovers(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        with open(os.path.join(repo_dir, "scratch.txt"), "w") as f:
            f.write("leftover")
        self.use({r"/repos/o/r/pulls/8": {"base": {"ref": "main"}, "head": {"ref": "fix"}}})
        text = w.prepare_task(agent(), {"id": "t2", "kind": "review", "source": "github:o/r#pull_requests:8@abc"})
        self.assertEqual(w.git(repo_dir, "log", "-1", "--format=%s"), "pr")
        self.assertIn("leftovers before task t2", w.git(repo_dir, "stash", "list"))
        self.assertIn("Your job: review this pull request", text)
        self.assertIn("Do not commit, push", text)


if __name__ == "__main__":
    unittest.main()
