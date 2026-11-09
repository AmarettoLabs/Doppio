package runtime

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

func TestParseExtraHosts(t *testing.T) {
	got, err := ParseExtraHosts(" llm-server:192.168.1.50, gpu-box.lan:10.0.0.2\nv6:fd00::1 ")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"llm-server:192.168.1.50", "gpu-box.lan:10.0.0.2", "v6:fd00::1"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if got, err := ParseExtraHosts(""); err != nil || got != nil {
		t.Fatalf("empty: got %v, %v", got, err)
	}
	for _, bad := range []string{"llm-server", "llm-server:notanip", ":1.2.3.4", "-x:1.2.3.4", "a b:1.2.3.4", "a_b:1.2.3.4"} {
		if _, err := ParseExtraHosts(bad); err == nil {
			t.Errorf("%q: expected error", bad)
		}
	}
}

func selfSignedPEM(t *testing.T) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test-ca"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IsCA:         true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func TestWriteCABundle(t *testing.T) {
	dir := t.TempDir()
	r := &Runtime{DataDir: dir}
	a := &model.Agent{ID: "a1"}
	if err := os.MkdirAll(r.agentDir(a.ID), 0o755); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(r.agentDir(a.ID), extraCAFile)

	src := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(src, selfSignedPEM(t), 0o644); err != nil {
		t.Fatal(err)
	}
	r.CABundle = src
	if ok, err := r.writeCABundle(a); err != nil || !ok {
		t.Fatalf("valid bundle: %v, %v", ok, err)
	}
	if _, err := os.Stat(dst); err != nil {
		t.Fatal(err)
	}

	// Unset: the stale copy is removed.
	r.CABundle = ""
	if ok, err := r.writeCABundle(a); err != nil || ok {
		t.Fatalf("unset: %v, %v", ok, err)
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatalf("stale bundle not removed: %v", err)
	}

	// Garbage and missing files are rejected.
	bad := filepath.Join(dir, "bad.pem")
	if err := os.WriteFile(bad, []byte("not a cert"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{bad, filepath.Join(dir, "missing.pem")} {
		r.CABundle = p
		if _, err := r.writeCABundle(a); err == nil {
			t.Errorf("%s: expected error", p)
		}
	}
}

func TestWriteAccount(t *testing.T) {
	dir := t.TempDir()
	r := &Runtime{DataDir: dir}
	a := &model.Agent{ID: "a1"}
	if err := os.MkdirAll(r.agentDir(a.ID), 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(r.agentDir(a.ID), accountFile)

	// No account: nothing written, a stale file is removed.
	if err := os.WriteFile(p, []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := r.writeAccount(a, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatal("stale account file not removed")
	}

	// With an account: a 0600 file with the credentials.
	secrets := map[string]string{"token": "claude-tok", "apiKey": "vibe-key"}
	if err := r.writeAccount(a, secrets); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(p)
	if err != nil || !strings.Contains(string(b), "claude-tok") {
		t.Fatalf("account file: %v %s", err, b)
	}
	fi, err := os.Stat(p)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("account file mode: %v %v", fi.Mode().Perm(), err)
	}
}
