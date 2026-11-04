// Command server is the Doppio backend. It serves the JSON API and
// the built Svelte SPA, and deploys each agent as a Docker container that it
// controls through the docker CLI (docker socket mounted in compose).
//
// Environment:
//
//	ADDR             listen address (default :8080)
//	DATA_DIR         state directory (default ./data)
//	STATIC_DIR       built SPA directory (default ./web/build)
//	AGENT_IMAGE      image for agent containers (default doppio-agent:latest)
//	AGENT_NETWORK    shared docker network for agents (default doppio_agents)
//	MANAGER_URL      URL agent containers use to reach this API (default http://manager:8080)
//	PORTAL_PASSWORD  if set, the portal requires login with this password
//	MAX_EVENTS_PER_AGENT  persisted events per agent (default 5000, 0 = unlimited)
//	DOCKER_BIN       docker CLI binary (default docker)
package main

import (
	"crypto/rand"
	"encoding/hex"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"

	"github.com/AmarettoLabs/Doppio/internal/api"
	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/runtime"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// managerToken returns the persistent internal token that agent containers
// must present to the /api/internal/* endpoints.
func managerToken(dataDir string) (string, error) {
	p := filepath.Join(dataDir, "manager-token")
	if b, err := os.ReadFile(p); err == nil && len(b) > 0 {
		return string(b), nil
	}
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(b)
	if err := os.WriteFile(p, []byte(tok), 0o600); err != nil {
		return "", err
	}
	return tok, nil
}

func main() {
	addr := getenv("ADDR", ":8080")
	dataDir := getenv("DATA_DIR", "./data")
	staticDir := getenv("STATIC_DIR", "./web/build")
	password := os.Getenv("PORTAL_PASSWORD")

	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		log.Fatal(err)
	}
	token, err := managerToken(dataDir)
	if err != nil {
		log.Fatal(err)
	}
	st, err := store.Open(dataDir)
	if err != nil {
		log.Fatal(err)
	}
	// Cap the persisted event log per agent (0 = keep everything).
	retention := 5000
	if v := os.Getenv("MAX_EVENTS_PER_AGENT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			retention = n
		}
	}
	st.SetEventRetention(retention)
	// Tasks whose worker died with a restart go back to pending (fresh context).
	for _, t := range st.RequeueRunningTasks() {
		st.AppendEvents(t.AgentID, t.ID, []model.Event{{Type: "status", Content: "task requeued after manager restart (context starts fresh)"}})
	}
	rt := runtime.NewFromEnv(dataDir, token)
	if err := rt.EnsureNetwork(); err != nil {
		log.Printf("warning: %v", err)
	}

	srv := api.NewServer(st, rt, password, staticDir, token)
	mode := "open"
	if password != "" {
		mode = "password protected"
	}
	log.Printf("doppio listening on %s (portal: %s, data: %s)", addr, mode, dataDir)
	log.Fatal(http.ListenAndServe(addr, srv.Handler()))
}
