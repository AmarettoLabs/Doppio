"""Tests for the agent worker. Run: python -m unittest discover -s deploy/agent"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.parse

WS_DIR = tempfile.mkdtemp(prefix="doppio-ws-")
# git stash/commit need an identity (the worker sets one up in the container).
for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
             ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ.setdefault(k, v)
os.environ["AGENT_WORKSPACE"] = WS_DIR
os.environ["AGENT_MIRRORS"] = os.path.join(WS_DIR, "mirrors")
sys.path.insert(0, os.path.dirname(__file__))
import agent_worker as w  # noqa: E402

# Engine HOMEs stay in the throwaway workspace, like everything else.
w.ENGINE_HOME_ROOT = os.path.join(WS_DIR, "engine-home")

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
    """llm_call against recorded provider responses, both formats."""

    def setUp(self):
        self.orig = w.http_json
        self.addCleanup(setattr, w, "http_json", self.orig)
        self.bodies = []

    def fake(self, replies, provider="mistral", **cfg):
        a = agent(provider=provider, model="m", apiToken="k", **cfg)

        def http(method, url, headers, body=None, timeout=30):
            self.bodies.append((url, body))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        w.http_json = http
        return a

    def run_loop(self, a, system="sys", prompt="hi"):
        return w.doppio_engine(w.TaskRun(RunTaskMgr(), a, system, {"id": "t"}, prompt, WS_DIR))

    # ---- native: OpenAI-compatible ----

    def msg(self, content=None, tool_calls=None, finish=None):
        m = {"content": content}
        if tool_calls is not None:
            m["tool_calls"] = tool_calls
        return {"choices": [{"message": m, "finish_reason": finish}]}

    def call(self, cid, name, args):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    def test_native_single_call_then_answer(self):
        a = self.fake([self.msg(tool_calls=[self.call("c1", "shell", {"command": "echo hi"})]),
                       self.msg(content="all done")])
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "all done"))
        # tools were declared, the call round-trips, the result comes back as a tool message
        first, second = self.bodies
        self.assertEqual(first[1]["tools"][0]["function"]["name"], "shell")
        self.assertEqual(first[1]["messages"][0]["role"], "system")
        assistant = second[1]["messages"][-2]
        tool = second[1]["messages"][-1]
        self.assertEqual((assistant["role"], assistant["tool_calls"][0]["id"]), ("assistant", "c1"))
        self.assertEqual((tool["role"], tool["tool_call_id"], tool["content"]), ("tool", "c1", "exit=0\nhi"))

    def test_native_several_calls_in_one_reply(self):
        a = self.fake([self.msg(tool_calls=[self.call("c1", "shell", {"command": "echo one"}),
                                            self.call("c2", "shell", {"command": "echo two"})]),
                       self.msg(content="done")])
        res = self.run_loop(a)
        self.assertEqual(res.outcome, "done")
        tool_msgs = [m for m in self.bodies[1][1]["messages"] if m["role"] == "tool"]
        self.assertEqual([(m["tool_call_id"], m["content"]) for m in tool_msgs],
                         [("c1", "exit=0\none"), ("c2", "exit=0\ntwo")])

    def test_native_empty_reply_is_not_the_answer(self):
        a = self.fake([self.msg(content=""), self.msg(content="here it is")])
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "here it is"))
        # the empty assistant reply is kept as a placeholder, never sent empty
        self.assertEqual(self.bodies[1][1]["messages"][-2]["content"], "(empty reply)")

    def test_native_server_without_tool_support_falls_back_to_text(self):
        for err in (
            "HTTP 400 from https://x/v1/chat/completions: 'tools' is not supported",
            "HTTP 422 from https://x/v1/chat/completions: Unrecognized request argument: tools",
            "HTTP 400 from https://x/v1/messages: extra_forbidden: tools",
            "HTTP 400: 'tool_choice' is not supported by this model",
        ):
            with self.subTest(err=err):
                self.bodies = []
                a = self.fake([RuntimeError(err),
                               self.msg(content='```json\n{"tool":"shell","command":"echo hi"}\n```'),
                               self.msg(content="done")])
                res = self.run_loop(a)
                self.assertEqual((res.outcome, res.answer), ("done", "done"))
                self.assertNotIn("tools", self.bodies[1][1])  # fell back to the text format
                self.assertIn("```json", self.bodies[1][1]["messages"][0]["content"])  # text spec

    def test_conversation_bugs_do_not_switch_to_text(self):
        # A 400 about the messages, not the tools parameter: switching cannot
        # fix it, and the next request would fail more confusingly.
        for err in (
            "HTTP 400 from https://x/v1/chat/completions: Assistant message must have either content or tool_calls",
            "HTTP 400 from https://x/v1/messages: tool_result block(s) provided when previous message has no tool_use",
            "HTTP 400: Unexpected role 'user' after role 'tool'",
            "HTTP 500 from https://x/v1/chat/completions: tools is not supported",
        ):
            with self.subTest(err=err):
                self.bodies = []
                a = self.fake([RuntimeError(err)])
                with self.assertRaises(RuntimeError):
                    self.run_loop(a)
                self.assertIn("tools", self.bodies[0][1])  # still declared, no fallback

    def test_no_fallback_after_the_first_request(self):
        # Once native turns are in the history, a tools error is a server
        # hiccup, not a missing feature: retry later, do not switch format.
        a = self.fake([self.msg(tool_calls=[self.call("c1", "shell", {"command": "echo hi"})]),
                       RuntimeError("HTTP 400 from https://x/v1/chat/completions: 'tools' is not supported")])
        with self.assertRaises(w.NoToolSupport):
            self.run_loop(a)  # run_task turns this into an "error" to retry later
        self.assertIn("tools", self.bodies[1][1])  # no switch: tools still declared

    # ---- native: Anthropic ----

    def anthropic(self, blocks, stop=None):
        return {"content": blocks, "stop_reason": stop}

    def test_native_anthropic_tool_use_and_result(self):
        a = self.fake([self.anthropic([{"type": "text", "text": "checking"},
                                      {"type": "tool_use", "id": "tu1", "name": "shell", "input": {"command": "echo hi"}}]),
                       self.anthropic([{"type": "text", "text": "all good"}])], provider="anthropic")
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "all good"))
        url, body = self.bodies[0]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(body["tools"][0]["name"], "shell")
        self.assertEqual([t["name"] for t in body["tools"]], list(w.TOOL_DEFS))
        # the second request carries the assistant blocks and one tool_result user message
        second = self.bodies[1][1]["messages"]
        self.assertEqual(second[0]["role"], "user")  # the task prompt
        self.assertEqual(second[1]["role"], "assistant")
        self.assertEqual(second[1]["content"][1]["id"], "tu1")
        self.assertEqual(second[2]["role"], "user")
        self.assertEqual(second[2]["content"][0]["tool_use_id"], "tu1")
        self.assertEqual(second[2]["content"][0]["content"], "exit=0\nhi")

    def test_native_anthropic_no_tools_in_request_when_text_mode(self):
        a = self.fake([self.anthropic([{"type": "text", "text": "plain answer"}])], provider="anthropic")
        a = dict(a, toolCallMode="text")
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "plain answer"))
        self.assertNotIn("tools", self.bodies[0][1])

    # ---- forced modes ----

    def test_forced_text_mode_never_declares_tools(self):
        a = self.fake([self.msg(content='```json\n{"tool":"shell","command":"echo hi"}\n```'),
                       self.msg(content="done")])
        a = dict(a, toolCallMode="text")
        res = self.run_loop(a)
        self.assertEqual(res.answer, "done")
        self.assertNotIn("tools", self.bodies[0][1])

    def test_forced_native_mode_does_not_fall_back(self):
        a = self.fake([RuntimeError("HTTP 400: 'tools' is not supported")], toolCallMode="native")
        with self.assertRaises(w.NoToolSupport):
            self.run_loop(a)  # forced native: no fallback to the text format

    def test_native_model_writing_json_calls_is_run_and_switches_to_text(self):
        # The server accepts the tools but the model answers in the text format.
        call = '```json\n{"tool":"shell","command":"echo hi"}\n```'
        replies = [self.msg(content=call) for _ in range(3)] + [self.msg(content="done")]
        a = self.fake(replies)
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "done"))
        # every call ran and its result came back, including the third one
        for i in range(3):
            self.assertIn("TOOL RESULT (shell):\nexit=0\nhi", self.bodies[i + 1][1]["messages"][-1]["content"])
        # the 4th request is in the text format
        self.assertNotIn("tools", self.bodies[3][1])
        self.assertIn("```json", self.bodies[3][1]["messages"][0]["content"])

    def test_native_text_call_result_comes_back_as_a_user_message(self):
        call = '```json\n{"tool":"shell","command":"echo hi"}\n```'
        a = self.fake([self.msg(content=call), self.msg(content="done")])
        res = self.run_loop(a)
        self.assertEqual(res.outcome, "done")
        last = self.bodies[1][1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith("TOOL RESULT (shell):"))

    def test_text_mode_runs_a_native_call_the_server_returns_anyway(self):
        # Mistral sometimes returns tool_calls even though no tools were
        # declared: the call must run, not be dropped as an empty reply.
        a = self.fake([self.msg(content="", tool_calls=[self.call("c1", "shell", {"command": "echo hi"})]),
                       self.msg(content="done")], toolCallMode="text")
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "done"))
        msgs = self.bodies[1][1]["messages"]
        self.assertEqual(msgs[-2]["role"], "assistant")
        self.assertIn('"tool": "shell"', msgs[-2]["content"])  # turned into the text format
        self.assertTrue(msgs[-1]["content"].startswith("TOOL RESULT (shell):\nexit=0\nhi"))

    def test_text_mode_prefers_the_json_call_in_the_text(self):
        # A fenced call in the text wins over a stray native one.
        a = self.fake([self.msg(content='```json\n{"tool":"shell","command":"echo one"}\n```',
                                tool_calls=[self.call("c1", "shell", {"command": "echo two"})]),
                       self.msg(content="done")], toolCallMode="text")
        res = self.run_loop(a)
        self.assertEqual(res.outcome, "done")
        self.assertTrue(self.bodies[1][1]["messages"][-1]["content"].startswith("TOOL RESULT (shell):\nexit=0\none"))

    # ---- reply cap ----

    def test_truncated_replies_are_retried_then_fail(self):
        a = self.fake([self.msg(content="same", finish="length"),
                       self.msg(content="same", finish="length"),
                       self.msg(content="same", finish="length")], maxTurns=10)
        res = self.run_loop(a)
        self.assertEqual(res.outcome, "failed")
        self.assertIn("length limit", res.error)

    def test_truncated_reply_is_followed_by_a_placeholder(self):
        a = self.fake([self.msg(content="same", finish="length"), self.msg(content="done")])
        res = self.run_loop(a)
        self.assertEqual((res.outcome, res.answer), ("done", "done"))
        # Mistral rejects a user message right after a tool message, and a
        # request ending with two user messages: keep an assistant one between.
        msgs = self.bodies[1][1]["messages"]
        self.assertEqual(msgs[-2]["role"], "assistant")
        self.assertEqual(msgs[-2]["content"], "(reply cut off: too long)")
        self.assertEqual(msgs[-1]["role"], "user")

    def test_reply_length_is_capped(self):
        cases = [
            # provider, apiBase, maxReplyTokens, field, cap
            ("openai", "", 0, "max_completion_tokens", 32768),
            ("mistral", "https://api.mistral.ai/v1/", 0, "max_tokens", 32768),
            ("openai", "https://llm-server:8443/v1", 0, "max_completion_tokens", 8192),
            ("custom", "http://x/v1", 0, "max_tokens", 8192),
            ("custom", "http://x/v1", 20000, "max_tokens", 20000),
        ]
        for provider, base, cap, field, want in cases:
            a = self.fake([self.msg(content="same", finish="length") for _ in range(3)],
                          provider=provider, apiBase=base, maxReplyTokens=cap)
            res = self.run_loop(a)
            self.assertEqual(res.outcome, "failed")
            self.assertEqual(self.bodies[-1][1][field], want, (provider, base, cap))


class ToolModeTest(unittest.TestCase):
    def test_tool_spec_by_mode(self):
        a = agent(gitRepos=["o/r"])
        for native in (False, True):
            spec = w.tool_spec(a, native)
            self.assertIn("Repos you watch: o/r.", spec)
            self.assertNotIn("{repos}", spec)
            self.assertTrue(spec.startswith("\n\n"), "the spec needs a separator")
        self.assertIn("```json", w.tool_spec(a, False))
        self.assertNotIn("```json", w.tool_spec(a, True))
        self.assertIn("tools", w.tool_spec(a, True))


class RunTaskMgr(MgrStub):
    def __init__(self):
        super().__init__()
        self.finished = []

    def finish(self, task_id, status, output="", error=""):
        self.finished.append((task_id, status, error))


class FakeVibe:
    """A fake `vibe` binary: records its argv and env, prints a scripted
    stream of JSON entries on stdout, and writes an export.json like the real
    one. `script` is a list of (lines, exit_code, export) steps."""

    def __init__(self, tmp, steps):
        self.dir = os.path.join(tmp, "bin")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "vibe")
        self.steps = steps
        self.calls = []
        with open(self.path, "w") as f:
            f.write("#!/bin/sh\n")
            f.write("python3 \"$FAKE_VIBE_SCRIPT\" \"$@\"\n")
        os.chmod(self.path, 0o755)
        self.script = os.path.join(tmp, "fake_vibe.py")
        with open(self.script, "w") as f:
            f.write("import json, os, sys\n")
            f.write("calls = os.environ['FAKE_VIBE_CALLS']\n")
            f.write("with open(calls, 'a') as fh:\n")
            f.write("    fh.write(json.dumps({'argv': ['vibe'] + sys.argv[1:], 'cwd': os.getcwd(),\n")
            f.write("                         'key': os.environ.get('MISTRAL_API_KEY'),\n")
            f.write("                         'vibe_home': os.environ.get('VIBE_HOME'),\n")
            f.write("                         'gh_token': os.environ.get('GH_TOKEN')}) + '\\n')\n")
            for lines, code, export in steps:
                f.write("for line in %r:\n    print(line, flush=True)\n" % (lines,))
                if export is not None:
                    f.write("import pathlib\n")
                    f.write("out = [a for a in sys.argv[1:] if a.startswith('--output-dir')]\n")
                    f.write("d = pathlib.Path(out[0][len('--output-dir='):] if '=' in out[0] else sys.argv[sys.argv.index('--output-dir') + 1])\n")
                    f.write("d.mkdir(parents=True, exist_ok=True)\n")
                    f.write("(d / 'export.json').write_text(json.dumps(%r))\n" % (export,))
                f.write("sys.exit(%d)\n" % (code,))
        os.environ["FAKE_VIBE_SCRIPT"] = self.script
        os.environ["FAKE_VIBE_CALLS"] = os.path.join(tmp, "calls.jsonl")
        self.calls_path = os.environ["FAKE_VIBE_CALLS"]

    def use(self):
        orig = os.environ["PATH"]
        os.environ["PATH"] = self.dir + os.pathsep + orig
        return lambda: os.environ.update(PATH=orig)

    def recorded(self):
        if not os.path.exists(self.calls_path):
            return []
        with open(self.calls_path) as f:
            return [json.loads(l) for l in f if l.strip()]


def vibe_run(mgr, tmp, agent_cfg=None, task=None, system="sys persona"):
    """Run the vibe engine on one task with a fake binary; returns (result,
    events, recorded call)."""
    if os.environ.get("FAKE_VIBE_CALLS") and os.path.exists(os.environ["FAKE_VIBE_CALLS"]):
        os.unlink(os.environ["FAKE_VIBE_CALLS"])
    a = agent(engine="vibe", **(agent_cfg or {}))
    t = task or {"id": "t9", "kind": "assigned", "title": "x", "input": "do it",
                 "source": "github:o/r#issues:7@assigned"}
    run = w.TaskRun(mgr, a, system, t, t["input"], WS_DIR, minutes=5)
    res = w.vibe_engine(run)
    calls = []
    p = os.environ.get("FAKE_VIBE_CALLS")
    if p and os.path.exists(p):
        with open(p) as f:
            calls = [json.loads(l) for l in f if l.strip()]
    return res, mgr.events, (calls[-1] if calls else None)


class VibeEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doppio-vibe-")
        self.mgr = RunTaskMgr()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # No leftover fake from another test: the real `vibe` must not run here.
        for k in ("FAKE_VIBE_SCRIPT", "FAKE_VIBE_CALLS"):
            os.environ.pop(k, None)
        # The engine environment is a whitelist now; the fake vibe harness
        # is shell on the other side of it.
        p = unittest.mock.patch.object(w, "ENGINE_ENV_ALLOW",
                                       w.ENGINE_ENV_ALLOW + ("FAKE_VIBE_CALLS", "FAKE_VIBE_SCRIPT"))
        p.start()
        self.addCleanup(p.stop)
        # Vibe's home and the run files go to temp dirs, never the real ones.
        for name, sub in (("VIBE_HOME", "vibe-home"), ("VIBE_RUN_DIR", "vibe-runs"),
                          ("VIBE_STATE", "ws-vibe-home")):
            p = unittest.mock.patch.object(w, name, os.path.join(self.tmp, sub))
            p.start()
            self.addCleanup(p.stop)

    def fake(self, steps):
        fv = FakeVibe(self.tmp, steps)
        self.addCleanup(fv.use())
        return fv

    def test_argv_prompt_and_env(self):
        fv = self.fake([([
            '{"type":"message","role":"assistant","content":[{"type":"text","text":"all good"}]}',
        ], 0, {"outcome": "finished", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                 "total_tokens": 15}, "cost_usd": 0.01, "steps": 2})])
        res, events, call = vibe_run(self.mgr, self.tmp,
                                     agent_cfg={"accountToken": "vibe-key", "model": "mistral-large-4",
                                                "maxTurns": 7, "taskMinutes": 5, "gitToken": "gt"})
        argv = call["argv"]
        self.assertEqual(argv[0], "vibe")
        self.assertIn("--prompt-file", argv)
        self.assertEqual(argv[argv.index("--output") + 1], "streaming")
        self.assertEqual(argv[argv.index("--agent") + 1], "auto-approve")
        self.assertEqual(argv[argv.index("--workdir") + 1], WS_DIR)
        self.assertEqual(argv[argv.index("--model") + 1], "mistral-large-4")
        self.assertIn("--output-dir", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        self.assertIn("--time-limit", argv)
        # The prompt file holds persona + standing task + the worker's prompt.
        prompt_file = argv[argv.index("--prompt-file") + 1]
        with open(prompt_file) as f:
            text = f.read()
        self.assertTrue(text.startswith("sys persona\n\n"))
        self.assertIn("do it", text)
        self.assertNotIn("--system-prompt-file", " ".join(argv))
        # Auth: the account's key, in the env and in Vibe's own ~/.vibe/.env.
        self.assertEqual(call["key"], "vibe-key")
        env_file = os.path.join(w.VIBE_HOME, ".env")
        with open(env_file) as f:
            self.assertEqual(f.read().strip(), "MISTRAL_API_KEY=vibe-key")
        self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
        # Vibe keeps its home, so the image's config.toml (no update checks)
        # still applies.
        self.assertIsNone(call["vibe_home"])
        self.assertEqual(call["gh_token"], "gt")
        # Outcome, answer and usage.
        self.assertEqual((res.outcome, res.answer), ("done", "all good"))
        self.assertIn(("assistant", "all good"), events)
        self.assertTrue(any("vibe usage: 15 tokens (10 in / 5 out), $0.0100, 2 steps" in c
                            for _, c in events), events)

    def test_streamed_tool_calls_and_results(self):
        self.fake([([
            '{"type":"effect","title":"shell","detail":{"tool_name":"shell","display":{"summary":"ls -la","verb":"Running"}},"state":{"status":"running"}}',
            '{"type":"effect","title":"shell","detail":{"tool_name":"shell","display":{"summary":"ls -la"}},"state":{"status":"completed","output_text":"a.txt\\nb.txt","display":{"message":"2 files"}}}',
            '{"type":"effect","title":"shell","detail":{"tool_name":"shell","display":{"summary":"rm -rf /"}},"state":{"status":"failed","error":{"message":"denied"},"output_text":""}}',
            '{"type":"reasoning","text":"thinking hard"}',
            '{"type":"notice","level":"warning","message":"a config file failed to load"}',
            '{"type":"message","role":"assistant","content":[{"type":"text","text":"done: two files"}]}',
        ], 0, {"outcome": "finished"})])
        res, events, _ = vibe_run(self.mgr, self.tmp)
        kinds = [(t, c) for t, c in events if t in ("action", "log", "assistant")]
        self.assertIn(("action", "Running ls -la"), kinds)
        self.assertIn(("log", "→ a.txt\nb.txt"), kinds)  # output_text, kept as printed
        self.assertIn(("log", "→ denied"), kinds)  # a failed call logs its error
        self.assertIn(("log", "thinking: thinking hard"), kinds)
        self.assertIn(("log", "vibe: a config file failed to load"), kinds)
        self.assertEqual((res.outcome, res.answer), ("done", "done: two files"))

    def test_outcomes(self):
        cases = [
            ({"outcome": "finished"}, 0, "failed"),  # no final message streamed
            ({"outcome": "turn_limit", "error": None}, 3, "failed"),
            ({"outcome": "token_limit"}, 3, "failed"),
            ({"outcome": "length"}, 3, "failed"),
            ({"outcome": "refusal"}, 3, "failed"),
            ({"outcome": "price_limit"}, 3, "limit"),
            ({"outcome": "infrastructure_failure", "error": {"message": "HTTP 500 from the API"}}, 2, "error"),
            ({"outcome": "config_error", "error": {"message": "Invalid API key", "code": "invalid_api_key"}}, 1, "error"),
            ({"outcome": "deadline"}, 3, "error"),
            ({"outcome": "terminated"}, 3, "error"),
            ({"outcome": "usage_error", "error": {"message": "Unknown model"}}, 1, "failed"),
            ({}, 2, "error"),       # crashed before writing an export
            ({}, 0, "failed"),     # exit 0 without an export: no usable answer
        ]
        for export, code, want in cases:
            with self.subTest(export=export, code=code):
                self.fake([([], code, export)])
                res, events, _ = vibe_run(self.mgr, self.tmp)
                self.assertEqual(res.outcome, want)
                if want != "done":
                    self.assertTrue(res.error, (export, code))

    def test_error_message_from_export_then_stderr(self):
        self.fake([([], 2, {"outcome": "infrastructure_failure",
                            "error": {"message": "connection refused"}})])
        res, _, _ = vibe_run(self.mgr, self.tmp)
        self.assertEqual(res.error, "connection refused")
        # No export (and a fresh out dir): fall back to the last stderr line.
        self.fake([([], 2, None)])
        res, _, _ = vibe_run(self.mgr, self.tmp, task={"id": "t10", "title": "x", "input": "hi"})
        self.assertIn("vibe exited 2", res.error)

    def test_finished_without_an_answer_fails(self):
        self.fake([([], 0, {"outcome": "finished"})])
        res, _, _ = vibe_run(self.mgr, self.tmp)
        self.assertEqual(res.outcome, "failed")
        self.assertIn("without a final message", res.error)

    def test_time_limit_kills_vibe(self):
        # A fake vibe that never exits: run_process kills it at the deadline.
        self.fake([(["import time\nwhile True: time.sleep(1)\n".replace("import", "#")], 0, None)])
        script = os.path.join(self.tmp, "slow.py")
        with open(script, "w") as f:
            f.write("import sys, time\nprint('starting', flush=True)\ntime.sleep(60)\nsys.exit(0)\n")
        os.environ["FAKE_VIBE_SCRIPT"] = script
        a = agent(engine="vibe")
        run = w.TaskRun(self.mgr, a, "sys", {"id": "t9", "title": "x", "input": "slow"}, "slow",
                       WS_DIR, minutes=5)
        run.deadline = time.monotonic() - 1  # as if the limit had passed
        with self.assertRaises(w.TaskTimeout):
            w.vibe_engine(run)

    def test_playbook_uses_gh_for_cli_engines(self):
        self.use = lambda routes: None
        # The assigned-issues playbook for a vibe agent names gh/glab.
        text = w.PLAYBOOKS["assigned_issues"].format(
            repo="o/r", repo_dir="/ws/r", base="main", num=7, branch="bot/7-x",
            existing="", fork="",
            pr_line=w.CLI_PR_LINE.format(repo="o/r", branch="bot/7-x", base="main", num=7))
        self.assertIn("gh pr create", text)
        self.assertIn("glab mr create", text)
        self.assertIn("Closes #7", text)
        self.assertNotIn("with create_pr", text)
        # The Doppio loop keeps its create_pr tool.
        text = w.PLAYBOOKS["assigned_issues"].format(
            repo="o/r", repo_dir="/ws/r", base="main", num=7, branch="bot/7-x",
            existing="", fork="",
            pr_line="open one with create_pr (repo {repo}, head `{branch}`, base `{base}`) whose body "
                    "explains the change and ends with `Closes #{num}`".format(
                        repo="o/r", branch="bot/7-x", base="main", num=7))
        self.assertIn("create_pr", text)

    def test_vibe_engine_needs_a_login(self):
        out = w.run_task(self.mgr, agent(engine="vibe"), "sys",
                         {"id": "t1", "title": "x", "input": "hi"})
        self.assertEqual(out, "failed")
        self.assertIn("needs a login", self.mgr.finished[-1][2])

    def test_vibe_is_a_registered_engine(self):
        self.assertIs(w.ENGINES["vibe"], w.vibe_engine)

    def test_model_alias_defaults_and_passes_through(self):
        self.assertEqual(w.vibe_model_alias({"model": "codestral-2"}), "codestral-2")
        self.assertEqual(w.vibe_model_alias({"model": "custom/thing"}), "custom/thing")
        self.assertIsNone(w.vibe_model_alias({}))

    def test_no_model_leaves_vibe_its_default(self):
        self.fake([(['{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'],
                    0, {"outcome": "finished"})])
        _, _, call = vibe_run(self.mgr, self.tmp, agent_cfg={"model": ""})
        self.assertNotIn("--model", call["argv"])

    def picky_vibe(self, learns):
        """A fake vibe that refuses --model like the real one does for a model
        missing from its config. learns=True: a run without --model "fetches
        the plan's list" (a marker file), after which --model is accepted."""
        script = os.path.join(self.tmp, "picky.py")
        marker = os.path.join(self.tmp, "plan-models-cached")
        with open(script, "w") as f:
            f.write(
                "import json, os, pathlib, sys\n"
                "a = sys.argv[1:]\n"
                "with open(os.environ['FAKE_VIBE_CALLS'], 'a') as fh:\n"
                "    fh.write(json.dumps({'argv': ['vibe'] + a}) + '\\n')\n"
                "d = pathlib.Path(a[a.index('--output-dir') + 1]); d.mkdir(parents=True, exist_ok=True)\n"
                f"marker = {marker!r}\n"
                "if '--model' in a and not os.path.exists(marker):\n"
                "    (d / 'export.json').write_text(json.dumps({'outcome': 'usage_error', 'error': {'message':\n"
                "        'Unknown model: mistral-large-4. Available: mistral-medium-3.5'}}))\n"
                "    sys.exit(1)\n"
                f"if {learns!r} and '--model' not in a:\n"
                "    open(marker, 'w').close()\n"
                "prompt = open(a[a.index('--prompt-file') + 1]).read()\n"
                "text = 'OK' if 'single word OK' in prompt else ('large' if '--model' in a else 'medium')\n"
                "print(json.dumps({'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}))\n"
                "(d / 'export.json').write_text(json.dumps({'outcome': 'finished'}))\n")
        self.fake([([], 0, None)])
        os.environ["FAKE_VIBE_SCRIPT"] = script

    def calls(self):
        with open(os.environ["FAKE_VIBE_CALLS"]) as f:
            return [json.loads(l)["argv"] for l in f if l.strip()]

    def test_unknown_model_is_learned_by_a_warmup_run(self):
        self.picky_vibe(learns=True)
        res, events, _ = vibe_run(self.mgr, self.tmp, agent_cfg={"model": "mistral-large-4"})
        argvs = self.calls()
        # Refused, warm-up without --model, then the task on the agent's model.
        self.assertEqual(["--model" in a for a in argvs], [True, False, True])
        self.assertEqual((res.outcome, res.answer), ("done", "large"))
        self.assertTrue(any("warm-up" in c for _, c in events), events)
        # The warm-up runs outside the checkout, with a short time limit.
        warm = argvs[1]
        self.assertNotEqual(warm[warm.index("--workdir") + 1], WS_DIR)
        self.assertLessEqual(int(warm[warm.index("--time-limit") + 1]), w.WARMUP_SECONDS)
        self.assertFalse(any(k == "error" for k, _ in events), events)
        # The warm-up's own reply never reaches the portal.
        self.assertFalse(any(c == "OK" for _, c in events), events)

    def test_model_still_unknown_runs_on_vibes_default_and_flags_it(self):
        self.picky_vibe(learns=False)
        res, events, _ = vibe_run(self.mgr, self.tmp, agent_cfg={"model": "mistral-large-4"})
        argvs = self.calls()
        # Refused, warm-up, refused again, then Vibe's default model.
        self.assertEqual(["--model" in a for a in argvs], [True, False, True, False])
        self.assertEqual((res.outcome, res.answer), ("done", "medium"))
        self.assertTrue(any(k == "error" and "does not know the model 'mistral-large-4'" in c
                            for k, c in events), events)

    def test_vibe_home_is_kept_in_the_workspace(self):
        home, state = w.VIBE_HOME, w.VIBE_STATE
        # A previous container left a cached model list, an old config and a
        # key file from a task that was killed.
        os.makedirs(state)
        for name, text in (("experiment_eval_cache.json", "plan"), ("config.toml", "old"),
                            (".env", "MISTRAL_API_KEY=stale\n")):
            with open(os.path.join(state, name), "w") as f:
                f.write(text)
        # The new container's image ships ~/.vibe with its config.
        os.makedirs(os.path.join(home, "logs"))
        for name, text in (("config.toml", "image"), ("experiment_eval_cache.json", "empty")):
            with open(os.path.join(home, name), "w") as f:
                f.write(text)
        w.persist_vibe_home()
        self.assertTrue(os.path.islink(home))
        read = lambda n: open(os.path.join(home, n)).read()
        self.assertEqual(read("config.toml"), "image")               # the image's config wins
        self.assertEqual(read("experiment_eval_cache.json"), "plan")  # the cache survives
        self.assertTrue(os.path.isdir(os.path.join(home, "logs")))
        self.assertFalse(os.path.exists(os.path.join(home, ".env")))  # a stale key never reaches the engine
        w.persist_vibe_home()  # idempotent
        self.assertTrue(os.path.islink(home))
        self.assertFalse(os.path.exists(os.path.join(state, ".env")))

    def test_nothing_is_written_into_the_repository(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        self.fake([(['{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'],
                    0, {"outcome": "finished"})])
        a = agent(engine="vibe", accountToken="vibe-key")
        t = {"id": "t11", "title": "x", "input": "do it"}
        run = w.TaskRun(self.mgr, a, "sys", t, t["input"], repo, minutes=5)
        self.assertEqual(w.vibe_engine(run).outcome, "done")
        self.assertEqual(os.listdir(repo), [])

    def test_vibe_env_without_a_key_writes_no_env_file(self):
        run = w.TaskRun(self.mgr, agent(engine="vibe"), "sys",
                        {"id": "t1", "title": "x", "input": "hi"}, "hi", WS_DIR)
        env = w.vibe_env(run)
        self.assertNotIn("MISTRAL_API_KEY", env)
        self.assertNotIn("VIBE_HOME", env)
        self.assertFalse(os.path.exists(os.path.join(w.VIBE_HOME, ".env")))

    def test_key_file_is_written_for_push_tasks_only_and_goes_with_the_task(self):
        os.makedirs(w.VIBE_STATE)
        w.persist_vibe_home()  # ~/.vibe → Vibe's shared home, as in the container
        key = "vibe-key"
        # A task that may push: the key file exists, the way `vibe --setup`
        # saves a login, and the env var carries the same key.
        a = agent(engine="vibe", accountToken=key, gitToken="gt")
        t = {"id": "t-p", "kind": "assigned", "title": "x", "input": "i",
             "source": "github:o/r#issues:7@assigned"}
        run = w.TaskRun(self.mgr, a, "sys", t, t["input"], WS_DIR)
        env = w.vibe_env(run)
        self.assertEqual(env["MISTRAL_API_KEY"], key)
        self.assertEqual(open(os.path.join(w.VIBE_HOME, ".env")).read(), f"MISTRAL_API_KEY={key}\n")
        w.cleanup_engine_home(run)
        self.assertFalse(os.path.exists(os.path.join(w.VIBE_STATE, ".env")))
        # Reviews and mentions: the env var still logs the engine in, but no
        # key file anywhere the engine user could keep reading.
        t = {"id": "t-r", "kind": "review", "title": "x", "input": "i",
             "source": "github:o/r#pull_requests:8@abc"}
        run = w.TaskRun(self.mgr, a, "sys", t, t["input"], WS_DIR)
        env = w.vibe_env(run)
        self.assertEqual(env["MISTRAL_API_KEY"], key)
        self.assertFalse(os.path.exists(os.path.join(w.VIBE_HOME, ".env")))

    def test_review_task_gets_no_git_credentials(self):
        self.fake([(['{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'],
                    0, {"outcome": "finished"})])
        task = {"id": "t-rev", "kind": "review", "title": "x", "input": "do it",
                "source": "github:o/r#pull_requests:8@abc"}
        res, _, call = vibe_run(self.mgr, self.tmp,
                                agent_cfg={"gitToken": "gt", "accountToken": "k"}, task=task)
        self.assertEqual((res.outcome, res.answer), ("done", "ok"))
        self.assertIsNone(call["gh_token"])  # only the engine's own login is passed
        self.assertEqual(call["key"], "k")
        # No account key file either: the env var alone authenticates it.
        self.assertFalse(os.path.exists(os.path.join(w.VIBE_HOME, ".env")))


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
        self.assertEqual(seen, {"prompt": w.CHAT_PLAYBOOK + "\n\nThe message:\ndo it", "system": "sys",
                                "workdir": WS_DIR, "minutes": 5, "turns": None})
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

    def test_review_task_answer_is_submitted_as_a_review(self):
        calls = []
        for name, fn in (("post_review", lambda a, src, ans: calls.append(("review", src, ans)) or "APPROVE"),
                         ("post_comment", lambda a, src, ans: calls.append(("comment", src, ans))),
                         ("prepare_task", lambda a, t: "context")):
            orig = getattr(w, name)
            setattr(w, name, fn)
            self.addCleanup(setattr, w, name, orig)
        w.ENGINES["fake"] = lambda run: w.EngineResult("done", answer="ok\nVERDICT: approve")
        for kind in ("review", "mention"):
            src = "github:o/r#pull_requests:5@x"
            w.run_task(self.mgr, agent(engine="fake"), "sys",
                       {"id": "t-" + kind, "kind": kind, "title": "x", "input": "go", "source": src})
        self.assertEqual([c[0] for c in calls], ["review", "comment"])

    def test_portal_messages_get_the_chat_playbook_and_git_tasks_do_not(self):
        prompts = []
        orig = w.prepare_task
        w.prepare_task = lambda a, t: "GIT CONTEXT"
        self.addCleanup(setattr, w, "prepare_task", orig)
        for name in ("post_comment", "post_review"):
            orig_fn = getattr(w, name)
            setattr(w, name, lambda *a: "COMMENT")
            self.addCleanup(setattr, w, name, orig_fn)
        w.ENGINES["fake"] = lambda run: prompts.append(run.prompt) or w.EngineResult("done", answer="hi")
        w.run_task(self.mgr, agent(engine="fake"), "sys", {"id": "c", "kind": "manual", "title": "Hello", "input": "Hello"})
        w.run_task(self.mgr, agent(engine="fake"), "sys", {"id": "m", "kind": "mention", "title": "x", "input": "ctx",
                                                           "source": "github:o/r#issues:3@t"})
        self.assertTrue(prompts[0].startswith(w.CHAT_PLAYBOOK) and prompts[0].endswith("The message:\nHello"))
        self.assertIn("Do not go looking for assigned issues", prompts[0])
        self.assertIn("Do not read credentials", prompts[0])
        self.assertEqual(prompts[1], "GIT CONTEXT\n\nctx")

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


class ExtraCaTest(unittest.TestCase):
    """setup_extra_ca: the extra bundle is trusted by everything the worker and
    its shell tool run, including the Node-based CLIs."""

    def test_sets_the_ca_env_for_python_git_curl_and_node(self):
        live = w.CA_BUNDLE
        live_before = open(live, "rb").read() if os.path.exists(live) else None
        with tempfile.TemporaryDirectory() as tmp:
            extra = os.path.join(tmp, "extra-ca.pem")
            bundle = os.path.join(tmp, "bundle.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", os.devnull, "-out", extra, "-days", "1",
                            "-subj", "/CN=doppio-test"],
                           check=True, capture_output=True)
            old_ctx = w._ssl_ctx
            self.addCleanup(setattr, w, "_ssl_ctx", old_ctx)
            # patch.dict restores the environment on exit.
            with unittest.mock.patch.dict(os.environ, {"DOPPIO_EXTRA_CA": extra}):
                w.setup_extra_ca(bundle)
                for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                          "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"):
                    self.assertEqual(os.environ[k], bundle, k)
                self.assertIsNotNone(w._ssl_ctx)
                with open(bundle) as f, open(extra) as e:
                    self.assertTrue(f.read().endswith(e.read()))
        # The live bundle of a running worker is never touched.
        self.assertEqual(open(live, "rb").read() if os.path.exists(live) else None, live_before)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(WS_DIR, "long.txt")
        with open(self.path, "w") as f:
            f.write("".join(f"line {i:04d} " + "x" * 40 + "\n" for i in range(1, 501)))

    def run_obj(self, kind="assigned"):
        t = {"id": "t-" + (kind or "plain"), "kind": kind, "title": "x", "input": "i",
             "source": "github:o/r#issues:7@assigned"}
        return w.TaskRun(MgrStub(), agent(), "sys", t, "p", WS_DIR)

    def test_read_file_in_parts(self):
        run = self.run_obj()
        out = w.execute_tool(run, "read_file", {"path": "long.txt"})
        self.assertIn("line 0001", out)
        self.assertRegex(out, r"\[lines 1-(\d+) of 500\. Read more with start_line=\d+\.\]$")
        nxt = int(re.search(r"start_line=(\d+)", out).group(1))
        out2 = w.execute_tool(run, "read_file", {"path": "long.txt", "start_line": nxt})
        self.assertTrue(out2.startswith(f"line {nxt:04d}"))
        tail = w.execute_tool(run, "read_file", {"path": "long.txt", "start_line": 495, "end_line": 600})
        self.assertIn("line 0500", tail)
        self.assertTrue(tail.endswith("[lines 495-500 of 500.]"))

    def test_short_file_is_returned_as_is(self):
        p = os.path.join(WS_DIR, "short.txt")
        with open(p, "w") as f:
            f.write("a\nb\n")
        self.assertEqual(w.execute_tool(self.run_obj(), "read_file", {"path": "short.txt"}), "a\nb\n")

    def test_bash_is_an_alias_of_shell(self):
        self.assertIn("hi", w.execute_tool(self.run_obj(), "bash", {"command": "echo hi"}))

    def test_shell_runs_through_the_user_drop(self):
        seen = []
        orig = w.engine_argv
        w.engine_argv = lambda argv: seen.append(argv) or argv
        self.addCleanup(setattr, w, "engine_argv", orig)
        w.execute_tool(self.run_obj(), "shell", {"command": "echo hi"})
        self.assertIn(["sh", "-c", "echo hi"], seen)

    def test_write_tools_are_refused_on_reviews_and_mentions(self):
        for kind in ("review", "mention"):
            for tool in ("write_file", "create_pr"):
                out = w.execute_tool(self.run_obj(kind), tool,
                                     {"path": "a.txt", "content": "x"}) if tool == "write_file" else \
                                     w.execute_tool(self.run_obj(kind), tool,
                                                    {"repo": "o/r", "head": "b", "base": "main",
                                                     "title": "t", "body": "b"})
                self.assertIn(tool, out)
                self.assertIn("not available for this task", out)

    def test_write_file_still_works_on_assigned_tasks(self):
        run = w.TaskRun(MgrStub(), agent(), "sys", {"id": "t-w"}, "p", WS_DIR)
        for path in ("tools-written.txt", os.path.join("nested", "tools-written.txt")):
            out = w.execute_tool(run, "write_file", {"path": path, "content": "ok"})
            self.assertIn("wrote 2 bytes", out)
            p = os.path.join(WS_DIR, path)
            with open(p) as f:
                self.assertEqual(f.read(), "ok")
            # At the workspace root too: the file must be the engine user's,
            # or its own shell cannot edit it.
            ids = w.engine_uid_gid()
            if ids:
                self.assertEqual(os.stat(p).st_uid, ids[0], path)


