// Package codexauth reads and refreshes the auth.json Codex CLI writes after
// a ChatGPT login.
//
// ChatGPT refresh tokens are single-use: a refresh hands out a new one and
// the old one stops working ("refresh token was already used"). Several
// agents sharing one login must therefore never refresh it themselves, or
// the first refresh locks the others (and the manager) out. The manager owns
// the account's auth.json: it refreshes it well before the access token
// expires (codex only refreshes an expired token, or after a 401) and hands
// every agent the current copy.
package codexauth

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// ClientID is the OAuth client Codex CLI logs in and refreshes with.
const ClientID = "app_EMoamEEZ73f0CkXaXp7hrann"

// DefaultTokenURL is where Codex refreshes its tokens.
const DefaultTokenURL = "https://auth.openai.com/oauth/token"

// TokenURL is DefaultTokenURL unless CODEX_REFRESH_TOKEN_URL_OVERRIDE (the
// variable codex itself honors) points elsewhere, e.g. at a test server.
func TokenURL() string {
	if u := os.Getenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE"); u != "" {
		return u
	}
	return DefaultTokenURL
}

// RefreshBefore is how long before the access token expires the manager
// refreshes it; RefreshEvery is the longest a login goes without a refresh
// when the expiry can't be read.
const (
	RefreshBefore = 48 * time.Hour
	RefreshEvery  = 7 * 24 * time.Hour
)

// Auth is a parsed auth.json. The raw document is kept so fields this
// package doesn't know survive a refresh.
type Auth struct {
	raw map[string]any
}

// Parse reads an auth.json document. It must hold either ChatGPT tokens
// (with a refresh token) or an API key.
func Parse(s string) (*Auth, error) {
	var raw map[string]any
	dec := json.NewDecoder(strings.NewReader(s))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return nil, fmt.Errorf("auth.json is not a JSON object: %v", err)
	}
	a := &Auth{raw: raw}
	if a.RefreshToken() == "" && a.APIKey() == "" {
		return nil, errors.New("auth.json holds neither ChatGPT tokens (tokens.refresh_token) nor an API key (OPENAI_API_KEY)")
	}
	return a, nil
}

// String returns the document as JSON.
func (a *Auth) String() string {
	b, _ := json.MarshalIndent(a.raw, "", "  ")
	return string(b)
}

func (a *Auth) tokens() map[string]any {
	t, _ := a.raw["tokens"].(map[string]any)
	return t
}

func (a *Auth) token(name string) string {
	s, _ := a.tokens()[name].(string)
	return s
}

// RefreshToken is the ChatGPT refresh token ("" for an API-key login).
func (a *Auth) RefreshToken() string { return a.token("refresh_token") }

// APIKey is the OpenAI API key of an API-key login ("" otherwise).
func (a *Auth) APIKey() string {
	s, _ := a.raw["OPENAI_API_KEY"].(string)
	return s
}

// IsChatGPT reports whether this is a ChatGPT login (with tokens to refresh).
func (a *Auth) IsChatGPT() bool { return a.RefreshToken() != "" }

// AccountID is the ChatGPT account the tokens belong to: tokens.account_id,
// or the claim in the ID token.
func (a *Auth) AccountID() string {
	if id := a.token("account_id"); id != "" {
		return id
	}
	claims := jwtClaims(a.token("id_token"))
	if auth, ok := claims["https://api.openai.com/auth"].(map[string]any); ok {
		if id, ok := auth["chatgpt_account_id"].(string); ok {
			return id
		}
	}
	return ""
}

// AccessExpiry is when the access token expires (zero if unknown).
func (a *Auth) AccessExpiry() time.Time {
	switch v := jwtClaims(a.token("access_token"))["exp"].(type) {
	case json.Number:
		if n, err := v.Int64(); err == nil {
			return time.Unix(n, 0)
		}
	case float64:
		return time.Unix(int64(v), 0)
	}
	return time.Time{}
}

