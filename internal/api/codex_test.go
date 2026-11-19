package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/runtime"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

func testJWT(claims map[string]any) string {
	enc := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	return enc(map[string]string{"alg": "none"}) + "." + enc(claims) + ".sig"
}

// codexAuthJSON is an auth.json like codex writes after a ChatGPT login.
func codexAuthJSON(account, refresh string, exp, last time.Time) string {
	b, _ := json.Marshal(map[string]any{
		"tokens": map[string]any{
			"id_token":      testJWT(map[string]any{"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": account}}),
			"access_token":  testJWT(map[string]any{"exp": exp.Unix()}),
			"refresh_token": refresh,
		},
		"last_refresh": last.UTC().Format(time.RFC3339Nano),
	})
	return string(b)
}

// codexRuntime is a recordingRuntime that also takes live account updates
// and runs a scripted device-code login.
type codexRuntime struct {
	*recordingRuntime
	mu      sync.Mutex
	updates map[string][]map[string]string // agent id -> the secrets written
	login   func(ctx context.Context, onLine func(string)) (string, error)
}

func (r *codexRuntime) UpdateAccount(agentID string, secrets map[string]string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.updates[agentID] = append(r.updates[agentID], secrets)
	return nil
}

func (r *codexRuntime) CodexLogin(ctx context.Context, onLine func(string)) (string, error) {
	return r.login(ctx, onLine)
}

func (r *codexRuntime) lastUpdate(agentID string) map[string]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	u := r.updates[agentID]
	if len(u) == 0 {
		return nil
	}
	return u[len(u)-1]
}

type codexFixture struct {
	t       *testing.T
	st      *store.Store
	rt      *codexRuntime
	srv     *Server
	h       http.Handler
	account string
	agent   string
}

