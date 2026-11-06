"""Tests for the agent worker. Run: python -m unittest discover -s deploy/agent"""
import os
import re
import subprocess
import sys
import tempfile
import time
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

    def mention_pass(self, state, comments):
        self.use({
            r"/search/issues\?.*": search_route({("mentions", "is:issue"): [dict(ISSUE, body="")]}),
            r"/repos/o/r/issues/7/comments.*": events(*comments),
        })
        mgr = MgrStub()
        w.check_git_items(mgr, agent(triggers=["mentions"]), state, os.path.join(WS_DIR, "m.json"), "now")
        return [t["source"] for t in mgr.tasks]

    def test_each_new_owner_mention_is_new_work(self):
        c = lambda ts, body="@bot look": {"user": {"login": "boss"}, "body": body, "created_at": ts}
        state = {"baseline": "2026-11-01T00:00:00Z", "mentions_keyed_by_time": True}
        first = [c("2026-11-05T09:30:00Z")]
        self.assertEqual(self.mention_pass(state, first), ["github:o/r#issues:7@2026-11-05T09:30:00Z"])
        self.assertEqual(self.mention_pass(state, first), [])  # same mention: nothing new
        # A comment without a mention isn't a new request; a new mention is.
        self.assertEqual(self.mention_pass(state, first + [c("2026-11-05T10:00:00Z", "thanks")]), [])
        self.assertEqual(self.mention_pass(state, first + [c("2026-11-05T11:00:00Z")]),
                         ["github:o/r#issues:7@2026-11-05T11:00:00Z"])

    def test_mentions_handled_before_the_upgrade_are_not_redone(self):
        c = lambda ts: {"user": {"login": "boss"}, "body": "@bot look", "created_at": ts}
        state = {"baseline": "2026-11-01T00:00:00Z", "handled": ["github:o/r#issues:7"]}
        self.assertEqual(self.mention_pass(state, [c("2026-11-05T09:30:00Z")]), [])
        self.assertTrue(state["mentions_keyed_by_time"])
        self.assertEqual(self.mention_pass(state, [c("2026-11-05T09:30:00Z"), c("2026-11-05T12:00:00Z")]),
                         ["github:o/r#issues:7@2026-11-05T12:00:00Z"])

    def mention_on_assigned_pr_pass(self, state, assigner, comments):
        pr = dict(PR, body="", assignees=[{"login": "bot"}], requested_reviewers=[])
        self.use({
            r"/search/issues\?.*": search_route({("mentions", "is:pull-request"): [pr]}),
            r"/repos/o/r/pulls\?.*": [pr],
            r"/repos/o/r/issues/8/events.*": events({"event": "assigned", "assignee": {"login": "bot"},
                                                     "actor": {"login": "bot"}, "assigner": {"login": assigner}}),
            r"/repos/o/r/issues/8/comments.*": events(*comments),
            r"/repos/o/r/pulls/8/(reviews|comments).*": [],
        })
        mgr = MgrStub()
        w.check_git_items(mgr, agent(), state, os.path.join(WS_DIR, "a.json"), "now")
        return [(t["kind"], t["source"]) for t in mgr.tasks]

    def test_owner_mention_on_assigned_pr_is_assigned_work(self):
        # The mention and the assignment share the owner's comment as key:
        # the assigned task (which may change code) must win.
        c = lambda ts: {"user": {"login": "boss"}, "body": "@bot please fix the review", "created_at": ts}
        state = {"baseline": "2026-11-01T00:00:00Z", "mentions_keyed_by_time": True}
        first = [c("2026-11-05T11:00:00Z")]
        self.assertEqual(self.mention_on_assigned_pr_pass(state, "boss", first),
                         [("assigned", "github:o/r#pull_requests:8@2026-11-05T11:00:00Z")])
        self.assertEqual(self.mention_on_assigned_pr_pass(state, "boss", first), [])
        # A later mention is new feedback: assigned work again, never a mention task.
        self.assertEqual(self.mention_on_assigned_pr_pass(state, "boss", first + [c("2026-11-05T12:00:00Z")]),
                         [("assigned", "github:o/r#pull_requests:8@2026-11-05T12:00:00Z")])

    def test_owner_mention_on_pr_assigned_by_someone_else_is_answered(self):
        c = {"user": {"login": "boss"}, "body": "@bot what do you think?", "created_at": "2026-11-05T11:00:00Z"}
        state = {"baseline": "2026-11-01T00:00:00Z", "mentions_keyed_by_time": True}
        self.assertEqual(self.mention_on_assigned_pr_pass(state, "peer", [c]),
                         [("mention", "github:o/r#pull_requests:8@2026-11-05T11:00:00Z")])

    def test_new_owner_feedback_on_assigned_work_is_new_work(self):
        it = dict(ISSUE, _repo=REPO, _kind="issues", _trigger="assigned_issues")
        self.use({r"/repos/o/r/issues/7/comments.*": events(
            {"user": {"login": "boss"}, "body": "answer", "created_at": "2026-11-05T11:00:00Z"},
            {"user": {"login": "bot"}, "body": "question", "created_at": "2026-11-05T12:00:00Z"})})
        self.assertEqual(w.work_ref(agent(), it), "github:o/r#issues:7@2026-11-05T11:00:00Z")

    def test_review_refs_are_keyed_by_head_sha(self):
        it = dict(PR, _repo=REPO, _kind="pull_requests", _trigger="review_requests")
        self.assertEqual(w.work_ref(agent(), it), "github:o/r#pull_requests:8@abcdef123456")