// LastRefresh is when the tokens were last refreshed (zero if unknown).
func (a *Auth) LastRefresh() time.Time {
	s, _ := a.raw["last_refresh"].(string)
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}
	}
	return t
}

// NeedsRefresh reports whether the manager should refresh the login now:
// the access token expires within RefreshBefore, or (expiry unknown) the
// last refresh is older than RefreshEvery.
func (a *Auth) NeedsRefresh(now time.Time) bool {
	if !a.IsChatGPT() {
		return false
	}
	if exp := a.AccessExpiry(); !exp.IsZero() {
		return exp.Sub(now) < RefreshBefore
	}
	return now.Sub(a.LastRefresh()) > RefreshEvery
}

// NewerThan reports whether a holds a later refresh of the same ChatGPT
// account than old: what an agent reports back after its codex refreshed
// the login on its own (the manager then adopts it, since the old refresh
// token no longer works). A different account is never newer: an engine
// must not be able to swap the shared login for another one.
func (a *Auth) NewerThan(old *Auth) bool {
	if !a.IsChatGPT() || !old.IsChatGPT() {
		return false
	}
	if a.AccountID() == "" || a.AccountID() != old.AccountID() {
		return false
	}
	if a.RefreshToken() == old.RefreshToken() {
		return false
	}
	return a.LastRefresh().After(old.LastRefresh())
}

// Refresh exchanges the refresh token for new tokens at tokenURL, the way
// codex does, and returns the updated document. The receiver is unchanged.
func (a *Auth) Refresh(ctx context.Context, client *http.Client, tokenURL string, now time.Time) (*Auth, error) {
	if !a.IsChatGPT() {
		return nil, errors.New("not a ChatGPT login: nothing to refresh")
	}
	body, _ := json.Marshal(map[string]string{
		"client_id":     ClientID,
		"grant_type":    "refresh_token",
		"refresh_token": a.RefreshToken(),
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("token refresh failed: HTTP %d: %s", resp.StatusCode, refreshError(data))
	}
	var got struct {
		IDToken      string `json:"id_token"`
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
	}
	if err := json.Unmarshal(data, &got); err != nil || got.AccessToken == "" {
		return nil, errors.New("token refresh failed: no access token in the reply")
	}
	out := a.clone()
	t := out.tokens()
	if t == nil {
		t = map[string]any{}
		out.raw["tokens"] = t
	}
	t["access_token"] = got.AccessToken
	if got.IDToken != "" {
		t["id_token"] = got.IDToken
	}
	if got.RefreshToken != "" {
		t["refresh_token"] = got.RefreshToken
	}
	out.raw["last_refresh"] = now.UTC().Format(time.RFC3339Nano)
	return out, nil
}

// refreshError is the error code or message of a failed refresh, never the
// whole body (it could echo a token).
func refreshError(data []byte) string {
	var e struct {
		Error            any    `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if json.Unmarshal(data, &e) != nil {
		return "unreadable reply"
	}
	parts := []string{}
	switch v := e.Error.(type) {
	case string:
		parts = append(parts, v)
	case map[string]any:
		for _, k := range []string{"code", "message"} {
			if s, ok := v[k].(string); ok && s != "" {
				parts = append(parts, s)
			}
		}
	}
	if e.ErrorDescription != "" {
		parts = append(parts, e.ErrorDescription)
	}
	if len(parts) == 0 {
		return "no error detail"
	}
	s := strings.Join(parts, ": ")
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}

func (a *Auth) clone() *Auth {
	b, _ := json.Marshal(a.raw)
	var raw map[string]any
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	_ = dec.Decode(&raw)
	return &Auth{raw: raw}
}

// jwtClaims decodes a JWT's payload without verifying it (the manager only
// reads expiry and account id from tokens it got from the auth server).
func jwtClaims(tok string) map[string]any {
	parts := strings.Split(tok, ".")
	if len(parts) < 2 {
		return nil
	}
	b, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return nil
	}
	var m map[string]any
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if dec.Decode(&m) != nil {
		return nil
	}
	return m
}
