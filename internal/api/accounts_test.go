package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

func doJSON(t *testing.T, h http.Handler, method, path, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := do(h, method, path, "", body)
	var m map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &m)
	return rec, m
}

func createAccount(t *testing.T, h http.Handler, body string) (string, map[string]any) {
	t.Helper()
	rec, m := doJSON(t, h, "POST", "/api/accounts", body)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create account: %d %s", rec.Code, rec.Body)
	}
	id, _ := m["id"].(string)
	if id == "" {
		t.Fatalf("no id in %s", rec.Body)
	}
	return id, m
}

func TestAccountKindsEndpoint(t *testing.T) {
	srv, _ := newTestServer(t, "")
	rec := do(srv.Handler(), "GET", "/api/account-kinds", "", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("kinds: %d %s", rec.Code, rec.Body)
	}
	for _, want := range []string{`"claude-code"`, `"codex"`, `"vibe"`, `"endpoint"`, `"api-key"`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("kinds missing %s: %s", want, rec.Body)
		}
	}
}

func TestAccountCRUDAndSecretHandling(t *testing.T) {
	srv, st := newTestServer(t, "")
	h := srv.Handler()

	// Invalid input.
	for _, body := range []string{
		`{"name":"","kind":"api-key"}`,
		`{"name":"x","kind":"nope"}`,
		`{"name":"x","kind":"api-key","secrets":{"apiToken":"k","other":"v"}}`,
	} {
		if rec := do(h, "POST", "/api/accounts", "", body); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d %s", body, rec.Code, rec.Body)
		}
	}

	id, m := createAccount(t, h, `{"name":"Work key","kind":"api-key","secrets":{"apiToken":"sk-live-123"}}`)
	if m["status"] != "configured" || m["hasSecrets"] != true {
		t.Fatalf("created view: %s", mustJSON(m))
	}
	if fields, ok := m["secretFields"].([]any); !ok || len(fields) != 1 || fields[0] != "apiToken" {
		t.Fatalf("secretFields: %s", mustJSON(m))
	}
	// The secret is never returned.
	body := mustJSON(m)
	if strings.Contains(body, "sk-live-123") {
		t.Fatalf("secret leaked in the account view: %s", body)
	}
	rec := do(h, "GET", "/api/accounts/"+id, "", "")
	if strings.Contains(rec.Body.String(), "sk-live-123") {
		t.Fatalf("secret leaked in GET: %s", rec.Body)
	}
	rec = do(h, "GET", "/api/accounts", "", "")
	if strings.Contains(rec.Body.String(), "sk-live-123") {
		t.Fatalf("secret leaked in list: %s", rec.Body)
	}
	// Nor in the store file or the agent-facing payloads.
	b, err := os.ReadFile(filepath.Join(st.Dir(), "store.json"))
	if err != nil || strings.Contains(string(b), "sk-live-123") {
		t.Fatalf("secret in store.json: %v", err)
	}

	// Rename; secrets nil keeps them.
	rec, m = doJSON(t, h, "PATCH", "/api/accounts/"+id, `{"name":"Renamed","secrets":null}`)
	if rec.Code != http.StatusOK || m["name"] != "Renamed" || m["hasSecrets"] != true {
		t.Fatalf("rename: %d %s", rec.Code, rec.Body)
	}
	if v, err := st.Accounts().ReadSecret(id, "apiToken"); err != nil || v != "sk-live-123" {
		t.Fatalf("secret lost on rename: %q %v", v, err)
	}
	// Replace the secret.
	rec, _ = doJSON(t, h, "PATCH", "/api/accounts/"+id, `{"name":"","secrets":{"apiToken":"sk-new"}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("replace: %d %s", rec.Code, rec.Body)
	}
	if v, _ := st.Accounts().ReadSecret(id, "apiToken"); v != "sk-new" {
		t.Fatalf("secret not replaced: %q", v)
	}
	// A missing credential reports status "missing".
	rec, _ = doJSON(t, h, "PATCH", "/api/accounts/"+id, `{"secrets":{"apiToken":""}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("clear: %d %s", rec.Code, rec.Body)
	}
	rec = do(h, "GET", "/api/accounts/"+id, "", "")
	var v struct {
		Status       string   `json:"status"`
		HasSecrets   bool     `json:"hasSecrets"`
		SecretFields []string `json:"secretFields"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.Status != "missing" || v.HasSecrets || len(v.SecretFields) != 0 {
		t.Fatalf("cleared account: %s", rec.Body)
	}
	if got, _ := st.Accounts().ReadSecret(id, "apiToken"); got != "" {
		t.Fatalf("cleared secret still readable: %q", got)
	}

	// Unknown account.
	if rec := do(h, "GET", "/api/accounts/nope", "", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("get unknown: %d", rec.Code)
	}
	if rec := do(h, "DELETE", "/api/accounts/nope", "", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("delete unknown: %d", rec.Code)
	}

	// Delete removes the files too.
	if rec := do(h, "DELETE", "/api/accounts/"+id, "", ""); rec.Code != http.StatusOK {
		t.Fatalf("delete: %d %s", rec.Code, rec.Body)
	}
	if st.Accounts().Get(id) != nil {
		t.Fatal("account still in the store")
	}
	if _, err := st.Accounts().ReadSecret(id, "apiToken"); err != nil {
		t.Fatalf("secret dir not removed: %v", err)
	}
}

func TestDeleteAccountInUseIsRefused(t *testing.T) {
	srv, st := newTestServer(t, "")
	h := srv.Handler()
	id, _ := createAccount(t, h, `{"name":"shared","kind":"endpoint","secrets":{"baseURL":"https://llm:8443/v1","apiToken":"t"}}`)

	for _, name := range []string{"agent-one", "agent-two"} {
		rec := do(h, "POST", "/api/agents", "", agentJSON(`"name":"`+name+`","ownerLogin":"me","accountId":"`+id+`"`))
		if rec.Code != http.StatusCreated {
			t.Fatalf("create agent: %d %s", rec.Code, rec.Body)
		}
		var v AgentView
		_ = json.Unmarshal(rec.Body.Bytes(), &v)
		if v.AccountID != id {
			t.Fatalf("agent account: %s", rec.Body)
		}
	}
	// The agent view shows the account's name.
	agents := st.Agents()
	if len(agents) != 2 {
		t.Fatalf("agents: %d", len(agents))
	}
	rec := do(h, "GET", "/api/agents/"+agents[0].ID, "", "")
	if !strings.Contains(rec.Body.String(), `"accountName":"shared"`) {
		t.Fatalf("agent view lacks the account name: %s", rec.Body)
	}
	// The account view lists the agents using it.
	rec = do(h, "GET", "/api/accounts/"+id, "", "")
	if !strings.Contains(rec.Body.String(), "agent-one") || !strings.Contains(rec.Body.String(), "agent-two") {
		t.Fatalf("account view lacks its agents: %s", rec.Body)
	}

	rec = do(h, "DELETE", "/api/accounts/"+id, "", "")
	if rec.Code != http.StatusConflict {
		t.Fatalf("delete in use: %d %s", rec.Code, rec.Body)
	}
	if !strings.Contains(rec.Body.String(), "agent-one") || !strings.Contains(rec.Body.String(), "agent-two") {
		t.Fatalf("refusal lacks the agent list: %s", rec.Body)
	}
	if st.Accounts().Get(id) == nil {
		t.Fatal("account deleted while in use")
	}

	// Detach one agent: still refused (the other uses it).
	rec = do(h, "PATCH", "/api/agents/"+agents[0].ID, "", agentJSON(`"ownerLogin":"me","accountId":"none"`))
	if rec.Code != http.StatusOK {
		t.Fatalf("detach: %d %s", rec.Code, rec.Body)
	}
	var v AgentView
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if v.AccountID != "" {
		t.Fatalf("account not cleared: %s", rec.Body)
	}
	rec = do(h, "DELETE", "/api/accounts/"+id, "", "")
	if rec.Code != http.StatusConflict || !strings.Contains(rec.Body.String(), "agent-two") {
		t.Fatalf("still in use: %d %s", rec.Code, rec.Body)
	}

	// Detach the other one: now it can be deleted.
	if rec := do(h, "PATCH", "/api/agents/"+agents[1].ID, "", agentJSON(`"ownerLogin":"me","accountId":"none"`)); rec.Code != http.StatusOK {
		t.Fatalf("detach 2: %d %s", rec.Code, rec.Body)
	}
	if rec := do(h, "DELETE", "/api/accounts/"+id, "", ""); rec.Code != http.StatusOK {
		t.Fatalf("delete after detach: %d %s", rec.Code, rec.Body)
	}
}

func TestAgentWithUnknownAccountRejected(t *testing.T) {
	srv, _ := newTestServer(t, "")
	h := srv.Handler()
	rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me","accountId":"nope"`))
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "unknown account") {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	// On update too.
	rec = do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me"`))
	var v AgentView
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","accountId":"nope"`))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("update: %d %s", rec.Code, rec.Body)
	}
}

