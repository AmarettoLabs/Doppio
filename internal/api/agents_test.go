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
	if !strings.Contains(rec.Body.String(), `"engines":[{"id":"doppio","label":"Doppio loop","available":true}`) {
		t.Fatalf("providers: %s", rec.Body)
	}
}