class ParseToolCallTest(unittest.TestCase):
    def test_closed_and_unclosed_fences(self):
        call = ("shell", {"tool": "shell", "command": "ls"})
        self.assertEqual(w.parse_tool_call('```json\n{"tool":"shell","command":"ls"}\n```'), call)
        # Some models stop right before the closing fence.
        self.assertEqual(w.parse_tool_call('```json\n{"tool":"shell","command":"ls"}'), call)

    def test_braces_in_the_command_and_several_calls(self):
        name, args = w.parse_tool_call('```json\n{"tool":"shell","command":"awk \'{print $1}\' f"}\n```')
        self.assertEqual(args["command"], "awk '{print $1}' f")
        # Several calls glued together: the first one wins.
        self.assertEqual(w.parse_tool_call('```json\n{"tool":"a"}\n``````json\n{"tool":"b"}\n```')[0], "a")

    def test_bare_call_without_fence(self):
        text = 'let me use shell to view specific ranges.json\n{"tool":"shell","command":"sed -n \'1,5p\' a.go"}\n```'
        self.assertEqual(w.parse_tool_call(text), ("shell", {"tool": "shell", "command": "sed -n '1,5p' a.go"}))

    def test_invalid_and_plain_answers(self):
        self.assertEqual(w.parse_tool_call('```json\n{broken\n```')[0], "__invalid__")
        self.assertEqual(w.parse_tool_call('```json\n{"no":"tool"}\n```')[0], "__invalid__")
        self.assertIsNone(w.parse_tool_call("The PR looks good."))


