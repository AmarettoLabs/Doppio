package runtime

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// TestAgentImageShipsTools pins what deploy/Dockerfile.agent installs: the
// coding CLIs, the git hosts' CLIs and the toolchains agents test a checkout
// with. Each version must stay a build arg, so a bump is a one-line change,
// every download must be checksummed and happen in a build stage, and the
// worker itself must still be copied in.
func TestAgentImageShipsTools(t *testing.T) {
	data, err := os.ReadFile("../../deploy/Dockerfile.agent")
	if err != nil {
		t.Fatal(err)
	}
	// Join wrapped lines so a command split over several lines is checked as
	// a whole.
	f := strings.ReplaceAll(string(data), "\\\n", " ")
	for _, arg := range []string{
		"CLAUDE_CODE_VERSION", "QWEN_CODE_VERSION", "CODEX_VERSION", "VIBE_VERSION",
		"GH_VERSION", "GLAB_VERSION", "GO_VERSION",
	} {
		if !regexp.MustCompile(`(?m)^ARG ` + arg + `=\d`).MatchString(f) {
			t.Errorf("%s is not pinned as a build arg in deploy/Dockerfile.agent", arg)
		}
	}
	// The checksums are build args next to their version, and each must be a
	// real SHA-256 (64 hex characters), or the ADD below fails the build.
	checksums := regexp.MustCompile(`(?m)^ARG ([A-Z_]+_SHA256_(?:AMD64|ARM64))=(\S+)$`).FindAllStringSubmatch(f, -1)
	if len(checksums) == 0 {
		t.Error("the download checksums are not pinned as build args in deploy/Dockerfile.agent")
	}
	for _, m := range checksums {
		if !regexp.MustCompile(`^[0-9a-fA-F]{64}$`).MatchString(m[2]) {
			t.Errorf("%s is not a SHA-256 hash (64 hex characters): %s", m[1], m[2])
		}
	}
	// Every download must be verified, and only in a build stage: an ADD
	// from a URL without --checksum would let a replaced release in
	// silently, and one in the final stage would ship the archive in the
	// image (a file deleted in a later layer still takes space in its own).
	final := f[strings.LastIndex(f, "\nFROM "):]
	for _, line := range strings.Split(f, "\n") {
		if !strings.HasPrefix(line, "ADD") || !strings.Contains(line, "https://") {
			continue
		}
		if !strings.Contains(line, "--checksum=sha256:") {
			t.Errorf("download is not verified with ADD --checksum: %s", strings.TrimSpace(line))
		}
		if strings.Contains(final, line) {
			t.Errorf("download belongs in a build stage, not the final image: %s", strings.TrimSpace(line))
		}
	}
	if !strings.Contains(f, "COPY deploy/agent/agent_worker.py /agent/agent_worker.py") {
		t.Error("the agent worker is not copied into the image")
	}
	// vibe's entry points are thin scripts that import its Python package, so
	// its venv must come along or `vibe` exits with ModuleNotFoundError; the
	// system site-packages must not be overwritten with vibe's dependencies.
	if !strings.Contains(f, "COPY --from=vibe-cli /opt/vibe /opt/vibe") {
		t.Error("vibe's virtual environment is not copied into the image")
	}
	if strings.Contains(f, "/usr/local/lib/python3.14/site-packages") {
		t.Error("vibe must stay in its venv, not in the system site-packages")
	}
}
