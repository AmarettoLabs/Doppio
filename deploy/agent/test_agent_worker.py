"""Tests for the agent worker. Run: python -m unittest discover -s deploy/agent"""
import json
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
