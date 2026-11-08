package store

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

func openAccounts(t *testing.T) (*Store, *Accounts) {
	t.Helper()
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return st, st.Accounts()
}

func TestAccountCreateReadUpdateDelete(t *testing.T) {
	st, ac := openAccounts(t)

	acc, err := ac.Create("Work OpenAI", model.AcctAPIKey, "team key", map[string]string{"apiToken": "sk-test"})
	if err != nil {
		t.Fatal(err)
	}
	if acc.ID == "" || acc.Name != "Work OpenAI" || !acc.HasSecret("apiToken") {
		t.Fatalf("created: %+v", acc)
	}
	// The secret is in a 0600 file under DATA_DIR/accounts/<id>/, not in the
	// store JSON.
	p := filepath.Join(st.dir, AccountsDir, acc.ID, "apiToken.secret")
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("secret file: %v", err)
	}
	if string(b) != "sk-test" {
		t.Fatalf("secret file holds %q", b)
	}
	fi, err := os.Stat(p)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("secret file mode: %v %v", fi.Mode().Perm(), err)
	}
	storeJSON, err := os.ReadFile(filepath.Join(st.dir, "store.json"))
	if err != nil {
		t.Fatal(err)
	}
	if contains(string(storeJSON), "sk-test") {
		t.Fatal("the store JSON holds the secret")
	}

	// Secrets() returns the values; ReadSecret one field.
	secrets, err := ac.Secrets(acc.ID)
	if err != nil || !reflect.DeepEqual(secrets, map[string]string{"apiToken": "sk-test"}) {
		t.Fatalf("Secrets: %v %v", secrets, err)
	}
	if v, err := ac.ReadSecret(acc.ID, "apiToken"); err != nil || v != "sk-test" {
		t.Fatalf("ReadSecret: %q %v", v, err)
	}

	// Rename + replace the secret; a nil map keeps it.
	if _, err := ac.Update(acc.ID, "Work key", "renamed", nil); err != nil {
		t.Fatal(err)
	}
	if v, _ := ac.ReadSecret(acc.ID, "apiToken"); v != "sk-test" {
		t.Fatalf("secret not kept on update: %q", v)
	}
	upd, err := ac.Update(acc.ID, "", "", map[string]string{"apiToken": "sk-new"})
	if err != nil || upd.Name != "Work key" || upd.Notes != "" {
		t.Fatalf("update: %+v %v", upd, err)
	}
	if v, _ := ac.ReadSecret(acc.ID, "apiToken"); v != "sk-new" {
		t.Fatalf("secret not replaced: %q", v)
	}
	// An empty value clears the field.
	if _, err := ac.Update(acc.ID, "", "", map[string]string{"apiToken": ""}); err != nil {
		t.Fatal(err)
	}
	if ac.Get(acc.ID).HasSecret("apiToken") {
		t.Fatal("cleared secret still marked set")
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatal("cleared secret file still there")
	}
	// Restore for the delete check.
	if _, err := ac.Update(acc.ID, "", "", map[string]string{"apiToken": "sk-new"}); err != nil {
		t.Fatal(err)
	}

	if err := ac.Delete(acc.ID); err != nil {
		t.Fatal(err)
	}
	if ac.Get(acc.ID) != nil {
		t.Fatal("metadata still there after delete")
	}
	if _, err := os.Stat(filepath.Join(st.dir, AccountsDir, acc.ID)); !os.IsNotExist(err) {
		t.Fatal("secret dir still there after delete")
	}
	if err := ac.Delete(acc.ID); !os.IsNotExist(err) {
		t.Fatalf("deleting twice: %v", err)
	}
}

