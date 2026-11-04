// Package runtime deploys agents as Docker containers via the docker CLI.
// Each agent gets one container on a shared network; its full configuration
// (including secrets) is written to a 0600 file mounted read-only into the
// container, never passed via env or labels.
package runtime

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

// Runtime creates and controls agent containers.
type Runtime struct {
	DockerBin    string
	Image        string
	Network      string
	DataDir      string
	ManagerURL   string
	ManagerToken string
}

// NewFromEnv builds a Runtime from the environment:
// DOCKER_BIN, AGENT_IMAGE, AGENT_NETWORK, MANAGER_URL plus dataDir/token.
func NewFromEnv(dataDir, managerToken string) *Runtime {
	get := func(k, def string) string {
		if v := os.Getenv(k); v != "" {
			return v
		}
		return def
	}
	// Bind mounts need absolute paths.
	absData, err := filepath.Abs(dataDir)
	if err != nil {
		absData = dataDir
	}
	return &Runtime{
		DockerBin:    get("DOCKER_BIN", "docker"),
		Image:        get("AGENT_IMAGE", "doppio-agent:latest"),
		Network:      get("AGENT_NETWORK", "doppio_agents"),
		DataDir:      absData,
		ManagerURL:   get("MANAGER_URL", "http://manager:8080"),
		ManagerToken: managerToken,
	}
}

func (r *Runtime) run(args ...string) (string, error) {
	cmd := exec.Command(r.DockerBin, args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// ContainerName is the deterministic container name for an agent.
func ContainerName(agentID string) string { return "doppio-agent-" + agentID }

// EnsureNetwork creates the shared network if missing.
func (r *Runtime) EnsureNetwork() error {
	if _, err := r.run("network", "inspect", r.Network); err != nil {
		if _, err := r.run("network", "create", r.Network); err != nil {
			return fmt.Errorf("docker network create %s: %v", r.Network, err)
		}
	}
	return nil
}

func (r *Runtime) agentDir(id string) string {
	return filepath.Join(r.DataDir, "agents", id)
}

// AgentToken derives the internal-API token of one agent from the manager's
// master token. Each container only receives its own token, so a compromised
// agent cannot act on behalf of other agents.
func AgentToken(masterToken, agentID string) string {
	mac := hmac.New(sha256.New, []byte(masterToken))
	mac.Write([]byte("agent:" + agentID))
	return hex.EncodeToString(mac.Sum(nil))
}

// writeConfig writes the agent's full config (with secrets) to a 0600 file.
func (r *Runtime) writeConfig(a *model.Agent) (string, error) {
	dir := r.agentDir(a.ID)
	if err := os.MkdirAll(filepath.Join(dir, "workspace"), 0o755); err != nil {
		return "", err
	}
	cfg := map[string]any{
		"agent":        a,
		"managerUrl":   r.ManagerURL,
		"managerToken": AgentToken(r.ManagerToken, a.ID),
	}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", err
	}
	p := filepath.Join(dir, "config.json")
	// A stale directory at this path (e.g. left by a failed single-file bind)
	// would make WriteFile fail; remove it first.
	if st, err := os.Stat(p); err == nil && st.IsDir() {
		if err := os.RemoveAll(p); err != nil {
			return "", err
		}
	}
	if err := os.WriteFile(p, b, 0o600); err != nil {
		return "", err
	}
	return p, nil
}

// Start creates and starts the agent's container, returning its ID.
func (r *Runtime) Start(a *model.Agent) (string, error) {
	if _, err := r.writeConfig(a); err != nil {
		return "", err
	}
	name := ContainerName(a.ID)
	_, _ = r.run("rm", "-f", name) // clean slate; ignore "no such container"
	args := []string{
		"run", "-d",
		"--name", name,
		"--network", r.Network,
		"--restart", "no",
		"--add-host", "host.docker.internal:host-gateway",
		"--label", "doppio.agent-id=" + a.ID,
		// Mount the whole per-agent dir read-only (single-file binds from a
		// volume misbehave), plus the workspace read-write. "z" relabels the
		// mounts for SELinux hosts (e.g. Fedora); a no-op elsewhere.
		"-v", r.agentDir(a.ID) + ":/agent/state:ro,z",
		"-v", filepath.Join(r.agentDir(a.ID), "workspace") + ":/agent/workspace:z",
		"-e", "AGENT_CONFIG=/agent/state/config.json",
		"-e", "AGENT_WORKSPACE=/agent/workspace",
		r.Image,
	}
	out, err := r.run(args...)
	if err != nil {
		return "", fmt.Errorf("docker run: %v: %s", err, strings.TrimSpace(out))
	}
	return strings.TrimSpace(out), nil
}

// Stop stops and removes the agent's container.
func (r *Runtime) Stop(agentID string) error {
	name := ContainerName(agentID)
	_, err1 := r.run("stop", name)
	_, err2 := r.run("rm", "-f", name)
	if err1 != nil && err2 != nil {
		return fmt.Errorf("docker stop/rm %s: %v / %v", name, err1, err2)
	}
	return nil
}

// RemoveState deletes the agent's on-disk state: its config (with secrets),
// git credentials and workspace.
func (r *Runtime) RemoveState(agentID string) error {
	if agentID == "" || strings.ContainsAny(agentID, `/\`) || agentID == "." || agentID == ".." {
		return fmt.Errorf("invalid agent id %q", agentID)
	}
	return os.RemoveAll(r.agentDir(agentID))
}

// Status returns the container state: running, exited, created, ... or
// "not-found".
func (r *Runtime) Status(agentID string) string {
	out, err := r.run("inspect", "-f", "{{.State.Status}}", ContainerName(agentID))
	if err != nil {
		return "not-found"
	}
	return strings.TrimSpace(out)
}