class EngineUserTest(unittest.TestCase):
    """The worker hands the engine per-task secrets: nothing on reviews and
    mentions, the git token on assigned issues and PRs."""

    def task_run(self, kind, **cfg):
        t = {"id": "t-" + (kind or "plain").lower(), "kind": kind, "title": "x", "input": "i",
             "source": "github:o/r#issues:7@assigned"}
        return w.TaskRun(MgrStub(), agent(**cfg), "sys", t, "p", WS_DIR)

    def test_engine_argv_drops_to_the_unprivileged_user(self):
        orig = w.engine_uid_gid
        w.engine_uid_gid = lambda: (1000, 1000)
        self.addCleanup(setattr, w, "engine_uid_gid", orig)
        self.assertEqual(w.engine_argv(["sh", "-c", "ls"]),
                         ["setpriv", "--reuid=1000", "--regid=1000", "--init-groups",
                          "--", "sh", "-c", "ls"])

    def test_engine_argv_is_plain_without_a_drop(self):
        orig = w.engine_uid_gid
        w.engine_uid_gid = lambda: None
        self.addCleanup(setattr, w, "engine_uid_gid", orig)
        self.assertEqual(w.engine_argv(["sh", "-c", "ls"]), ["sh", "-c", "ls"])

    def test_review_and_mention_tasks_are_readonly(self):
        for kind in ("review", "mention"):
            self.assertTrue(self.task_run(kind).readonly)
        for kind in ("assigned", "manual", None):
            self.assertFalse(self.task_run(kind).readonly)

    def test_engine_env_has_the_git_token_only_on_assigned_tasks(self):
        for kind, want in (("assigned", "t"), ("review", None), ("mention", None)):
            env = w.engine_env(self.task_run(kind))
            self.assertEqual(env.get("GIT_TOKEN"), want, kind)

    def test_engine_env_leaves_the_agent_secrets_paths_out(self):
        with unittest.mock.patch.dict(os.environ, {"AGENT_CONFIG": "/agent/state/config.json",
                                                   "AGENT_ACCOUNT": "/agent/state/account.json",
                                                   "A_SECRET_HANDED_TO_THE_WORKER": "hush",
                                                   "LC_ALL": "C.UTF-8"}):
            env = w.engine_env(self.task_run("assigned"))
        self.assertNotIn("AGENT_CONFIG", env)
        self.assertNotIn("AGENT_ACCOUNT", env)
        self.assertNotIn("A_SECRET_HANDED_TO_THE_WORKER", env)  # a whitelist, not a deny list
        self.assertTrue(env["HOME"].startswith(w.ENGINE_HOME_ROOT))
        for k in ("PATH", "LC_ALL"):
            self.assertIn(k, env)  # what the engines run on comes through

    def test_chown_to_engine_never_follows_the_engines_symlinks(self):
        calls = []
        orig_ids, orig_run = w.engine_uid_gid, w.subprocess.run
        w.engine_uid_gid = lambda: (1000, 1000)
        w.subprocess.run = lambda argv, **kw: calls.append(argv) or None
        self.addCleanup(setattr, w, "engine_uid_gid", orig_ids)
        self.addCleanup(setattr, w.subprocess, "run", orig_run)
        w.chown_to_engine(os.path.join(WS_DIR, "repos", "r"))
        self.assertEqual(calls, [["chown", "-R", "-h", "1000:1000", os.path.join(WS_DIR, "repos", "r")]])

    def test_engine_home_holds_credentials_only_when_the_task_may_push(self):
        home = self.task_run("assigned").engine_home()
        gitconfig = open(os.path.join(home, ".gitconfig")).read()
        self.assertIn("helper = store", gitconfig)
        with open(os.path.join(home, ".git-credentials")) as f:
            self.assertIn("x-access-token:t@github.com", f.read())
        self.assertEqual(os.stat(os.path.join(home, ".git-credentials")).st_mode & 0o777, 0o600)
        # Reviews and mentions: no credentials anywhere in the home.
        home = self.task_run("review").engine_home()
        self.assertFalse(os.path.exists(os.path.join(home, ".git-credentials")))
        self.assertNotIn("helper = store", open(os.path.join(home, ".gitconfig")).read())

    def test_cleanup_removes_the_home_and_its_credentials(self):
        run = self.task_run("assigned")
        home = run.engine_home()
        self.assertTrue(os.path.exists(os.path.join(home, ".git-credentials")))
        w.cleanup_engine_home(run)
        self.assertFalse(os.path.exists(home))
        w.cleanup_engine_home(run)  # twice: no error

    def test_can_push_needs_a_source(self):
        self.assertFalse(w.can_push({"kind": "assigned"}))
        self.assertTrue(w.can_push({"kind": "assigned", "source": "github:o/r#issues:7@x"}))
        self.assertFalse(w.can_push({"kind": "assigned", "source": ""}))


