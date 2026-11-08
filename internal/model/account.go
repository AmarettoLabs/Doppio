package model

import (
	"strings"
	"time"
)

// AccountKind is what an account logs in with.
type AccountKind string

const (
	// AcctClaudeCode holds a long-lived token from `claude setup-token`
	// (needs a Claude subscription).
	AcctClaudeCode AccountKind = "claude-code"
	// AcctCodex is a ChatGPT login for the Codex CLI (device-code flow).
	AcctCodex AccountKind = "codex"
	// AcctVibe holds the key Mistral Vibe writes to ~/.vibe/.env after
	// logging in with a Mistral account (MISTRAL_API_KEY).
	AcctVibe AccountKind = "vibe"
	// AcctEndpoint is an OpenAI-compatible base URL plus a token (e.g. a
	// local LLM).
	AcctEndpoint AccountKind = "endpoint"
	// AcctAPIKey is a plain API key for the Doppio loop (OpenAI, Anthropic,
	// Mistral or a custom provider/model).
	AcctAPIKey AccountKind = "api-key"
)

// AccountKindInfo describes an account kind for the UI.
type AccountKindInfo struct {
	ID    AccountKind `json:"id"`
	Label string      `json:"label"`
	// SecretFields are the credential fields the account stores, in order.
	SecretFields []string `json:"secretFields"`
	Hint         string   `json:"hint"`
}

// AccountKinds lists every account kind, in display order.
func AccountKinds() []AccountKindInfo {
	return []AccountKindInfo{
		{AcctAPIKey, "API key (Doppio loop)", []string{"apiToken"},
			"OpenAI, Anthropic, Mistral or a custom provider/model, with a pasted API key"},
		{AcctEndpoint, "OpenAI-compatible endpoint", []string{"baseURL", "apiToken"},
			"Base URL + token, e.g. a local LLM; uses the extra hosts / CA bundle settings"},
		{AcctClaudeCode, "Claude Code", []string{"token"},
			"Long-lived token from `claude setup-token` (needs a Claude subscription), pasted"},
		{AcctCodex, "Codex CLI (ChatGPT login)", []string{},
			"ChatGPT login through the device-code flow"},
		{AcctVibe, "Mistral Vibe", []string{"apiKey"},
			"The key Vibe writes to ~/.vibe/.env (MISTRAL_API_KEY) after logging in with a Mistral account, pasted"},
	}
}

// LookupAccountKind returns the kind's info, ok=false if unknown.
func LookupAccountKind(k AccountKind) (AccountKindInfo, bool) {
	for _, info := range AccountKinds() {
		if info.ID == k {
			return info, true
		}
	}
	return AccountKindInfo{}, false
}

// ValidAccountKind reports whether k is a known kind.
func ValidAccountKind(k AccountKind) bool {
	_, ok := LookupAccountKind(k)
	return ok
}

// Account is a login shared by every agent using its engine: a CLI account
// (Claude Code, Codex, Vibe), an OpenAI-compatible endpoint, or an API key
// for the Doppio loop. The secrets live in files under the data directory,
// never in the store; only which of them are set is kept here.
type Account struct {
	ID        string      `json:"id"`
	Name      string      `json:"name"`
	Kind      AccountKind `json:"kind"`
	CreatedAt time.Time   `json:"createdAt"`
	UpdatedAt time.Time   `json:"updatedAt"`
	// SecretSet records which credential fields hold a value, so the API can
	// report them without ever returning the values.
	SecretSet map[string]bool `json:"secretSet"`
	// Notes are free-form, e.g. which subscription or server the account is
	// for. Never a secret.
	Notes string `json:"notes,omitempty"`
}

// HasSecret reports whether the named credential field is set.
func (a *Account) HasSecret(field string) bool {
	return a.SecretSet[field]
}

// SetSecret marks a credential field as set (value not kept).
func (a *Account) SetSecret(field string) {
	if a.SecretSet == nil {
		a.SecretSet = map[string]bool{}
	}
	a.SecretSet[field] = true
}

// UnsetSecret marks a credential field as empty.
func (a *Account) UnsetSecret(field string) {
	delete(a.SecretSet, field)
}

// SecretFields returns the kind's credential fields that are set.
func (a *Account) SecretFields() []string {
	info, ok := LookupAccountKind(a.Kind)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(info.SecretFields))
	for _, f := range info.SecretFields {
		if a.HasSecret(f) {
			out = append(out, f)
		}
	}
	return out
}

// RequiredSecretFields returns the credential fields the kind needs.
func (a *Account) RequiredSecretFields() []string {
	info, ok := LookupAccountKind(a.Kind)
	if !ok {
		return nil
	}
	return info.SecretFields
}

// Validate checks the account's own fields (not the secrets). The caller
// checks the secrets against the files on disk.
func (a *Account) Validate() string {
	if strings.TrimSpace(a.Name) == "" {
		return "name is required"
	}
	if !ValidAccountKind(a.Kind) {
		return "unknown account kind " + string(a.Kind)
	}
	return ""
}
