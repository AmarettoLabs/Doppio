package runtime

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpdateAccountRewritesDeployedAgentsOnly(t *testing.T) {
	r := &Runtime{DataDir: t.TempDir()}
	// Not deployed yet: nothing to update, no dir created.
	if err := r.UpdateAccount("a1", map[string]string{"authJson": "{}"}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(r.agentDir("a1")); !os.IsNotExist(err) {
		t.Fatal("UpdateAccount created the state dir of an agent never started")
	}
	if err := os.MkdirAll(r.agentDir("a1"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := r.UpdateAccount("a1", map[string]string{"authJson": `{"v":2}`}); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(r.agentDir("a1"), accountFile)
	b, err := os.ReadFile(p)
	if err != nil || !strings.Contains(string(b), `{\"v\":2}`) {
		t.Fatalf("account file: %v %s", err, b)
	}
	if fi, _ := os.Stat(p); fi.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", fi.Mode().Perm())
	}
	// No temporary files left next to it.
	ents, _ := os.ReadDir(r.agentDir("a1"))
	if len(ents) != 1 {
		t.Fatalf("leftovers in the state dir: %v", ents)
	}
}

func TestReadLinesStripsColors(t *testing.T) {
	var got []string
	readLines(strings.NewReader("1. Open\n   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n   \x1b[94mABCD-12345\x1b[0m  \n"),
		func(l string) { got = append(got, l) })
	if len(got) != 3 || got[1] != "   https://auth.openai.com/codex/device" || got[2] != "   ABCD-12345" {
		t.Fatalf("lines: %q", got)
	}
}