class SetupGitTest(unittest.TestCase):
    def config(self, home):
        out = subprocess.run(["git", "config", "--global", "--list"], check=True,
                             capture_output=True, text=True, env=dict(os.environ, HOME=home))
        return out.stdout

    def test_a_token_configures_identity_and_credentials(self):
        with tempfile.TemporaryDirectory() as home:
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                w.setup_git(agent())
            cfg = self.config(home)
            self.assertIn("user.name=bot", cfg)
            self.assertIn("credential.helper=store --file=", cfg)
            cred = os.path.join(home, ".git-credentials")
            self.assertIn("x-access-token:t@github.com", open(cred).read())

    def test_without_a_token_no_credential_store_is_configured(self):
        with tempfile.TemporaryDirectory() as home:
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                w.setup_git(agent(gitToken=""))
            cfg = self.config(home)
            self.assertNotIn("credential.helper", cfg)
            self.assertNotIn("user.name=", cfg)
            self.assertFalse(os.path.exists(os.path.join(home, ".git-credentials")))
            self.assertIn("init.defaultbranch=main", cfg)  # the one unconditional setting



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


class ReviewVerdictTest(Patched):
    SRC = "github:o/r#pull_requests:31@abc"

    def test_split_verdict(self):
        self.assertEqual(w.split_verdict("Looks good.\n\nVERDICT: approve"), ("Looks good.", "APPROVE"))
        self.assertEqual(w.split_verdict("Fix x.\n**Verdict:** request changes — see above"), ("Fix x.", "REQUEST_CHANGES"))
        self.assertEqual(w.split_verdict("A question.\nverdict: comment"), ("A question.", "COMMENT"))
        # No verdict: a plain comment review, body untouched.
        self.assertEqual(w.split_verdict("Just notes.\n"), ("Just notes.", "COMMENT"))
        # The last verdict line wins; earlier ones are kept in the body.
        body, event = w.split_verdict("VERDICT: request changes was my first take.\nNow fine.\nVERDICT: approve")
        self.assertEqual(event, "APPROVE")
        self.assertIn("first take", body)

    def test_only_the_text_after_the_review_line_is_posted(self):
        answer = ("Spot check passed. Review done.\n\nREVIEW:\nLooks good: the fix is tested.\n\n"
                  "- `a.py:3` (nit): a typo.\n\nVERDICT: approve")
        self.assertEqual(w.split_verdict(answer),
                         ("Looks good: the fix is tested.\n\n- `a.py:3` (nit): a typo.", "APPROVE"))
        # Text on the marker line itself is kept; markdown around the marker is fine.
        self.assertEqual(w.split_verdict("notes\n**REVIEW:** Fine.\nVERDICT: approve"), ("Fine.", "APPROVE"))
        # The last marker wins.
        self.assertEqual(w.split_verdict("REVIEW: draft\nREVIEW:\nFinal.\nVERDICT: comment"),
                         ("Final.", "COMMENT"))
        # Only an upper-case marker line counts; prose mentioning a review doesn't.
        self.assertEqual(w.split_verdict("Review: the code is fine.\nVERDICT: approve"),
                         ("Review: the code is fine.", "APPROVE"))
        # A marker with nothing after it doesn't throw the review away.
        self.assertEqual(w.split_verdict("All good.\nREVIEW:\nVERDICT: approve"),
                         ("All good.", "APPROVE"))

    def test_review_is_submitted_with_the_verdict(self):
        gh = self.use({r"/repos/o/r/pulls/31/reviews": {"id": 1}})
        self.assertEqual(w.post_review(agent(), self.SRC, "All good.\nVERDICT: approve"), "APPROVE")
        self.assertEqual(gh.calls, [("POST", API + "/repos/o/r/pulls/31/reviews",
                                     {"body": "All good.", "event": "APPROVE"})])

    def test_refused_approval_falls_back_to_a_comment_review(self):
        def reviews(path, calls=[]):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("HTTP 422: Can not approve your own pull request")
            return {"id": 2}
        gh = self.use({r"/repos/o/r/pulls/31/reviews": reviews})
        self.assertEqual(w.post_review(agent(), self.SRC, "Fine.\nVERDICT: approve"), "COMMENT")
        self.assertEqual([c[2]["event"] for c in gh.calls], ["APPROVE", "COMMENT"])

    def test_review_playbook_asks_for_a_verdict(self):
        self.assertIn("VERDICT: approve", w.PLAYBOOKS["review_requests"])
        self.assertIn("VERDICT: request changes", w.PLAYBOOKS["review_requests"])
        self.assertIn("`REVIEW:`", w.PLAYBOOKS["review_requests"])