class LlmCallTest(unittest.TestCase):
    def reply(self, message):
        orig = w.http_json
        self.addCleanup(setattr, w, "http_json", orig)
        w.http_json = lambda *a, **k: {"choices": [{"message": message}]}
        return w.llm_call(agent(provider="mistral", model="m", apiToken="k"), "sys", [{"role": "user", "content": "hi"}])

    def test_builtin_tool_calls_become_text_calls(self):
        text = self.reply({"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]}],
                           "tool_calls": [{"type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}},
                                          {"type": "function", "function": {"name": "shell", "arguments": '{"command": "pwd"}'}}]})
        self.assertEqual(w.parse_tool_call(text), ("shell", {"tool": "shell", "command": "ls"}))

    def test_reply_length_is_capped(self):
        bodies = []
        orig = w.http_json
        self.addCleanup(setattr, w, "http_json", orig)

        def fake(method, url, headers, body=None, timeout=30):
            bodies.append(body)
            return {"choices": [{"message": {"content": "same same same"}, "finish_reason": "length"}]}
        w.http_json = fake
        cases = [
            # provider, apiBase, maxReplyTokens, field, cap
            ("openai", "", 0, "max_completion_tokens", 32768),
            ("mistral", "https://api.mistral.ai/v1/", 0, "max_tokens", 32768),
            ("openai", "https://llm-server:8443/v1", 0, "max_completion_tokens", 8192),
            ("custom", "http://x/v1", 0, "max_tokens", 8192),
            ("custom", "http://x/v1", 20000, "max_tokens", 20000),
        ]
        for provider, base, cap, field, want in cases:
            with self.assertRaises(w.ReplyTruncated):
                w.llm_call(agent(provider=provider, model="m", apiToken="k", apiBase=base, maxReplyTokens=cap), "sys", [])
            self.assertEqual(bodies[-1][field], want, (provider, base, cap))

    def test_truncated_replies_are_retried_then_fail(self):
        orig = w.llm_call
        self.addCleanup(setattr, w, "llm_call", orig)
        replies = iter([w.ReplyTruncated("cut"), "Done, concisely."])
        sent = []

        def fake(agent_, system, messages):
            sent.append([dict(m) for m in messages])
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return r
        w.llm_call = fake
        self.assertEqual(w.run_task(RunTaskMgr(), agent(), "sys", {"id": "t", "title": "x", "input": "hi"}), "done")
        self.assertIn("cut off", sent[-1][-1]["content"])

        def always(*a):
            raise w.ReplyTruncated("cut")
        w.llm_call = always
        mgr = RunTaskMgr()
        self.assertEqual(w.run_task(mgr, agent(maxTurns=10), "sys", {"id": "t", "title": "x", "input": "hi"}), "failed")
        self.assertIn("length limit", mgr.finished[-1][2])

    def test_text_calls_win_and_plain_answers_stay(self):
        text = self.reply({"content": '```json\n{"tool":"read_file","path":"a"}\n```',
                           "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "ls"}}}]})
        self.assertEqual(w.parse_tool_call(text)[0], "read_file")
        self.assertEqual(self.reply({"content": "Done.", "tool_calls": None}), "Done.")

    def test_empty_replies_are_never_sent_back_empty(self):
        replies = iter(["", "All done."])
        sent = []
        orig = w.llm_call
        self.addCleanup(setattr, w, "llm_call", orig)
        def fake(agent_, system, messages):
            sent.append([dict(m) for m in messages])
            return next(replies)
        w.llm_call = fake
        mgr = RunTaskMgr()
        self.assertEqual(w.run_task(mgr, agent(), "sys", {"id": "t", "title": "x", "input": "hi"}), "done")
        self.assertTrue(all(m["content"] for m in sent[-1]))


class EngineTest(unittest.TestCase):
    def setUp(self):
        orig = dict(w.ENGINES)
        self.addCleanup(lambda: (w.ENGINES.clear(), w.ENGINES.update(orig)))
        self.mgr = RunTaskMgr()

    def run_task(self, engine, **cfg):
        w.ENGINES["fake"] = engine
        return w.run_task(self.mgr, agent(engine="fake", **cfg), "sys", {"id": "t1", "title": "x", "input": "do it"})

    def test_engine_gets_the_prompt_and_limits(self):
        seen = {}

        def fake(run):
            seen.update(prompt=run.prompt, system=run.system, workdir=run.workdir,
                        minutes=run.minutes, turns=run.max_turns)
            return w.EngineResult("done", answer="all good")
        self.assertEqual(self.run_task(fake, maxTurns=-1, taskMinutes=5), "done")
        self.assertEqual(seen, {"prompt": "do it", "system": "sys", "workdir": WS_DIR,
                                "minutes": 5, "turns": None})
        self.assertEqual(self.mgr.finished[-1][1], "done")

    def test_outcomes(self):
        for outcome in ("failed", "error", "limit"):
            self.assertEqual(self.run_task(lambda run, o=outcome: w.EngineResult(o, error="nope")), outcome)
            self.assertEqual(self.mgr.finished[-1][1:], ("failed", "nope"))

        def crash(run):
            raise OSError("boom")
        self.assertEqual(self.run_task(crash), "error")

    def test_time_limit(self):
        def slow(run):
            run.deadline = time.monotonic() - 1  # as if the limit had passed
            run.check_time()
        self.assertEqual(self.run_task(slow, taskMinutes=7), "error")
        self.assertIn("time limit (7 min)", self.mgr.finished[-1][2])

    def test_unknown_engine_fails(self):
        out = w.run_task(self.mgr, agent(engine="nope"), "sys", {"id": "t1", "title": "x", "input": "hi"})
        self.assertEqual((out, self.mgr.finished[-1][1]), ("failed", "failed"))

    def test_usage_limits_are_retried(self):
        src = "github:o/r#issues:3@assigned"
        state = {"handled": [src]}
        w.after_git_task(MgrStub(), state, os.path.join(WS_DIR, "limit-state.json"), {"id": "t", "source": src}, "limit")
        self.assertNotIn(src, state["handled"])
        self.assertEqual(state["retries"][src], 1)


class RunProcessTest(unittest.TestCase):
    def run_obj(self, minutes=1):
        return w.TaskRun(MgrStub(), agent(), "sys", {"id": "t"}, "p", WS_DIR, minutes=minutes)

    def test_streams_stdout_and_keeps_stderr(self):
        lines = []
        code, err = w.run_process(self.run_obj(), ["sh", "-c", "cat; echo second; echo oops >&2; exit 3"],
                                  stdin_text="first\n", on_line=lines.append)
        self.assertEqual((code, lines, err), (3, ["first", "second"], "oops"))

    def test_killed_at_the_time_limit(self):
        run = self.run_obj()
        run.deadline = time.monotonic() + 0.5
        started = time.monotonic()
        with self.assertRaises(w.TaskTimeout):
            w.run_process(run, ["sh", "-c", "sleep 30 & sleep 30"])
        self.assertLess(time.monotonic() - started, 10)
class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(WS_DIR, "long.txt")
        with open(self.path, "w") as f:
            f.write("".join(f"line {i:04d} " + "x" * 40 + "\n" for i in range(1, 501)))

    def test_read_file_in_parts(self):
        out = w.execute_tool(agent(), "read_file", {"path": "long.txt"})
        self.assertIn("line 0001", out)
        self.assertRegex(out, r"\[lines 1-(\d+) of 500\. Read more with start_line=\d+\.\]$")
        nxt = int(re.search(r"start_line=(\d+)", out).group(1))
        out2 = w.execute_tool(agent(), "read_file", {"path": "long.txt", "start_line": nxt})
        self.assertTrue(out2.startswith(f"line {nxt:04d}"))
        tail = w.execute_tool(agent(), "read_file", {"path": "long.txt", "start_line": 495, "end_line": 600})
        self.assertIn("line 0500", tail)
        self.assertTrue(tail.endswith("[lines 495-500 of 500.]"))

    def test_short_file_is_returned_as_is(self):
        p = os.path.join(WS_DIR, "short.txt")
        with open(p, "w") as f:
            f.write("a\nb\n")
        self.assertEqual(w.execute_tool(agent(), "read_file", {"path": "short.txt"}), "a\nb\n")

    def test_bash_is_an_alias_of_shell(self):
        self.assertIn("hi", w.execute_tool(agent(), "bash", {"command": "echo hi"}))


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


class RunTaskMgr(MgrStub):
    def __init__(self):
        super().__init__()
        self.finished = []

    def finish(self, task_id, status, output="", error=""):
        self.finished.append((task_id, status, error))


class RetryTest(unittest.TestCase):
    def setUp(self):
        orig = w.llm_call
        self.addCleanup(setattr, w, "llm_call", orig)
        self.path = os.path.join(WS_DIR, "retry-state.json")

    def run_with(self, reply):
        w.llm_call = reply
        mgr = RunTaskMgr()
        out = w.run_task(mgr, agent(maxTurns=1), "sys", {"id": "t1", "title": "x", "input": "hi"})
        return out, mgr

    def test_run_task_outcomes(self):
        def broken(*a):
            raise RuntimeError("HTTP 401 from https://llm/v1/chat/completions")
        self.assertEqual(self.run_with(broken)[0], "error")
        self.assertEqual(self.run_with(lambda *a: '```json\n{"tool":"read_file","path":"x"}\n```')[0], "failed")
        out, mgr = self.run_with(lambda *a: "all good")
        self.assertEqual((out, mgr.finished[-1][1]), ("done", "done"))

    def test_errors_are_retried_then_given_up_until_restart(self):
        src = "github:o/r#pull_requests:20@abc"
        state = {"handled": [src, "other"]}
        mgr = MgrStub()
        t = {"id": "t1", "source": src}
        for attempt in range(1, w.MAX_TASK_ATTEMPTS):
            state["handled"].append(src)  # the next check re-created it
            w.after_git_task(mgr, state, self.path, t, "error")
            self.assertNotIn(src, state["handled"], f"attempt {attempt} should be retried")
        state["handled"].append(src)
        w.after_git_task(mgr, state, self.path, t, "error")
        self.assertIn(src, state["handled"])  # given up
        self.assertIn("giving up", mgr.events[-1][1])
        # A restart grants another round.
        self.assertEqual(w.reset_given_up(state), [src])
        self.assertNotIn(src, state["handled"])
        self.assertIn("other", state["handled"])
        # Success clears the count.
        w.after_git_task(mgr, state, self.path, t, "done")
        self.assertNotIn(src, state["retries"])

    def test_tasks_that_ran_and_failed_or_manual_tasks_are_not_retried(self):
        src = "github:o/r#issues:7@assigned"
        state = {"handled": [src]}
        w.after_git_task(MgrStub(), state, self.path, {"id": "t1", "source": src}, "failed")
        self.assertIn(src, state["handled"])
        w.after_git_task(MgrStub(), state, self.path, {"id": "t2"}, "error")  # manual: no source
        self.assertEqual(state.get("retries", {}), {})


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
