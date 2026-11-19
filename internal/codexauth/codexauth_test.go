package codexauth

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func jwt(claims map[string]any) string {
	enc := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	return enc(map[string]string{"alg": "none"}) + "." + enc(claims) + ".sig"
}

// authJSON builds an auth.json like codex writes after a ChatGPT login.
func authJSON(t *testing.T, account, refresh string, exp time.Time, last time.Time) string {
	t.Helper()
	doc := map[string]any{
		"OPENAI_API_KEY": nil,
		"tokens": map[string]any{
			"id_token": jwt(map[string]any{"email": "a@example.com",
				"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": account}}),
			"access_token":  jwt(map[string]any{"exp": exp.Unix()}),
			"refresh_token": refresh,
		},
		"last_refresh": last.UTC().Format(time.RFC3339Nano),
		"future_field": map[string]any{"kept": true},
	}
	b, _ := json.Marshal(doc)
	return string(b)
}

func mustParse(t *testing.T, s string) *Auth {
	t.Helper()
	a, err := Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func TestParseRejectsWhatCodexCannotUse(t *testing.T) {
	for _, s := range []string{"", "not json", "[]", `{"tokens":{}}`, `{"OPENAI_API_KEY":""}`} {
		if _, err := Parse(s); err == nil {
			t.Errorf("Parse(%q) accepted", s)
		}
	}
	if a := mustParse(t, `{"OPENAI_API_KEY":"sk-x"}`); a.IsChatGPT() || a.APIKey() != "sk-x" {
		t.Errorf("API-key login misread")
	}
}

func TestReadsAccountAndExpiry(t *testing.T) {
	now := time.Now()
	a := mustParse(t, authJSON(t, "acct-1", "rt-0", now.Add(5*24*time.Hour), now.Add(-time.Hour)))
	if a.AccountID() != "acct-1" {
		t.Errorf("account id = %q", a.AccountID())
	}
	if got := a.AccessExpiry().Unix(); got != now.Add(5*24*time.Hour).Unix() {
		t.Errorf("expiry = %d", got)
	}
	if a.LastRefresh().IsZero() {
		t.Error("last_refresh not read")
	}
}

func TestNeedsRefresh(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name string
		exp  time.Time
		last time.Time
		want bool
	}{
		{"fresh", now.Add(5 * 24 * time.Hour), now.Add(-time.Hour), false},
		{"expires within two days", now.Add(30 * time.Hour), now.Add(-time.Hour), true},
		{"expired", now.Add(-time.Minute), now.Add(-time.Hour), true},
	}
	for _, c := range cases {
		if got := mustParse(t, authJSON(t, "a", "rt", c.exp, c.last)).NeedsRefresh(now); got != c.want {
			t.Errorf("%s: NeedsRefresh = %v", c.name, got)
		}
	}
	// No readable expiry: fall back to the age of the last refresh.
	noExp := `{"tokens":{"access_token":"opaque","refresh_token":"rt"},"last_refresh":"` +
		now.Add(-8*24*time.Hour).Format(time.RFC3339) + `"}`
	if !mustParse(t, noExp).NeedsRefresh(now) {
		t.Error("an 8-day-old login with an opaque token should be refreshed")
	}
	if mustParse(t, `{"OPENAI_API_KEY":"sk"}`).NeedsRefresh(now) {
		t.Error("an API key has nothing to refresh")
	}
}

func TestRefreshLikeCodex(t *testing.T) {
	now := time.Now()
	var got map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"id_token":      jwt(map[string]any{"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": "acct-1"}}),
			"access_token":  jwt(map[string]any{"exp": now.Add(10 * 24 * time.Hour).Unix()}),
			"refresh_token": "rt-1",
		})
	}))
	defer srv.Close()
	old := mustParse(t, authJSON(t, "acct-1", "rt-0", now.Add(time.Hour), now.Add(-9*24*time.Hour)))
	fresh, err := old.Refresh(context.Background(), srv.Client(), srv.URL, now)
	if err != nil {
		t.Fatal(err)
	}
	if got["client_id"] != ClientID || got["grant_type"] != "refresh_token" || got["refresh_token"] != "rt-0" {
		t.Errorf("refresh request = %v", got)
	}
	if fresh.RefreshToken() != "rt-1" || fresh.NeedsRefresh(now) {
		t.Errorf("tokens not replaced: %s", fresh)
	}
	if fresh.LastRefresh().Sub(now).Abs() > time.Second {
		t.Errorf("last_refresh = %v", fresh.LastRefresh())
	}
	if !strings.Contains(fresh.String(), "future_field") {
		t.Error("unknown fields must survive a refresh")
	}
	if old.RefreshToken() != "rt-0" {
		t.Error("Refresh changed the receiver")
	}
	if !fresh.NewerThan(old) || old.NewerThan(fresh) {
		t.Error("the refreshed login should be newer than the old one, not the reverse")
	}
}

func TestRefreshErrorNeverEchoesTheBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"code":"refresh_token_reused","message":"already used"},"echo":"rt-secret"}`))
	}))
	defer srv.Close()
	a := mustParse(t, authJSON(t, "a", "rt-secret", time.Now(), time.Now()))
	_, err := a.Refresh(context.Background(), srv.Client(), srv.URL, time.Now())
	if err == nil || !strings.Contains(err.Error(), "refresh_token_reused") || strings.Contains(err.Error(), "rt-secret") {
		t.Errorf("err = %v", err)
	}
}

func TestNewerThanRefusesAnotherAccount(t *testing.T) {
	now := time.Now()
	old := mustParse(t, authJSON(t, "acct-1", "rt-0", now, now.Add(-time.Hour)))
	other := mustParse(t, authJSON(t, "acct-2", "rt-9", now, now))
	if other.NewerThan(old) {
		t.Error("a login for another ChatGPT account must never replace the shared one")
	}
	same := mustParse(t, authJSON(t, "acct-1", "rt-0", now, now))
	if same.NewerThan(old) {
		t.Error("same refresh token: nothing new")
	}
	older := mustParse(t, authJSON(t, "acct-1", "rt-5", now, now.Add(-2*time.Hour)))
	if older.NewerThan(old) {
		t.Error("an older refresh must not win")
	}
}
