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
// and the worker itself must still be copied in.
func TestAgentImageShipsTools(t *testing.T) {
	data, err := os.ReadFile("../../deploy/Dockerfile.agent")
	if err != nil {
		t.Fatal(err)
	}
	f := string(data)
	for _, arg := range []string{
		"CLAUDE_CODE_VERSION", "QWEN_CODE_VERSION", "CODEX_VERSION", "VIBE_VERSION",
		"GH_VERSION", "GLAB_VERSION", "GO_VERSION",
	} {
		if !regexp.MustCompile(`(?m)^ARG ` + arg + `=\d`).MatchString(f) {
			t.Errorf("%s is not pinned as a build arg in deploy/Dockerfile.agent", arg)
		}
	}
	for _, tool := range []string{"claude", "codex", "vibe", "qwen", "gh", "glab", "go", "node"} {
		if !strings.Contains(f, tool) {
			t.Errorf("deploy/Dockerfile.agent does not mention %s", tool)
		}
	}
	if !strings.Contains(f, "COPY deploy/agent/agent_worker.py /agent/agent_worker.py") {
		t.Error("the agent worker is not copied into the image")
	}
	// vibe's entry points are thin scripts that import its Python package, so
	// site-packages must come along or `vibe` exits with ModuleNotFoundError.
	if !strings.Contains(f, "COPY --from=vibe-cli /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages") {
		t.Error("vibe's Python package is not copied into the image")
	}
}