class StatusLabelTest(Patched):
    SRC = "github:o/r#issues:84@2026-11-17T03:26:49Z"
    PALAZZO = ["bug", "question", "ready", "in-progress", "needs-review", "blocked", "on-hold"]
    LAGUNA = ["status:ready", "status:triage", "status:in-progress", "status:blocked"]

    def repo(self, labels, on_item):
        return self.use({
            r"/repos/o/r/labels\?.*": lambda p: [{"name": n} for n in labels] if p.endswith("page=1") else [],
            r"/repos/o/r/issues/84": {"labels": [{"name": n} for n in on_item]},
            r"/repos/o/r/issues/84/labels(/.*)?": [],
        })

    def changes(self, gh):
        return [(m, u.replace(API + "/repos/o/r/issues/84/labels", ""), b) for m, u, b in gh.calls
                if m in ("POST", "DELETE")]

    def test_label_status_reads_both_namings(self):
        self.assertEqual([w.label_status(n) for n in ("ready", "status:ready", "Status: In-Progress", "status:triage", "bug")],
                         ["ready", "ready", "in-progress", None, None])

    def test_plain_labels_are_written_and_other_status_labels_removed(self):
        gh = self.repo(self.PALAZZO, ["milestone", "ready", "status:ready"])
        self.assertEqual(w.set_status(agent(), self.SRC, "in-progress"), "in-progress")
        self.assertEqual(self.changes(gh), [("POST", "", {"labels": ["in-progress"]}),
                                            ("DELETE", "/ready", None), ("DELETE", "/status%3Aready", None)])

    def test_prefixed_labels_are_written_when_that_is_what_the_repo_has(self):
        gh = self.repo(self.LAGUNA, ["status:in-progress"])
        self.assertEqual(w.set_status(agent(), self.SRC, "blocked"), "status:blocked")
        self.assertEqual(self.changes(gh), [("POST", "", {"labels": ["status:blocked"]}),
                                            ("DELETE", "/status%3Ain-progress", None)])

    def test_plain_names_win_when_a_repo_has_both(self):
        self.repo(["status:ready", "ready", "blocked", "status:blocked"], [])
        self.assertEqual(w.repo_status_labels(agent(), "o/r"), {"ready": "ready", "blocked": "blocked"})

    def test_repos_without_status_labels_are_left_alone(self):
        # GitHub's default labels include "question": without a ready label it is not a status label.
        gh = self.repo(["bug", "question", "enhancement"], [])
        self.assertEqual(w.set_status(agent(), self.SRC, "question"), "")
        # A status the repo has no label for changes nothing either.
        gh2 = self.repo(self.LAGUNA, ["status:in-progress"])
        self.assertEqual(w.set_status(agent(), self.SRC, "needs-review"), "")
        self.assertEqual(self.changes(gh) + self.changes(gh2), [])

    def test_split_status(self):
        self.assertEqual(w.split_status("Done: https://github.com/o/r/pull/9\n\nSTATUS: needs-review"),
                         ("Done: https://github.com/o/r/pull/9", "needs-review"))
        self.assertEqual(w.split_status("Which one?\n**Status:** on hold"), ("Which one?", "on-hold"))
        self.assertEqual(w.split_status("STATUS: question at first\nNo, stuck.\nSTATUS: blocked")[1], "blocked")
        # No status line: a pull request link means it is ready for review, otherwise it waits on the owner.
        self.assertEqual(w.split_status("Opened https://github.com/o/r/pull/9"),
                         ("Opened https://github.com/o/r/pull/9", "needs-review"))
        self.assertEqual(w.split_status("Should this go in contract/?"), ("Should this go in contract/?", "question"))

    def test_assigned_task_labels_in_progress_then_its_status(self):
        calls = []
        for name, fn in (("set_status", lambda a, src, st: calls.append(st) or st),
                         ("post_comment", lambda a, src, ans: calls.append(("comment", ans))),
                         ("prepare_task", lambda a, t: "context")):
            orig = getattr(w, name)
            setattr(w, name, fn)
            self.addCleanup(setattr, w, name, orig)
        orig = dict(w.ENGINES)
        self.addCleanup(lambda: (w.ENGINES.clear(), w.ENGINES.update(orig)))
        mgr = RunTaskMgr()
        task = {"id": "t", "kind": "assigned", "title": "x", "input": "go", "source": self.SRC}
        w.ENGINES["fake"] = lambda run: w.EngineResult("done", answer="Which file?\nSTATUS: question")
        self.assertEqual(w.run_task(mgr, agent(engine="fake"), "sys", task), "done")
        self.assertEqual(calls, ["in-progress", ("comment", "Which file?"), "question"])
        # A task that ran and failed is blocked; one that will be retried stays in progress.
        for outcome, want in (("failed", ["in-progress", "blocked"]), ("error", ["in-progress"])):
            calls.clear()
            w.ENGINES["fake"] = lambda run, o=outcome: w.EngineResult(o, error="nope")
            w.run_task(mgr, agent(engine="fake"), "sys", task)
            self.assertEqual(calls, want)
        # Giving up after the last retry blocks it too.
        calls.clear()
        state = {"retries": {self.SRC: w.MAX_TASK_ATTEMPTS - 1}}
        w.after_git_task(MgrStub(), state, os.path.join(WS_DIR, "label-state.json"), task, "error", agent())
        self.assertEqual(calls, ["blocked"])
        # Reviews and mentions don't touch labels.
        calls.clear()
        w.ENGINES["fake"] = lambda run: w.EngineResult("done", answer="Sure.")
        w.run_task(mgr, agent(engine="fake"), "sys", dict(task, kind="mention"))
        self.assertEqual(calls, [("comment", "Sure.")])

    def test_assigned_playbooks_ask_for_a_status(self):
        for trigger in ("assigned_issues", "assigned_prs"):
            self.assertIn("STATUS: needs-review", w.PLAYBOOKS[trigger])
            self.assertIn("STATUS: blocked", w.PLAYBOOKS[trigger])


