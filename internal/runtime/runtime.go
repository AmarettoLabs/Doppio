// Package runtime deploys agents as Docker containers via the docker CLI.
// Each agent gets one container on a shared network; its full configuration
// (including secrets) is written to a 0600 file mounted read-only into the
// container, never passed via env or labels.
package runtime

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"log"
	"net"
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
	// ExtraHosts are "name:ip" entries added to every agent container's
	// /etc/hosts (docker --add-host), for hosts the container's DNS can't
	// resolve, e.g. LAN machines only reachable via mDNS on the host.
	ExtraHosts []string
	// CABundle is the path (as seen by the manager) of a PEM file with extra
	// CA certificates agents should trust on top of the system ones, e.g. the
	// CA of a self-signed local inference server.
	CABundle string
}

// extraCAFile is the extra CA bundle's name in the per-agent state dir,
// mounted in the container at /agent/state.
const extraCAFile = "extra-ca.pem"

// ParseExtraHosts parses a comma- or whitespace-separated list of
// "hostname:ip" entries (IPv6 addresses allowed: "name:fd00::1").
func ParseExtraHosts(s string) ([]string, error) {
	var out []string
	for _, e := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == ' ' || r == '\t' || r == '\n' }) {
		name, ip, ok := strings.Cut(e, ":")
		if !ok || !validHostname(name) || net.ParseIP(ip) == nil {
			return nil, fmt.Errorf("invalid extra host %q (want hostname:ip)", e)
		}
		out = append(out, name+":"+ip)
	}
	return out, nil
}

func validHostname(h string) bool {
	if h == "" || len(h) > 253 || strings.HasPrefix(h, "-") || strings.HasPrefix(h, ".") {
		return false
	}
	for _, c := range h {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '.') {
			return false
		}
	}
	return true
}

// NewFromEnv builds a Runtime from the environment: DOCKER_BIN, AGENT_IMAGE,
// AGENT_NETWORK, MANAGER_URL, AGENT_EXTRA_HOSTS, AGENT_CA_BUNDLE plus
// dataDir/token.
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
	hosts, err := ParseExtraHosts(os.Getenv("AGENT_EXTRA_HOSTS"))
	if err != nil {
		log.Printf("warning: AGENT_EXTRA_HOSTS ignored: %v", err)
		hosts = nil
	}
	return &Runtime{
		ExtraHosts:   hosts,
		CABundle:     os.Getenv("AGENT_CA_BUNDLE"),
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

// writeCABundle copies the configured extra CA bundle into the agent's state
// dir, or removes a stale copy when none is configured. Reports whether the
// agent has a bundle.
func (r *Runtime) writeCABundle(a *model.Agent) (bool, error) {
	dst := filepath.Join(r.agentDir(a.ID), extraCAFile)
	if r.CABundle == "" {
		if err := os.Remove(dst); err != nil && !os.IsNotExist(err) {
			return false, err
		}
		return false, nil
	}
	b, err := os.ReadFile(r.CABundle)
	if err != nil {
		return false, fmt.Errorf("AGENT_CA_BUNDLE: %w", err)
	}
	if err := checkPEMCerts(b); err != nil {
		return false, fmt.Errorf("AGENT_CA_BUNDLE %s: %w", r.CABundle, err)
	}
	if err := os.WriteFile(dst, b, 0o644); err != nil {
		return false, err
	}
	return true, nil
}

// checkPEMCerts verifies b holds at least one PEM certificate and that every
// certificate in it parses.
func checkPEMCerts(b []byte) error {
	n := 0
	for {
		var blk *pem.Block
		blk, b = pem.Decode(b)
		if blk == nil {
			break
		}
		if blk.Type != "CERTIFICATE" {
			continue
		}
		if _, err := x509.ParseCertificate(blk.Bytes); err != nil {
			return fmt.Errorf("certificate %d: %w", n+1, err)
		}
		n++
	}
	if n == 0 {
		return fmt.Errorf("no PEM certificates found")
	}
	return nil
}

// Start creates and starts the agent's container, returning its ID.
func (r *Runtime) Start(a *model.Agent) (string, error) {
	if _, err := r.writeConfig(a); err != nil {
		return "", err
	}
	hasCA, err := r.writeCABundle(a)
	if err != nil {
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
	}
	for _, h := range r.ExtraHosts {
		args = append(args, "--add-host", h)
	}
	if hasCA {
		args = append(args, "-e", "DOPPIO_EXTRA_CA=/agent/state/"+extraCAFile)
	}
	args = append(args, r.Image)
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
