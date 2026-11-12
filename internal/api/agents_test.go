package api

import (
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func agentJSON(extra string) string {
	base := `{"name":"a","persona":"p","task":"t","provider":"openai","model":"m",` +
		`"gitProvider":"github","gitToken":"tok","gitLogin":"bot","gitRepos":["o/r"]`
	if extra != "" {
		base += "," + extra
	}
	return base + "}"
}

func TestCreateAgentRequiresOwnerWithGit(t *testing.T) {
	srv, _ := newTestServer(t, "")
	rec := do(srv.Handler(), "POST", "/api/agents", "", agentJSON(""))
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "ownerLogin") {
		t.Fatalf("no owner: got %d %s", rec.Code, rec.Body)
	}
	// Without a git account no owner is needed.
	rec = do(srv.Handler(), "POST", "/api/agents", "",
		`{"name":"a","persona":"p","task":"t","provider":"openai","model":"m"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("no git: got %d %s", rec.Code, rec.Body)
	}
}

func TestAgentTriggers(t *testing.T) {
	srv, _ := newTestServer(t, "")
	h := srv.Handler()

	for _, bad := range []string{`"triggers":[]`, `"triggers":["deploys"]`} {
		rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me",`+bad))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d %s", bad, rec.Code, rec.Body)
		}
	}

	// Unset triggers mean all of them.
	rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"@me"`))
	if rec.Code != http.StatusCreated {
		t.Fatalf("create: got %d %s", rec.Code, rec.Body)
	}
	var v AgentView
	if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
		t.Fatal(err)
	}
	all := []string{"review_requests", "mentions", "assigned_issues", "assigned_prs"}
	if !reflect.DeepEqual(v.Triggers, all) || v.OwnerLogin != "me" {
		t.Fatalf("defaults: triggers %v owner %q", v.Triggers, v.OwnerLogin)
	}

	// Update with triggers sets them (deduped); without keeps them.
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","triggers":["review_requests","review_requests"]`))
	if rec.Code != http.StatusOK {
		t.Fatalf("update: got %d %s", rec.Code, rec.Body)
	}
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me"`))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if !reflect.DeepEqual(v.Triggers, []string{"review_requests"}) {
		t.Fatalf("after updates: %v", v.Triggers)
	}
}

func TestAgentEngine(t *testing.T) {
	srv, _ := newTestServer(t, "")
	h := srv.Handler()

	rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me"`))
	var v AgentView
	if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil || rec.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	if v.Engine != "doppio" || v.TaskMinutes != 30 {
		t.Fatalf("defaults: engine %q, taskMinutes %d", v.Engine, v.TaskMinutes)
	}

	for body, want := range map[string]string{
		`"engine":"gemini"`:    "unknown engine",
		`"engine":"codex"`:     "not available yet",
		`"engine":"qwen-code"`: "not available yet",
		`"taskMinutes":-1`:     "taskMinutes",
		`"taskMinutes":100000`: "taskMinutes",
	} {
		rec := do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me",`+body))
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), want) {
			t.Errorf("%s: got %d %s", body, rec.Code, rec.Body)
		}
	}

	// A set limit sticks; omitting engine/limit keeps the stored values.
	do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","engine":"doppio","taskMinutes":90`))
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me"`))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.Engine != "doppio" || v.TaskMinutes != 90 {
		t.Fatalf("after updates: engine %q, taskMinutes %d", v.Engine, v.TaskMinutes)
	}

	rec = do(h, "GET", "/api/providers", "", "")
	if !strings.Contains(rec.Body.String(), `"engines":[{"id":"doppio","label":"Doppio loop","available":true},{"id":"vibe","label":"Mistral Vibe","available":true}`) {
		t.Fatalf("providers: %s", rec.Body)
	}

	// Mistral Vibe is selectable, and sticks across updates.
	do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","engine":"vibe"`))
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me"`))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.Engine != "vibe" {
		t.Fatalf("engine after update: %q", v.Engine)
	}
}

func TestToolCallMode(t *testing.T) {
	srv, _ := newTestServer(t, "")
	h := srv.Handler()
	create := func(extra string) AgentView {
		t.Helper()
		rec := do(h, "POST", "/api/agents", "", agentJSON("\"ownerLogin\":\"me\""+extra))
		var v AgentView
		if rec.Code != http.StatusCreated || json.Unmarshal(rec.Body.Bytes(), &v) != nil {
			t.Fatalf("create %s: %d %s", extra, rec.Code, rec.Body)
		}
		return v
	}
	// default: detect automatically
	if v := create(""); v.ToolCallMode != "" {
		t.Fatalf("default mode: %q", v.ToolCallMode)
	}
	for _, m := range []string{"native", "text"} {
		if v := create(",\"toolCallMode\":\"" + m + "\""); v.ToolCallMode != m {
			t.Fatalf("%s: got %q", m, v.ToolCallMode)
		}
	}
	// unknown modes are rejected
	for _, bad := range []string{"auto", "builtin"} {
		rec := do(h, "POST", "/api/agents", "", agentJSON(",\"toolCallMode\":\""+bad+"\""))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d", bad, rec.Code)
		}
	}
	// the form sends the whole payload: an empty mode resets to detect
	v := create(",\"toolCallMode\":\"text\"")
	rec := do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON("\"ownerLogin\":\"me\",\"toolCallMode\":\"\""))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.ToolCallMode != "" {
		t.Fatalf("update reset: %q", v.ToolCallMode)
	}
	// and a set mode is kept when the payload repeats it
	v = create(",\"toolCallMode\":\"native\"")
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON("\"ownerLogin\":\"me\",\"toolCallMode\":\"native\""))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.ToolCallMode != "native" {
		t.Fatalf("update kept native: %q", v.ToolCallMode)
	}
}

func TestMaxReplyTokens(t *testing.T) {
	srv, _ := newTestServer(t, "")
	h := srv.Handler()
	create := func(extra string) AgentView {
		t.Helper()
		rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me"`+extra))
		var v AgentView
		if rec.Code != http.StatusCreated || json.Unmarshal(rec.Body.Bytes(), &v) != nil {
			t.Fatalf("create %s: %d %s", extra, rec.Code, rec.Body)
		}
		return v
	}
	for extra, want := range map[string]int{
		``: 32768, // openai, its own API
		`,"apiBase":"https://api.openai.com/v1/"`: 32768,
		`,"apiBase":"https://llm-server:8443/v1"`: 8192,
		`,"provider":"custom"`:                    8192,
	} {
		if v := create(extra); v.DefaultMaxReplyTokens != want || v.MaxReplyTokens != 0 {
			t.Errorf("%s: default %d (want %d), set %d", extra, v.DefaultMaxReplyTokens, want, v.MaxReplyTokens)
		}
	}
	v := create(`,"maxReplyTokens":16000`)
	if v.MaxReplyTokens != 16000 {
		t.Fatalf("set: %d", v.MaxReplyTokens)
	}
	for _, bad := range []string{`,"maxReplyTokens":-5`, `,"maxReplyTokens":100`} {
		if rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me"`+bad)); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d", bad, rec.Code)
		}
	}
	// 0 on update goes back to the default.
	rec := do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","maxReplyTokens":0`))
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.MaxReplyTokens != 0 {
		t.Fatalf("reset: %d", v.MaxReplyTokens)
	}
}