// TestUpdatePartialSecrets: a non-nil secrets map touches only the fields it
// contains; the others keep their stored values.
func TestUpdatePartialSecrets(t *testing.T) {
	_, ac := openAccounts(t)
	acc, err := ac.Create("ep", model.AcctEndpoint, "", map[string]string{"baseURL": "https://llm:8443/v1", "apiToken": "tok-1"})
	if err != nil {
		t.Fatal(err)
	}
	// Rename with an empty (non-nil) map keeps both fields.
	if _, err := ac.Update(acc.ID, "ep2", "", map[string]string{}); err != nil {
		t.Fatal(err)
	}
	if v, _ := ac.ReadSecret(acc.ID, "baseURL"); v != "https://llm:8443/v1" {
		t.Fatalf("baseURL lost on empty-map update: %q", v)
	}
	if v, _ := ac.ReadSecret(acc.ID, "apiToken"); v != "tok-1" {
		t.Fatalf("apiToken lost on empty-map update: %q", v)
	}
	// Only one field in the map: the other is untouched.
	if _, err := ac.Update(acc.ID, "", "", map[string]string{"apiToken": "tok-2"}); err != nil {
		t.Fatal(err)
	}
	if v, _ := ac.ReadSecret(acc.ID, "apiToken"); v != "tok-2" {
		t.Fatalf("apiToken not updated: %q", v)
	}
	if v, _ := ac.ReadSecret(acc.ID, "baseURL"); v != "https://llm:8443/v1" {
		t.Fatalf("baseURL wiped by a one-field update: %q", v)
	}
	// A field sent as "" clears exactly that field.
	if _, err := ac.Update(acc.ID, "", "", map[string]string{"apiToken": ""}); err != nil {
		t.Fatal(err)
	}
	if ac.Get(acc.ID).HasSecret("apiToken") {
		t.Fatal("cleared field still marked set")
	}
	if v, _ := ac.ReadSecret(acc.ID, "baseURL"); v != "https://llm:8443/v1" {
		t.Fatalf("clearing one field wiped the other: %q", v)
	}
}

func TestAccountKindValidation(t *testing.T) {
	_, ac := openAccounts(t)
	if _, err := ac.Create("", model.AcctAPIKey, "", nil); err == nil {
		t.Fatal("empty name accepted")
	}
	if _, err := ac.Create("x", model.AccountKind("nope"), "", nil); err == nil {
		t.Fatal("unknown kind accepted")
	}
	acc, err := ac.Create("vibe", model.AcctVibe, "", map[string]string{"apiKey": "k", "apiToken": "nope"})
	if err == nil {
		t.Fatalf("field of another kind accepted: %+v", acc)
	}
	if _, err := ac.Create("endpoint", model.AcctEndpoint, "", map[string]string{"baseURL": "https://llm:8443/v1", "apiToken": "t"}); err != nil {
		t.Fatal(err)
	}
}

func TestAgentsUsing(t *testing.T) {
	st, ac := openAccounts(t)
	acc, err := ac.Create("shared", model.AcctAPIKey, "", map[string]string{"apiToken": "k"})
	if err != nil {
		t.Fatal(err)
	}
	if got := ac.AgentsUsing(acc.ID); len(got) != 0 {
		t.Fatalf("empty: %v", got)
	}
	for _, id := range []string{"b", "a"} {
		if err := st.UpsertAgent(&model.Agent{ID: id, Name: "agent-" + id, AccountID: acc.ID}); err != nil {
			t.Fatal(err)
		}
	}
	if err := st.UpsertAgent(&model.Agent{ID: "c", Name: "agent-c"}); err != nil {
		t.Fatal(err)
	}
	if got := ac.AgentsUsing(acc.ID); !reflect.DeepEqual(got, []string{"agent-a", "agent-b"}) {
		t.Fatalf("AgentsUsing: %v", got)
	}
}

func TestAccountSecretsSurviveReopen(t *testing.T) {
	st, ac := openAccounts(t)
	acc, err := ac.Create("k", model.AcctClaudeCode, "", map[string]string{"token": "claude-tok"})
	if err != nil {
		t.Fatal(err)
	}
	st2, err := Open(st.dir)
	if err != nil {
		t.Fatal(err)
	}
	got := st2.Accounts().Get(acc.ID)
	if got == nil || !got.HasSecret("token") {
		t.Fatalf("reopened: %+v", got)
	}
	if v, err := st2.Accounts().ReadSecret(acc.ID, "token"); err != nil || v != "claude-tok" {
		t.Fatalf("secret after reopen: %q %v", v, err)
	}
}

func TestAccountBadIDs(t *testing.T) {
	_, ac := openAccounts(t)
	for _, id := range []string{"", "../x", "a/b", "."} {
		if _, err := ac.ReadSecret(id, "apiToken"); err == nil {
			t.Errorf("ReadSecret(%q): expected error", id)
		}
	}
	if err := ac.Delete("../escape"); err == nil {
		t.Error("Delete with a path traversal id: expected error")
	}
}

func contains(s, sub string) bool { return strings.Contains(s, sub) }