class RetryTest(unittest.TestCase):
    def setUp(self):
        orig = w.llm_call
        self.addCleanup(setattr, w, "llm_call", orig)
        self.path = os.path.join(WS_DIR, "retry-state.json")

    def run_with(self, reply):
        w.llm_call = reply
        mgr = RunTaskMgr()
        out = w.run_task(mgr, agent(maxTurns=1, toolCallMode="text"), "sys",
                         {"id": "t1", "title": "x", "input": "hi"})
        return out, mgr

    @staticmethod
    def text_reply(text):
        return lambda *a, **k: w.Reply(text)

    def test_run_task_outcomes(self):
        def broken(*a):
            raise RuntimeError("HTTP 401 from https://llm/v1/chat/completions")
        self.assertEqual(self.run_with(broken)[0], "error")
        self.assertEqual(self.run_with(self.text_reply('```json\n{"tool":"read_file","path":"x"}\n```'))[0], "failed")
        out, mgr = self.run_with(self.text_reply("all good"))
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
        # Each test names its own checkouts: start from none.
        shutil.rmtree(os.path.join(WS_DIR, "repos"), ignore_errors=True)

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

    def test_clone_is_reused_and_local_default_branch_fast_forwarded(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        marker = os.path.join(repo_dir, ".git", "kept")
        open(marker, "w").close()
        # main moves on upstream while the agent sits on its issue branch.
        sh("git", "push", "-q", self.origin, "HEAD:main", cwd=os.path.join(self.tmp, "seed"))
        self.use({r"/repos/o/r/pulls/8": {"base": {"ref": "main"}, "head": {"ref": "fix"}}})
        w.prepare_task(agent(), {"id": "t2", "kind": "review", "source": "github:o/r#pull_requests:8@abc"})
        self.assertTrue(os.path.exists(marker))  # same clone, not a new one
        self.assertEqual(w.git(repo_dir, "rev-parse", "main"), w.git(repo_dir, "rev-parse", "origin/main"))

    def test_local_default_branch_fast_forwarded_when_checked_out(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        w.git(repo_dir, "checkout", "-q", "main")  # the agent left it on main
        sh("git", "push", "-q", self.origin, "HEAD:main", cwd=os.path.join(self.tmp, "seed"))
        w.prepare_task(agent(), {"id": "t2", "kind": "assigned", "source": "github:o/r#issues:7"})
        self.assertEqual(w.git(repo_dir, "rev-parse", "main"), w.git(repo_dir, "rev-parse", "origin/main"))

    def test_diverged_local_default_branch_is_left_alone(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        branch = w.git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        # A local-only commit on main, then origin moves on: the two diverge.
        w.git(repo_dir, "checkout", "-q", "main")
        w.git(repo_dir, "commit", "-q", "--allow-empty", "-m", "local only")
        local = w.git(repo_dir, "rev-parse", "main")
        sh("git", "push", "-q", self.origin, "HEAD:main", cwd=os.path.join(self.tmp, "seed"))
        w.git(repo_dir, "fetch", "-q", "origin")
        for on in ("main", branch):  # checked out (merge --ff-only) and not (update-ref)
            w.git(repo_dir, "checkout", "-q", on)
            w.fast_forward_default(repo_dir, "main")
            self.assertEqual(w.git(repo_dir, "rev-parse", "main"), local, f"HEAD on {on}")

    def test_same_name_other_owner_gets_its_own_clone(self):
        other = os.path.join(WS_DIR, "repos", "r")
        sh("git", "init", "-q", other)
        sh("git", "-C", other, "remote", "add", "origin", "https://github.com/someone/r.git")
        w.clone_url = lambda agent, repo: f"https://github.com/{repo}.git"  # setUp's points at the local origin
        self.assertEqual(w.repo_path(agent(), "o/r"), os.path.join(WS_DIR, "repos", "o-r"))
        # Its own clone stays where it is.
        self.assertEqual(w.repo_path(agent(), "someone/r"), other)
        # The fallback is checked too: repos/o-r holding x/o-r moves on to o-r-2.
        taken = os.path.join(WS_DIR, "repos", "o-r")
        sh("git", "init", "-q", taken)
        self.addCleanup(shutil.rmtree, taken, True)
        sh("git", "-C", taken, "remote", "add", "origin", "https://github.com/x/o-r.git")
        self.assertEqual(w.repo_path(agent(), "o/r"), os.path.join(WS_DIR, "repos", "o-r-2"))
        self.assertEqual(w.repo_path(agent(), "x/o-r"), taken)

    def test_prepare_keeps_root_git_out_of_the_engine_s_checkout(self):
        """A checkout's .git was written by the engine user during its task;
        its config can make git run commands (url.*.insteadOf with ext::,
        credential.helper = !…). The worker's git, the only git that knows
        the token, must never run in the checkout: the checkout is updated
        from the root-owned mirror, as the engine user."""
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        repo_dir = os.path.join(WS_DIR, "repos", "r")
        mirror = w.mirror_path("o/r")
        # Plant what a steered engine can: config that runs a command for
        # any git trusting this .git while talking to upstream.
        pwned = os.path.join(self.tmp, "PWNED")
        sh("git", "-C", repo_dir, "config", "protocol.ext.allow", "always")
        sh("git", "-C", repo_dir, "config",
           f"url.ext::sh -c touch% {pwned}% >&2% ;% false.insteadOf", self.origin)
        sh("git", "-C", repo_dir, "config", "credential.helper", f"!touch {pwned}2")
        # Upstream moves on, so the next prepare has to fetch.
        sh("git", "push", "-q", self.origin, "HEAD:main", cwd=os.path.join(self.tmp, "seed"))
        # Everything the next prepare runs, split in two: through the engine
        # drop (engine_argv) and straight from the worker (root with a drop).
        all_argv, via_engine = [], []
        orig_run, orig_engine = w.subprocess.run, w.engine_argv
        def run_spy(argv, **kw):
            all_argv.append(tuple(argv))
            return orig_run(argv, **kw)
        def engine_spy(argv):
            out = orig_engine(argv)
            via_engine.append(tuple(out))
            return out
        w.subprocess.run, w.engine_argv = run_spy, engine_spy
        try:
            w.prepare_task(agent(), {"id": "t2", "kind": "assigned", "source": "github:o/r#issues:7"})
        finally:
            w.subprocess.run, w.engine_argv = orig_run, orig_engine
        # Neither payload ran: no root git met the poisoned config.
        self.assertFalse(os.path.exists(pwned))
        self.assertFalse(os.path.exists(pwned + "2"))
        # The worker's own git ran, and only in the mirror.
        root_argv = set(all_argv) - set(via_engine)
        self.assertTrue(root_argv)  # the mirror fetches happened
        for argv in root_argv:
            self.assertNotIn(repo_dir, argv)
            self.assertIn(mirror, argv)
        # The checkout did move: its engine fetched the mirror.
        self.assertEqual(w.git(repo_dir, "rev-parse", "origin/main"),
                         w.root_git(mirror, "rev-parse", "main"))

    def test_mirror_is_redone_when_the_clone_url_changes(self):
        self.use({r"/repos/o/r/issues/7": {"title": "Add CI"}, r"/repos/o/r/pulls\?.*": []})
        w.prepare_task(agent(), {"id": "t1", "kind": "assigned", "source": "github:o/r#issues:7"})
        mirror = w.mirror_path("o/r")
        # The same repo later answers at another base URL (a moved GitLab).
        moved = os.path.join(self.tmp, "moved.git")
        sh("git", "clone", "-q", "--bare", self.origin, moved)
        w.clone_url = lambda agent, repo: moved
        w.prepare_task(agent(), {"id": "t2", "kind": "assigned", "source": "github:o/r#issues:7"})
        self.assertEqual(w.root_git(mirror, "config", "--get", "remote.doppio.url"), moved)

    def test_git_in_a_checkout_runs_through_the_engine_drop(self):
        seen = []
        orig = w.engine_argv
        w.engine_argv = lambda argv: seen.append(argv) or argv
        self.addCleanup(setattr, w, "engine_argv", orig)
        w.git(WS_DIR, "status", "--porcelain", check=False)  # not even a repo: must not raise
        self.assertEqual(seen, [["git", "-C", WS_DIR, "status", "--porcelain"]])

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
        self.assertIn("scratch files (helper scripts, notes) under /tmp", text)


if __name__ == "__main__":
    unittest.main()


class AccountTest(unittest.TestCase):
    def test_load_account_missing_file(self):
        with unittest.mock.patch.object(w, "ACCOUNT_PATH", "/nonexistent/account.json"):
            self.assertEqual(w.load_account(), {})

    def test_load_account_reads_fields(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"apiToken": "sk-shared"}, f)
            path = f.name
        try:
            with unittest.mock.patch.object(w, "ACCOUNT_PATH", path):
                self.assertEqual(w.load_account(), {"apiToken": "sk-shared"})
        finally:
            os.unlink(path)

    def test_apply_account_overlays_token_and_base(self):
        agent = w.apply_account({"apiToken": "own", "apiBase": "", "provider": "openai"},
                                {"apiToken": "sk-shared", "baseURL": "https://llm:8443/v1"})
        self.assertEqual(agent["apiToken"], "sk-shared")
        self.assertEqual(agent["apiBase"], "https://llm:8443/v1")

    def test_apply_account_cli_token(self):
        agent = w.apply_account({"apiToken": ""}, {"token": "claude-tok"})
        self.assertEqual(agent["accountToken"], "claude-tok")
        self.assertEqual(agent["apiToken"], "")
        agent = w.apply_account({}, {"apiKey": "vibe-key"})
        self.assertEqual(agent["accountToken"], "vibe-key")

    def test_apply_account_without_account_keeps_agent(self):
        a = {"apiToken": "own"}
        self.assertEqual(w.apply_account(a, {}), a)