// TestAccountSecretsPassedToContainer checks the manager writes the account's
// credentials into the agent's state dir on start, read-only, and never into
// the store or events.
func TestAccountSecretsPassedToContainer(t *testing.T) {
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	dataDir := t.TempDir()
	rt := newRecordingRuntime(dataDir)
	srv := NewServer(st, rt, "", "", testMaster)
	h := srv.Handler()

	id, _ := createAccount(t, h, `{"name":"claude","kind":"claude-code","secrets":{"token":"claude-secret-tok"}}`)
	rec := do(h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me","accountId":"`+id+`"`))
	if rec.Code != http.StatusCreated {
		t.Fatalf("create agent: %d %s", rec.Code, rec.Body)
	}
	var v AgentView
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	if rec := do(h, "POST", "/api/agents/"+v.ID+"/start", "", ""); rec.Code != http.StatusOK {
		t.Fatalf("start: %d %s", rec.Code, rec.Body)
	}
	if len(rt.starts) != 1 {
		t.Fatalf("starts: %d", len(rt.starts))
	}
	if !rt.starts[0].withSecrets || rt.starts[0].secrets["token"] != "claude-secret-tok" {
		t.Fatalf("account secrets not passed: %+v", rt.starts[0])
	}
	// Written to a 0600 file inside the agent's state dir.
	p := filepath.Join(dataDir, "agents", v.ID, "account.json")
	b, err := os.ReadFile(p)
	if err != nil || !strings.Contains(string(b), "claude-secret-tok") {
		t.Fatalf("account file: %v %s", err, b)
	}
	fi, err := os.Stat(p)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("account file mode: %v %v", fi.Mode().Perm(), err)
	}
	// The secret is nowhere else: not in store.json, not in events, not in
	// the agent view.
	sb, _ := os.ReadFile(filepath.Join(dataDir, "store.json"))
	if strings.Contains(string(sb), "claude-secret-tok") {
		t.Fatal("secret in store.json")
	}
	for _, e := range st.EventsSince(v.ID, 0) {
		if strings.Contains(e.Content, "claude-secret-tok") {
			t.Fatalf("secret in event: %+v", e)
		}
	}
	rec = do(h, "GET", "/api/agents/"+v.ID, "", "")
	if strings.Contains(rec.Body.String(), "claude-secret-tok") {
		t.Fatalf("secret in the agent view: %s", rec.Body)
	}
	// Stopping and starting without an account removes the file.
	rec = do(h, "PATCH", "/api/agents/"+v.ID, "", agentJSON(`"ownerLogin":"me","accountId":"none"`))
	if rec.Code != http.StatusOK {
		t.Fatalf("detach: %d %s", rec.Code, rec.Body)
	}
	if rec := do(h, "POST", "/api/agents/"+v.ID+"/stop", "", ""); rec.Code != http.StatusOK {
		t.Fatalf("stop: %d %s", rec.Code, rec.Body)
	}
	if rec := do(h, "POST", "/api/agents/"+v.ID+"/start", "", ""); rec.Code != http.StatusOK {
		t.Fatalf("start: %d %s", rec.Code, rec.Body)
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatal("stale account file after detaching the account")
	}
	if rt.starts[len(rt.starts)-1].withSecrets {
		t.Fatal("secrets passed although the agent has no account")
	}
}