// newCodexFixture: a Codex account holding authJSON (none when empty) and an
// agent using it.
func newCodexFixture(t *testing.T, authJSON string) *codexFixture {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	rt := &codexRuntime{recordingRuntime: newRecordingRuntime(t.TempDir()), updates: map[string][]map[string]string{}}
	srv := NewServer(st, rt, "", "", testMaster)
	f := &codexFixture{t: t, st: st, rt: rt, srv: srv, h: srv.Handler()}
	body := `{"name":"chatgpt","kind":"codex"}`
	if authJSON != "" {
		body = mustJSON(map[string]any{"name": "chatgpt", "kind": "codex", "secrets": map[string]string{"authJson": authJSON}})
	}
	f.account, _ = createAccount(t, f.h, body)
	rec := do(f.h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me","engine":"codex","accountId":"`+f.account+`"`))
	if rec.Code != http.StatusCreated {
		t.Fatalf("create agent: %d %s", rec.Code, rec.Body)
	}
	var v AgentView
	_ = json.Unmarshal(rec.Body.Bytes(), &v)
	f.agent = v.ID
	return f
}

func (f *codexFixture) storedAuth() string {
	s, err := f.st.Accounts().ReadSecret(f.account, "authJson")
	if err != nil {
		f.t.Fatal(err)
	}
	return s
}

func (f *codexFixture) events() string {
	var b strings.Builder
	for _, e := range f.st.EventsSince(f.agent, 0) {
		b.WriteString(e.Type + ": " + e.Content + "\n")
	}
	return b.String()
}

func TestCodexAccountNeedsAUsableAuthJSON(t *testing.T) {
	f := newCodexFixture(t, "")
	rec, m := doJSON(t, f.h, "GET", "/api/accounts/"+f.account, "")
	if rec.Code != http.StatusOK || m["status"] != "missing" {
		t.Fatalf("a Codex account without a login is missing: %d %v", rec.Code, m)
	}
	for _, bad := range []string{"not json", `{"tokens":{}}`} {
		body := mustJSON(map[string]any{"secrets": map[string]string{"authJson": bad}})
		if rec := do(f.h, "PATCH", "/api/accounts/"+f.account, "", body); rec.Code != http.StatusBadRequest {
			t.Errorf("authJson %q accepted: %d", bad, rec.Code)
		}
	}
	good := codexAuthJSON("acct-1", "rt-0", time.Now().Add(5*24*time.Hour), time.Now())
	rec, m = doJSON(t, f.h, "PATCH", "/api/accounts/"+f.account, mustJSON(map[string]any{"secrets": map[string]string{"authJson": good}}))
	if rec.Code != http.StatusOK || m["status"] != "configured" {
		t.Fatalf("pasted auth.json: %d %v", rec.Code, m)
	}
	if strings.Contains(rec.Body.String(), "rt-0") {
		t.Fatal("the API returned the token")
	}
}

func TestCodexDeviceLogin(t *testing.T) {
	f := newCodexFixture(t, "")
	release := make(chan struct{})
	auth := codexAuthJSON("acct-1", "rt-0", time.Now().Add(5*24*time.Hour), time.Now())
	f.rt.login = func(ctx context.Context, onLine func(string)) (string, error) {
		// What codex 0.166 prints, colors already stripped by the runtime.
		for _, l := range []string{
			"Follow these steps to sign in with ChatGPT using device code authorization:",
			"1. Open this link in your browser and sign in to your account",
			"   https://auth.openai.com/codex/device",
			"2. Enter this one-time code (expires in 15 minutes)",
			"   ABCD-12345",
		} {
			onLine(l)
		}
		<-release
		return auth, nil
	}
	rec, m := doJSON(t, f.h, "POST", "/api/accounts/"+f.account+"/codex-login", "")
	if rec.Code != http.StatusOK || m["state"] != "waiting" ||
		m["url"] != "https://auth.openai.com/codex/device" || m["code"] != "ABCD-12345" {
		t.Fatalf("start: %d %v", rec.Code, m)
	}
	// A second start while waiting shows the same login.
	if _, m2 := doJSON(t, f.h, "POST", "/api/accounts/"+f.account+"/codex-login", ""); m2["code"] != "ABCD-12345" {
		t.Fatalf("second start: %v", m2)
	}
	close(release)
	waitFor(t, func() bool {
		_, m := doJSON(t, f.h, "GET", "/api/accounts/"+f.account+"/codex-login", "")
		return m["state"] == "done"
	})
	if f.storedAuth() != auth {
		t.Fatal("the login's auth.json was not stored")
	}
	if got := f.rt.lastUpdate(f.agent); got["authJson"] != auth {
		t.Fatalf("the agent's copy was not updated: %v", got)
	}
	if !strings.Contains(f.events(), "logged in with ChatGPT") {
		t.Fatalf("no event for the agent: %s", f.events())
	}
}

func TestCodexDeviceLoginCancelAndFailure(t *testing.T) {
	f := newCodexFixture(t, "")
	f.rt.login = func(ctx context.Context, onLine func(string)) (string, error) {
		onLine("https://auth.openai.com/codex/device")
		onLine("WXYZ-98765")
		<-ctx.Done()
		return "", ctx.Err()
	}
	doJSON(t, f.h, "POST", "/api/accounts/"+f.account+"/codex-login", "")
	if rec, m := doJSON(t, f.h, "DELETE", "/api/accounts/"+f.account+"/codex-login", ""); rec.Code != http.StatusOK || m["state"] != "cancelled" {
		t.Fatalf("cancel: %d %v", rec.Code, m)
	}
	time.Sleep(50 * time.Millisecond)
	if _, m := doJSON(t, f.h, "GET", "/api/accounts/"+f.account+"/codex-login", ""); m["state"] != "cancelled" || m["code"] != nil {
		t.Fatalf("after cancel: %v", m)
	}
	if f.storedAuth() != "" {
		t.Fatal("a cancelled login stored something")
	}

	// codex exits with an error: the reason is its last line.
	f.rt.login = func(ctx context.Context, onLine func(string)) (string, error) {
		onLine("Error: device code request failed with status 403")
		return "", context.Canceled
	}
	doJSON(t, f.h, "POST", "/api/accounts/"+f.account+"/codex-login", "")
	waitFor(t, func() bool {
		_, m := doJSON(t, f.h, "GET", "/api/accounts/"+f.account+"/codex-login", "")
		return m["state"] == "failed" && strings.Contains(m["error"].(string), "status 403")
	})
}

func TestCodexLoginOnlyForCodexAccounts(t *testing.T) {
	f := newCodexFixture(t, "")
	id, _ := createAccount(t, f.h, `{"name":"v","kind":"vibe","secrets":{"apiKey":"k"}}`)
	if rec := do(f.h, "POST", "/api/accounts/"+id+"/codex-login", "", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("vibe account login: %d", rec.Code)
	}
}

// tokenServer answers refreshes like auth.openai.com, counting them.
func tokenServer(t *testing.T, status int) (*httptest.Server, *int) {
	t.Helper()
	n := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n++
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":{"code":"refresh_token_reused"}}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"access_token":  testJWT(map[string]any{"exp": time.Now().Add(10 * 24 * time.Hour).Unix()}),
			"refresh_token": "rt-new",
		})
	}))
	t.Cleanup(srv.Close)
	t.Setenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE", srv.URL)
	return srv, &n
}

