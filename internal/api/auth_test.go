package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/runtime"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

const testMaster = "master-token-for-tests"

func newTestServer(t *testing.T, password string) (*Server, *store.Store) {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	rt := runtime.NewFromEnv(t.TempDir(), testMaster)
	return NewServer(st, rt, password, "", testMaster), st
}

func addAgent(t *testing.T, st *store.Store, id string) {
	t.Helper()
	now := time.Now()
	if err := st.UpsertAgent(&model.Agent{ID: id, Name: id, CreatedAt: now, UpdatedAt: now}); err != nil {
		t.Fatal(err)
	}
}

func do(h http.Handler, method, path, token, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("X-Manager-Token", token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestInternalAPIRequiresPerAgentToken(t *testing.T) {
	srv, st := newTestServer(t, "")
	addAgent(t, st, "a1")
	addAgent(t, st, "a2")
	h := srv.Handler()
	tokA1 := runtime.AgentToken(testMaster, "a1")

	cases := []struct {
		name, token string
		want        int
	}{
		{"no token", "", http.StatusUnauthorized},
		{"wrong token", "nope", http.StatusUnauthorized},
		{"master token is not accepted", testMaster, http.StatusUnauthorized},
		{"other agent's token", runtime.AgentToken(testMaster, "a2"), http.StatusUnauthorized},
		{"own token", tokA1, http.StatusCreated},
	}
	for _, c := range cases {
		rec := do(h, "POST", "/api/internal/agents/a1/events", c.token, `{"type":"log","content":"hi"}`)
		if rec.Code != c.want {
			t.Errorf("%s: got %d, want %d", c.name, rec.Code, c.want)
		}
	}
}

func TestFinishTaskChecksOwningAgent(t *testing.T) {
	srv, st := newTestServer(t, "")
	addAgent(t, st, "a1")
	addAgent(t, st, "a2")
	task, err := st.CreateTask("a1", "manual", "t", "do it", "")
	if err != nil {
		t.Fatal(err)
	}
	h := srv.Handler()
	path := "/api/internal/tasks/" + task.ID + "/finish"
	if rec := do(h, "POST", path, runtime.AgentToken(testMaster, "a2"), `{"status":"done"}`); rec.Code != http.StatusUnauthorized {
		t.Fatalf("other agent finishing task: got %d, want 401", rec.Code)
	}
	if rec := do(h, "POST", path, runtime.AgentToken(testMaster, "a1"), `{"status":"done"}`); rec.Code != http.StatusOK {
		t.Fatalf("owning agent finishing task: got %d, want 200: %s", rec.Code, rec.Body)
	}
}

func TestPortalPassword(t *testing.T) {
	srv, _ := newTestServer(t, "s3cret")
	h := srv.Handler()

	if rec := do(h, "GET", "/api/agents", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated API call: got %d, want 401", rec.Code)
	}
	if rec := do(h, "GET", "/api/health", "", ""); rec.Code != http.StatusOK {
		t.Fatalf("health should be public: got %d", rec.Code)
	}
	if rec := do(h, "POST", "/api/login", "", `{"password":"wrong"}`); rec.Code != http.StatusUnauthorized {
		t.Fatalf("wrong password: got %d, want 401", rec.Code)
	}
	rec := do(h, "POST", "/api/login", "", `{"password":"s3cret"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("login: got %d", rec.Code)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) == 0 {
		t.Fatal("login set no cookie")
	}
	req := httptest.NewRequest("GET", "/api/agents", nil)
	req.AddCookie(cookies[0])
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, req)
	if rec2.Code != http.StatusOK {
		t.Fatalf("authenticated API call: got %d, want 200", rec2.Code)
	}

	// A forged cookie must be rejected.
	req = httptest.NewRequest("GET", "/api/agents", nil)
	req.AddCookie(&http.Cookie{Name: sessionCookie, Value: "9999999999.deadbeef"})
	rec3 := httptest.NewRecorder()
	h.ServeHTTP(rec3, req)
	if rec3.Code != http.StatusUnauthorized {
		t.Fatalf("forged cookie: got %d, want 401", rec3.Code)
	}
}