// recordingRuntime records what Start* was called with.
type startRecord struct {
	withSecrets bool
	secrets     map[string]string
}

type recordingRuntime struct {
	dir    string
	starts []startRecord
}

func newRecordingRuntime(dir string) *recordingRuntime { return &recordingRuntime{dir: dir} }

func (r *recordingRuntime) EnsureNetwork() error { return nil }
func (r *recordingRuntime) Start(a *model.Agent) (string, error) {
	return r.StartWithAccounts(a, nil)
}
func (r *recordingRuntime) StartWithAccounts(a *model.Agent, secrets map[string]string) (string, error) {
	r.starts = append(r.starts, startRecord{withSecrets: len(secrets) > 0, secrets: secrets})
	// Mimic the real runtime: write the file into the agent's state dir.
	if len(secrets) > 0 {
		dir := filepath.Join(r.dir, "agents", a.ID)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return "", err
		}
		b, _ := json.Marshal(secrets)
		if err := os.WriteFile(filepath.Join(dir, "account.json"), b, 0o600); err != nil {
			return "", err
		}
	} else {
		_ = os.Remove(filepath.Join(r.dir, "agents", a.ID, "account.json"))
	}
	return "c0ffee00c0ffee00", nil
}
func (r *recordingRuntime) Stop(string) error        { return nil }
func (r *recordingRuntime) Status(string) string     { return "not-found" }
func (r *recordingRuntime) RemoveState(string) error { return nil }

func mustJSON(m map[string]any) string {
	b, _ := json.Marshal(m)
	return string(b)
}