func TestCodexRefreshBeforeExpiry(t *testing.T) {
	_, n := tokenServer(t, http.StatusOK)
	fresh := codexAuthJSON("acct-1", "rt-0", time.Now().Add(5*24*time.Hour), time.Now())
	f := newCodexFixture(t, fresh)
	f.srv.RefreshCodexLogins(context.Background())
	if *n != 0 || f.storedAuth() != fresh {
		t.Fatalf("a login valid for days was refreshed (%d calls)", *n)
	}

	f2 := newCodexFixture(t, codexAuthJSON("acct-1", "rt-0", time.Now().Add(20*time.Hour), time.Now().Add(-9*24*time.Hour)))
	f2.srv.RefreshCodexLogins(context.Background())
	if *n != 1 || !strings.Contains(f2.storedAuth(), "rt-new") {
		t.Fatalf("an expiring login was not refreshed: %d calls, %s", *n, f2.storedAuth())
	}
	if got := f2.rt.lastUpdate(f2.agent); !strings.Contains(got["authJson"], "rt-new") {
		t.Fatalf("the agent did not get the refreshed login: %v", got)
	}
	// Refreshed: the next pass leaves it alone.
	f2.srv.RefreshCodexLogins(context.Background())
	if *n != 1 {
		t.Fatalf("refreshed twice: %d", *n)
	}
}

func TestCodexRefreshFailureIsReported(t *testing.T) {
	tokenServer(t, http.StatusUnauthorized)
	old := codexAuthJSON("acct-1", "rt-0", time.Now().Add(time.Hour), time.Now())
	f := newCodexFixture(t, old)
	f.srv.RefreshCodexLogins(context.Background())
	if f.storedAuth() != old {
		t.Fatal("a failed refresh changed the stored login")
	}
	if ev := f.events(); !strings.Contains(ev, "refresh_token_reused") || !strings.Contains(ev, "log in again") {
		t.Fatalf("the failure was not reported to the agent: %s", ev)
	}
}

func TestCodexAuthReportedByAgent(t *testing.T) {
	tokenSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]string
		_ = json.NewDecoder(r.Body).Decode(&req)
		account := "acct-1"
		switch req["refresh_token"] {
		case "garbage":
			w.WriteHeader(http.StatusUnauthorized)
			return
		case "rt-other":
			account = "acct-2"
		case "rt-no-identity":
			_ = json.NewEncoder(w).Encode(map[string]string{"access_token": "opaque", "refresh_token": "verified"})
			return
		case "rt-1":
		default:
			t.Errorf("unexpected refresh request: %v", req)
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"id_token":      testJWT(map[string]any{"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": account}}),
			"access_token":  "verified-access",
			"refresh_token": "verified-refresh",
		})
	}))
	defer tokenSrv.Close()
	t.Setenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE", tokenSrv.URL)
	now := time.Now()
	stored := codexAuthJSON("acct-1", "rt-0", now.Add(time.Hour), now.Add(-time.Hour))
	f := newCodexFixture(t, stored)
	tok := runtime.AgentToken(testMaster, f.agent)
	path := "/api/internal/agents/" + f.agent + "/codex-auth"
	report := func(token, auth string) (int, map[string]any) {
		rec := do(f.h, "POST", path, token, mustJSON(map[string]any{"authJson": auth}))
		var m map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &m)
		return rec.Code, m
	}
	if code, _ := report("wrong", stored); code != http.StatusUnauthorized {
		t.Fatalf("report without the agent's token: %d", code)
	}
	// Another ChatGPT account: never adopted, whatever its date.
	if _, m := report(tok, codexAuthJSON("acct-2", "rt-9", now.Add(time.Hour), now)); m["adopted"] != false {
		t.Fatalf("another account was adopted: %v", m)
	}
	// The same refresh token: nothing new.
	if _, m := report(tok, stored); m["adopted"] != false {
		t.Fatalf("an unchanged login was adopted: %v", m)
	}
	// Forged claims cannot supply identity, even with a future timestamp.
	for _, token := range []string{"garbage", "rt-other", "rt-no-identity"} {
		forged := codexAuthJSON("acct-1", token, now.Add(time.Hour), now.Add(24*time.Hour))
		if _, m := report(tok, forged); m["adopted"] != false {
			t.Fatalf("forged report %s adopted: %v", token, m)
		}
		if f.storedAuth() != stored || f.rt.lastUpdate(f.agent) != nil {
			t.Fatal("rejected report changed the shared login")
		}
	}
	// A later refresh of the same account: adopted and handed on.
	newer := codexAuthJSON("acct-1", "rt-1", now.Add(10*24*time.Hour), now)
	if code, m := report(tok, newer); code != http.StatusOK || m["adopted"] != true {
		t.Fatalf("a later refresh was not adopted: %d %v", code, m)
	}
	if !strings.Contains(f.storedAuth(), "verified-refresh") {
		t.Fatalf("stored: %s", f.storedAuth())
	}
	if got := f.rt.lastUpdate(f.agent); !strings.Contains(got["authJson"], "verified-refresh") {
		t.Fatalf("agents did not get the adopted login: %v", got)
	}
	// An older one afterwards loses.
	if _, m := report(tok, codexAuthJSON("acct-1", "rt-2", now, now.Add(-30*time.Minute))); m["adopted"] != false {
		t.Fatalf("an older refresh was adopted: %v", m)
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}
