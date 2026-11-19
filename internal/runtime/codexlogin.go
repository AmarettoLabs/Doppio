package runtime

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// CodexLoginDir is where a device-code login's codex home lives while it
// runs, relative to the data dir (removed when the login ends).
const CodexLoginDir = "codex-login"

// CodexLogin runs `codex login --device-auth` in a throwaway container from
// the agent image and returns the auth.json it writes. onLine gets each line
// codex prints (the link to open and the one-time code among them), with
// terminal colors removed. Cancelling ctx stops the container. The login
// times out on codex's side after 15 minutes.
func (r *Runtime) CodexLogin(ctx context.Context, onLine func(string)) (string, error) {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	id := hex.EncodeToString(b)
	home := filepath.Join(r.DataDir, CodexLoginDir, id)
	if err := os.MkdirAll(home, 0o700); err != nil {
		return "", err
	}
	defer os.RemoveAll(home)
	name := "doppio-codex-login-" + id
	args := []string{
		"run", "--rm", "--name", name,
		"--label", "doppio.codex-login=" + id,
		// codex keeps its login in CODEX_HOME; not under /tmp, where it
		// refuses to set up its helper binaries.
		"-v", home + ":/codex-home:z", "-e", "CODEX_HOME=/codex-home",
		// As the manager's own user, who owns that dir (0700): with every
		// capability dropped, even root can only use what it owns.
		"--user", fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid()),
		"--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--memory", "512m", "--pids-limit", "128",
	}
	for _, h := range r.ExtraHosts {
		args = append(args, "--add-host", h)
	}
	args = append(args, "--entrypoint", "codex", r.Image, "login", "--device-auth")
	cmd := exec.Command(r.DockerBin, args...)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("docker run: %v", err)
	}
	stopped := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			// Killing the docker CLI would leave the container running.
			_, _ = r.run("rm", "-f", name)
		case <-stopped:
		}
	}()
	readLines(out, onLine)
	err = cmd.Wait()
	close(stopped)
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if err != nil {
		return "", fmt.Errorf("codex login failed: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(home, "auth.json"))
	if err != nil {
		return "", errors.New("codex login ended without writing auth.json")
	}
	return string(data), nil
}

var ansiRE = regexp.MustCompile(`\x1b\[[0-9;?]*[A-Za-z]`)

// readLines hands each line of r to onLine without terminal colors.
func readLines(r io.Reader, onLine func(string)) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		if onLine != nil {
			onLine(strings.TrimRight(ansiRE.ReplaceAllString(sc.Text(), ""), " \r"))
		}
	}
}

// RemoveStaleCodexLogins removes login homes and containers a manager
// restart left behind: a login never survives one.
func (r *Runtime) RemoveStaleCodexLogins() {
	if out, err := r.run("ps", "-aq", "--filter", "label=doppio.codex-login"); err == nil {
		for _, c := range strings.Fields(out) {
			_, _ = r.run("rm", "-f", c)
		}
	}
	_ = os.RemoveAll(filepath.Join(r.DataDir, CodexLoginDir))
}

// CodexLoginTimeout bounds a device-code login: codex gives up after 15
// minutes, the manager shortly after.
const CodexLoginTimeout = 16 * time.Minute
