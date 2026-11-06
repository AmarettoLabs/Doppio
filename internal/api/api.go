// Package api implements the manager's HTTP API: the public JSON API used by
// the Svelte UI, the internal ingest API used by agent worker containers, the
// SSE event stream, optional portal authentication, and static SPA serving.
package api

import (
	"crypto/hmac"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/runtime"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

// Containers runs agent containers; *runtime.Runtime in production, a fake
// in tests.
type Containers interface {
	EnsureNetwork() error
	Start(a *model.Agent) (string, error)
	Stop(agentID string) error
	Status(agentID string) string
	RemoveState(agentID string) error
}

// Server wires the store, runtime and routes together.
type Server struct {
	store          *store.Store
	rt             Containers
	portalPassword string // empty = portal is open
	staticDir      string
	managerToken   string
	broker         *broker

	// restartMu serializes restarts that apply saved settings; restarts
	// tracks the ones running in the background (tests wait on it).
	restartMu sync.Mutex
	restarts  sync.WaitGroup
}

// NewServer builds the API server.
func NewServer(st *store.Store, rt Containers, portalPassword, staticDir, managerToken string) *Server {
	return &Server{
		store:          st,
		rt:             rt,
		portalPassword: portalPassword,
		staticDir:      staticDir,
		managerToken:   managerToken,
		broker:         newBroker(),
	}
}

// Handler returns the root HTTP handler with auth applied.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	s.routes(mux)
	return s.authMiddleware(mux)
}

func (s *Server) routes(mux *http.ServeMux) {
	// public API (UI)
	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("GET /api/auth", s.handleAuthInfo)
	mux.HandleFunc("POST /api/login", s.handleLogin)
	mux.HandleFunc("POST /api/logout", s.handleLogout)
	mux.HandleFunc("GET /api/providers", s.handleProviders)
	mux.HandleFunc("GET /api/agents", s.handleListAgents)
	mux.HandleFunc("POST /api/agents", s.handleCreateAgent)
	mux.HandleFunc("GET /api/agents/{id}", s.handleGetAgent)
	mux.HandleFunc("PATCH /api/agents/{id}", s.handleUpdateAgent)
	mux.HandleFunc("DELETE /api/agents/{id}", s.handleDeleteAgent)
	mux.HandleFunc("POST /api/agents/{id}/start", s.handleStart)
	mux.HandleFunc("POST /api/agents/{id}/stop", s.handleStop)
	mux.HandleFunc("POST /api/agents/{id}/restart", s.handleRestart)
	mux.HandleFunc("GET /api/agents/{id}/tasks", s.handleListTasks)
	mux.HandleFunc("POST /api/agents/{id}/tasks", s.handleCreateTask)
	mux.HandleFunc("GET /api/agents/{id}/events", s.handleEvents)
	mux.HandleFunc("GET /api/agents/{id}/stream", s.handleStream)
	mux.HandleFunc("GET /api/tasks/{id}", s.handleGetTask)

	// internal API (agent worker containers, authenticated by manager token)
	mux.HandleFunc("GET /api/internal/agents/{id}/ping", s.handleInternalPing)
	mux.HandleFunc("POST /api/internal/agents/{id}/tasks", s.handleInternalCreateTask)
	mux.HandleFunc("GET /api/internal/agents/{id}/next-task", s.handleInternalNextTask)
	mux.HandleFunc("POST /api/internal/agents/{id}/events", s.handleInternalEvents)
	mux.HandleFunc("POST /api/internal/agents/{id}/poll", s.handleInternalPoll)
	mux.HandleFunc("POST /api/internal/tasks/{id}/finish", s.handleInternalFinishTask)

	// static SPA with fallback to index.html
	mux.HandleFunc("GET /", s.handleStatic)
}

// ---------- helpers ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return false
	}
	return true
}

// AgentView is the agent as exposed to the UI: secrets are never sent, only
// whether they are set.
type AgentView struct {
	ID          string     `json:"id"`
	Name        string     `json:"name"`
	Persona     string     `json:"persona"`
	Task        string     `json:"task"`
	Provider    string     `json:"provider"`
	Model       string     `json:"model"`
	APIBase     string     `json:"apiBase,omitempty"`
	HasAPIToken bool       `json:"hasApiToken"`
	Emails      []string   `json:"emails"`
	GitProvider string     `json:"gitProvider,omitempty"`
	GitBaseURL  string     `json:"gitBaseURL,omitempty"`
	HasGitToken bool       `json:"hasGitToken"`
	GitLogin    string     `json:"gitLogin,omitempty"`
	GitRepos    []string   `json:"gitRepos"`
	OwnerLogin  string     `json:"ownerLogin,omitempty"`
	PeerLogins  []string   `json:"peerLogins"`
	Triggers    []string   `json:"triggers"`
	PollSeconds int        `json:"pollSeconds"`
	MaxTurns    int        `json:"maxTurns"`
	Status      string     `json:"status"`
	ContainerID string     `json:"containerId,omitempty"`
	Error       string     `json:"error,omitempty"`
	LastPollAt  *time.Time `json:"lastPollAt,omitempty"`
	NextPollAt  *time.Time `json:"nextPollAt,omitempty"`
	// Working is true while one of the agent's tasks is running or pending.
	// Only set by the list/get endpoints.
	Working bool `json:"working"`
	// RestartPending: saved settings wait for the current work to finish.
	RestartPending bool      `json:"restartPending"`
	CreatedAt      time.Time `json:"createdAt"`
	UpdatedAt      time.Time `json:"updatedAt"`
}

func toView(a *model.Agent) AgentView {
	return AgentView{
		ID: a.ID, Name: a.Name, Persona: a.Persona, Task: a.Task,
		Provider: string(a.Provider), Model: a.Model, APIBase: a.APIBase,
		HasAPIToken: a.APIToken != "", Emails: a.Emails,
		GitProvider: string(a.GitProvider), GitBaseURL: a.GitBaseURL,
		HasGitToken: a.GitToken != "", GitLogin: a.GitLogin, GitRepos: a.GitRepos,
		OwnerLogin: a.OwnerLogin, PeerLogins: a.PeerLogins, Triggers: triggerStrings(a.EffectiveTriggers()),
		PollSeconds: a.PollSeconds,
		MaxTurns:    maxTurnsOrDefault(a.MaxTurns),
		Status:      a.Status, ContainerID: a.ContainerID, Error: a.Error,
		LastPollAt: a.LastPollAt, NextPollAt: a.NextPollAt,
		RestartPending: a.RestartPending,
		CreatedAt:      a.CreatedAt, UpdatedAt: a.UpdatedAt,
	}
}

// agentInput is the create/update payload. Empty token fields mean "keep the
// existing token" on update.
type agentInput struct {
	Name        string   `json:"name"`
	Persona     string   `json:"persona"`
	Task        string   `json:"task"`
	Provider    string   `json:"provider"`
	Model       string   `json:"model"`
	APIBase     string   `json:"apiBase"`
	APIToken    string   `json:"apiToken"`
	Emails      []string `json:"emails"`
	GitProvider string   `json:"gitProvider"`
	GitBaseURL  string   `json:"gitBaseURL"`
	GitToken    string   `json:"gitToken"`
	GitLogin    string   `json:"gitLogin"`
	GitRepos    []string `json:"gitRepos"`
	OwnerLogin  string   `json:"ownerLogin"`
	PeerLogins  []string `json:"peerLogins"`
	// Triggers: nil keeps the stored ones on update (all of them on create).
	Triggers    []string `json:"triggers"`
	PollSeconds int      `json:"pollSeconds"`
	MaxTurns    int      `json:"maxTurns"`
}

func triggerStrings(ts []model.Trigger) []string {
	out := make([]string, len(ts))
	for i, t := range ts {
		out[i] = string(t)
	}
	return out
}

// parseTriggers dedupes the input triggers; nil stays nil.
func parseTriggers(in []string) []model.Trigger {
	if in == nil {
		return nil
	}
	seen := map[model.Trigger]bool{}
	out := []model.Trigger{}
	for _, s := range in {
		t := model.Trigger(strings.TrimSpace(s))
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	return out
}

// maxTurnsOrDefault maps the stored 0 (unset) to the default of 25; -1 means
// unlimited and is preserved.
func maxTurnsOrDefault(v int) int {
	if v == 0 {
		return 25
	}
	return v
}

// normalizeRepo accepts "owner/repo", URLs and .git suffixes -> "owner/repo".
func normalizeRepo(r string) string {
	r = strings.TrimSpace(r)
	r = strings.TrimSuffix(r, "/")
	r = strings.TrimSuffix(r, ".git")
	if i := strings.Index(r, "://"); i >= 0 {
		r = r[i+3:]
		if j := strings.Index(r, "/"); j >= 0 {
			r = r[j+1:]
		}
	} else if strings.HasPrefix(r, "git@") {
		if i := strings.Index(r, ":"); i >= 0 {
			r = r[i+1:]
		}
	}
	return strings.Trim(r, "/")
}

func normalizeLogin(s string) string {
	return strings.TrimPrefix(strings.TrimSpace(s), "@")
}

func normalizeAgentInput(in *agentInput) {
	in.GitLogin = normalizeLogin(in.GitLogin)
	in.OwnerLogin = normalizeLogin(in.OwnerLogin)
	for i, p := range in.PeerLogins {
		in.PeerLogins[i] = normalizeLogin(p)
	}
	for i, r := range in.GitRepos {
		in.GitRepos[i] = normalizeRepo(r)
	}
}

func validProvider(p string) bool {
	for _, pi := range model.Providers() {
		if pi.ID == p {
			return true
		}
	}
	return false
}

func (in *agentInput) validate() string {
	if strings.TrimSpace(in.Name) == "" {
		return "name is required"
	}
	if strings.TrimSpace(in.Provider) == "" {
		return "provider is required (e.g. openai, anthropic, mistral — or a custom one)"
	}
	if strings.TrimSpace(in.Model) == "" {
		return "model is required"
	}
	if strings.TrimSpace(in.Persona) == "" {
		return "persona is required"
	}
	if strings.TrimSpace(in.Task) == "" {
		return "task is required"
	}
	gp := in.GitProvider
	if gp == "" {
		gp = string(model.GitNone)
	}
	if gp != string(model.GitNone) && gp != string(model.GitGitHub) && gp != string(model.GitGitLab) {
		return "gitProvider must be none, github or gitlab"
	}
	if gp != string(model.GitNone) && (in.GitToken == "" || in.GitLogin == "") {
		return "gitToken and gitLogin are required when a git provider is set"
	}
	if gp != string(model.GitNone) && normalizeLogin(in.OwnerLogin) == "" {
		return "ownerLogin is required when a git provider is set: the agent only acts on its owner's requests"
	}
	if in.Triggers != nil {
		ts := parseTriggers(in.Triggers)
		if len(ts) == 0 {
			return "pick at least one trigger"
		}
		for _, t := range ts {
			if !model.ValidTrigger(t) {
				return fmt.Sprintf("unknown trigger %q (want review_requests, mentions, assigned_issues or assigned_prs)", t)
			}
		}
	}
	if in.PollSeconds == 0 {
		in.PollSeconds = 1800
	}
	if in.PollSeconds < 5 {
		return "pollSeconds must be at least 5"
	}
	if in.MaxTurns == 0 {
		in.MaxTurns = 25
	}
	if in.MaxTurns < -1 {
		return "maxTurns must be -1 (unlimited) or >= 1"
	}
	return ""
}

// ---------- broker (SSE fan-out) ----------

type broker struct {
	mu   sync.Mutex
	subs map[string]map[chan model.Event]struct{}
}

func newBroker() *broker { return &broker{subs: map[string]map[chan model.Event]struct{}{}} }

func (b *broker) subscribe(agentID string) chan model.Event {
	ch := make(chan model.Event, 256)
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.subs[agentID] == nil {
		b.subs[agentID] = map[chan model.Event]struct{}{}
	}
	b.subs[agentID][ch] = struct{}{}
	return ch
}

func (b *broker) unsubscribe(agentID string, ch chan model.Event) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if m := b.subs[agentID]; m != nil {
		delete(m, ch)
		if len(m) == 0 {
			delete(b.subs, agentID)
		}
	}
}

func (b *broker) publish(agentID string, ev model.Event) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for ch := range b.subs[agentID] {
		select {
		case ch <- ev:
		default: // slow consumer: drop rather than block
		}
	}
}

// ---------- public handlers ----------

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"time":   time.Now().UTC().Format(time.RFC3339),
		"auth":   s.portalPassword != "",
	})
}

func (s *Server) handleAuthInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"required": s.portalPassword != ""})
}

func (s *Server) handleProviders(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"llm": model.Providers(),
		"git": model.GitProviders(),
	})
}

func (s *Server) handleListAgents(w http.ResponseWriter, _ *http.Request) {
	agents := s.store.Agents()
	views := make([]AgentView, 0, len(agents))
	working := s.store.WorkingAgents()
	for _, a := range agents {
		a.Status = liveStatus(s.rt, a)
		v := toView(a)
		v.Working = working[a.ID]
		views = append(views, v)
	}
	writeJSON(w, http.StatusOK, views)
}

func (s *Server) handleGetAgent(w http.ResponseWriter, r *http.Request) {
	a := s.store.GetAgent(r.PathValue("id"))
	if a == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	a.Status = liveStatus(s.rt, a)
	v := toView(a)
	v.Working = s.store.WorkingAgents()[a.ID]
	writeJSON(w, http.StatusOK, v)
}

// liveStatus resolves the container state, mapping "not-found" to the stored
// status (created) or stopped.
func liveStatus(rt Containers, a *model.Agent) string {
	st := rt.Status(a.ID)
	if st == "not-found" {
		if a.Status == "created" || a.Status == "running" || a.Status == "error" {
			if a.Status == "running" {
				return "stopped" // container is gone
			}
			return a.Status
		}
		return "stopped"
	}
	return st
}

func (s *Server) handleCreateAgent(w http.ResponseWriter, r *http.Request) {
	var in agentInput
	if !decode(w, r, &in) {
		return
	}
	if msg := in.validate(); msg != "" {
		writeErr(w, http.StatusBadRequest, msg)
		return
	}
	normalizeAgentInput(&in)
	now := time.Now().UTC()
	a := &model.Agent{
		ID:          newAgentID(),
		Name:        strings.TrimSpace(in.Name),
		Persona:     in.Persona,
		Task:        in.Task,
		Provider:    model.LLMProvider(in.Provider),
		Model:       strings.TrimSpace(in.Model),
		APIBase:     strings.TrimSpace(in.APIBase),
		APIToken:    in.APIToken,
		Emails:      cleanList(in.Emails),
		GitProvider: model.GitProvider(orDefault(in.GitProvider, string(model.GitNone))),
		GitBaseURL:  strings.TrimSpace(in.GitBaseURL),
		GitToken:    in.GitToken,
		GitLogin:    strings.TrimSpace(in.GitLogin),
		GitRepos:    cleanList(in.GitRepos),
		OwnerLogin:  strings.TrimSpace(in.OwnerLogin),
		PeerLogins:  cleanList(in.PeerLogins),
		Triggers:    parseTriggers(in.Triggers),
		PollSeconds: in.PollSeconds,
		MaxTurns:    in.MaxTurns,
		Status:      "created",
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.store.UpsertAgent(a); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.store.AppendEvents(a.ID, "", []model.Event{{Type: "status", Content: "agent created"}})
	writeJSON(w, http.StatusCreated, toView(a))
}

func (s *Server) handleUpdateAgent(w http.ResponseWriter, r *http.Request) {
	a := s.store.GetAgent(r.PathValue("id"))
	if a == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	var in agentInput
	if !decode(w, r, &in) {
		return
	}
	// Empty token fields keep the stored token.
	if in.APIToken == "" {
		in.APIToken = a.APIToken
	}
	if in.GitToken == "" {
		in.GitToken = a.GitToken
	}
	if msg := in.validate(); msg != "" {
		writeErr(w, http.StatusBadRequest, msg)
		return
	}
	normalizeAgentInput(&in)
	a.Name = strings.TrimSpace(in.Name)
	a.Persona = in.Persona
	a.Task = in.Task
	a.Provider = model.LLMProvider(in.Provider)
	a.Model = strings.TrimSpace(in.Model)
	a.APIBase = strings.TrimSpace(in.APIBase)
	a.APIToken = in.APIToken
	a.Emails = cleanList(in.Emails)
	a.GitProvider = model.GitProvider(orDefault(in.GitProvider, string(model.GitNone)))
	a.GitBaseURL = strings.TrimSpace(in.GitBaseURL)
	a.GitToken = in.GitToken
	a.GitLogin = strings.TrimSpace(in.GitLogin)
	a.GitRepos = cleanList(in.GitRepos)
	a.OwnerLogin = strings.TrimSpace(in.OwnerLogin)
	a.PeerLogins = cleanList(in.PeerLogins)
	if in.Triggers != nil {
		a.Triggers = parseTriggers(in.Triggers)
	}
	a.PollSeconds = in.PollSeconds
	a.MaxTurns = in.MaxTurns
	a.UpdatedAt = time.Now().UTC()
	if err := s.store.UpsertAgent(a); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.store.AppendEvents(a.ID, "", []model.Event{{Type: "status", Content: "agent updated"}})
	writeJSON(w, http.StatusOK, s.applySettings(a))
}

// busy reports whether the agent's worker is in the middle of something a
// restart would break: a task running or waiting to be claimed, or a git
// check (the worker clears nextPollAt while it checks).
func (s *Server) busy(a *model.Agent) bool {
	return s.store.WorkingAgents()[a.ID] || (a.LastPollAt != nil && a.NextPollAt == nil)
}

// applySettings gets saved settings to a running agent, whose container only
// reads its config at start: an idle agent is restarted now; a busy one is
// marked and restarted when its worker next asks for work (see
// handleInternalNextTask). A stopped agent uses them at its next start.
func (s *Server) applySettings(a *model.Agent) AgentView {
	if liveStatus(s.rt, a) != "running" {
		return s.view(a)
	}
	s.restartMu.Lock()
	defer s.restartMu.Unlock()
	if s.busy(a) {
		if !a.RestartPending {
			a.RestartPending = true
			_ = s.store.UpsertAgent(a)
			s.event(a.ID, "status", "restart pending: the new settings apply after the current work")
		}
		return s.view(a)
	}
	if started, _, err := s.restart(a.ID, "restarting to apply the new settings"); err == nil {
		a = started
	} else if fresh := s.store.GetAgent(a.ID); fresh != nil {
		a = fresh
	}
	return s.view(a)
}

// applyPendingRestart restarts an agent whose settings changed while it was
// busy, once it's idle. Runs in the background: the request that triggers
// it comes from the container being restarted.
func (s *Server) applyPendingRestart(id string) {
	s.restarts.Add(1)
	go func() {
		defer s.restarts.Done()
		s.restartMu.Lock()
		defer s.restartMu.Unlock()
		a := s.store.GetAgent(id)
		if a == nil || !a.RestartPending {
			return // already applied by another start/stop
		}
		_, _, _ = s.restart(id, "restarting to apply the new settings")
	}()
}

// view is toView plus the fields resolved from the runtime and tasks.
func (s *Server) view(a *model.Agent) AgentView {
	a.Status = liveStatus(s.rt, a)
	v := toView(a)
	v.Working = s.store.WorkingAgents()[a.ID]
	return v
}

// event records and broadcasts one agent-level event.
func (s *Server) event(id, typ, content string) {
	for _, ev := range s.store.AppendEvents(id, "", []model.Event{{Type: typ, Content: content}}) {
		s.broker.publish(id, ev)
	}
}

func (s *Server) handleDeleteAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if s.store.GetAgent(id) == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	_ = s.rt.Stop(id)
	if err := s.store.DeleteAgent(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	// The container is gone; drop its config (secrets) and workspace too.
	if err := s.rt.RemoveState(id); err != nil {
		writeErr(w, http.StatusInternalServerError, "agent deleted but its state could not be removed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
}

func (s *Server) handleStart(w http.ResponseWriter, r *http.Request) {
	s.startAgent(w, r.PathValue("id"))
}

func (s *Server) handleRestart(w http.ResponseWriter, r *http.Request) {
	a, code, err := s.restart(r.PathValue("id"), "")
	if err != nil {
		writeErr(w, code, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toView(a))
}

// restart stops the agent's container and starts a new one, with an event
// saying why when reason is set.
func (s *Server) restart(id, reason string) (*model.Agent, int, error) {
	if reason != "" && s.store.GetAgent(id) != nil {
		s.event(id, "status", reason)
	}
	_ = s.rt.Stop(id)
	return s.start(id)
}

func (s *Server) startAgent(w http.ResponseWriter, id string) {
	a, code, err := s.start(id)
	if err != nil {
		writeErr(w, code, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toView(a))
}

// start starts the agent's container with its saved settings. On failure it
// returns the HTTP status to report.
func (s *Server) start(id string) (*model.Agent, int, error) {
	a := s.store.GetAgent(id)
	if a == nil {
		return nil, http.StatusNotFound, errors.New("agent not found")
	}
	if st := s.rt.Status(id); st == "running" {
		return nil, http.StatusConflict, errors.New("agent container is already running")
	}
	if err := s.rt.EnsureNetwork(); err != nil {
		return nil, http.StatusInternalServerError, err
	}
	cid, err := s.rt.Start(a)
	if err != nil {
		a.Status = "error"
		a.Error = err.Error()
		a.RestartPending = false
		_ = s.store.UpsertAgent(a)
		s.event(id, "error", "start failed: "+err.Error())
		return nil, http.StatusInternalServerError, err
	}
	a.Status = "running"
	a.ContainerID = cid
	a.Error = ""
	a.RestartPending = false
	a.UpdatedAt = time.Now().UTC()
	_ = s.store.UpsertAgent(a)
	s.store.AppendEvents(id, "", []model.Event{{Type: "status", Content: "container started: " + cid[:12]}})
	// A fresh worker means in-flight tasks lost their context: requeue them.
	for _, t := range s.store.RequeueRunningTasksFor(id) {
		evs := s.store.AppendEvents(id, t.ID, []model.Event{{Type: "status", Content: "task requeued: agent restarted, context starts fresh"}})
		for _, ev := range evs {
			s.broker.publish(id, ev)
		}
	}
	s.broker.publish(id, model.Event{AgentID: id, Type: "status", Content: "container started"})
	return a, http.StatusOK, nil
}

func (s *Server) handleStop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a := s.store.GetAgent(id)
	if a == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	if err := s.rt.Stop(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.Status = "stopped"
	a.ContainerID = ""
	a.RestartPending = false // the next start uses the saved settings
	a.UpdatedAt = time.Now().UTC()
	_ = s.store.UpsertAgent(a)
	s.store.AppendEvents(id, "", []model.Event{{Type: "status", Content: "container stopped"}})
	writeJSON(w, http.StatusOK, toView(a))
}

func (s *Server) handleListTasks(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.store.TasksFor(r.PathValue("id")))
}

func (s *Server) handleGetTask(w http.ResponseWriter, r *http.Request) {
	t := s.store.GetTask(r.PathValue("id"))
	if t == nil {
		writeErr(w, http.StatusNotFound, "task not found")
		return
	}
	writeJSON(w, http.StatusOK, t)
}

type taskInput struct {
	Title string `json:"title"`
	Input string `json:"input"`
}

func (s *Server) handleCreateTask(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if s.store.GetAgent(id) == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	var in taskInput
	if !decode(w, r, &in) {
		return
	}
	if strings.TrimSpace(in.Input) == "" {
		writeErr(w, http.StatusBadRequest, "input is required")
		return
	}
	title := strings.TrimSpace(in.Title)
	if title == "" {
		title = firstLine(in.Input, 80)
	}
	t, err := s.store.CreateTask(id, "manual", title, in.Input, "")
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.store.AppendEvents(id, t.ID, []model.Event{{Type: "status", Content: "task queued: " + title}})
	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	var since int64
	fmt.Sscanf(r.URL.Query().Get("since"), "%d", &since)
	writeJSON(w, http.StatusOK, s.store.EventsSince(r.PathValue("id"), since))
}

func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if s.store.GetAgent(id) == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	send := func(ev model.Event) {
		b, _ := json.Marshal(ev)
		fmt.Fprintf(w, "data: %s\n\n", b)
		flusher.Flush()
	}
	for _, ev := range s.store.EventsSince(id, 0) {
		send(ev)
	}
	ch := s.broker.subscribe(id)
	defer s.broker.unsubscribe(id, ch)
	for {
		select {
		case <-r.Context().Done():
			return
		case ev := <-ch:
			send(ev)
		}
	}
}

// ---------- internal handlers (worker containers) ----------

// checkInternal authenticates a worker container for agentID: the request
// must carry that agent's own derived token (see runtime.AgentToken).
func (s *Server) checkInternal(w http.ResponseWriter, r *http.Request, agentID string) bool {
	got := r.Header.Get("X-Manager-Token")
	if s.managerToken == "" || agentID == "" || got == "" ||
		!hmac.Equal([]byte(got), []byte(runtime.AgentToken(s.managerToken, agentID))) {
		writeErr(w, http.StatusUnauthorized, "invalid agent token")
		return false
	}
	return true
}

func (s *Server) handleInternalPing(w http.ResponseWriter, r *http.Request) {
	if !s.checkInternal(w, r, r.PathValue("id")) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type internalTaskInput struct {
	Kind   string `json:"kind"`
	Title  string `json:"title"`
	Input  string `json:"input"`
	Source string `json:"source"`
}

func (s *Server) handleInternalCreateTask(w http.ResponseWriter, r *http.Request) {
	if !s.checkInternal(w, r, r.PathValue("id")) {
		return
	}
	id := r.PathValue("id")
	if s.store.GetAgent(id) == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	var in internalTaskInput
	if !decode(w, r, &in) {
		return
	}
	if in.Kind == "" {
		in.Kind = "mention"
	}
	t, err := s.store.CreateTask(id, in.Kind, in.Title, in.Input, in.Source)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleInternalNextTask(w http.ResponseWriter, r *http.Request) {
	if !s.checkInternal(w, r, r.PathValue("id")) {
		return
	}
	id := r.PathValue("id")
	// The worker only asks for work when it's idle: the moment to apply
	// settings saved while it was busy. No task now; the new container
	// picks up whatever is pending.
	if a := s.store.GetAgent(id); a != nil && a.RestartPending {
		writeJSON(w, http.StatusOK, nil)
		_ = http.NewResponseController(w).Flush()
		s.applyPendingRestart(id)
		return
	}
	t := s.store.ClaimNextTask(id)
	if t == nil {
		writeJSON(w, http.StatusOK, nil)
		return
	}
	evs := s.store.AppendEvents(t.AgentID, t.ID, []model.Event{{Type: "status", Content: "task claimed: " + t.Title}})
	for _, ev := range evs {
		s.broker.publish(t.AgentID, ev)
	}
	writeJSON(w, http.StatusOK, t)
}

type eventInput struct {
	TaskID  string `json:"taskId"`
	Type    string `json:"type"`
	Content string `json:"content"`
}

func (s *Server) handleInternalEvents(w http.ResponseWriter, r *http.Request) {
	if !s.checkInternal(w, r, r.PathValue("id")) {
		return
	}
	id := r.PathValue("id")
	if s.store.GetAgent(id) == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	var in eventInput
	if !decode(w, r, &in) {
		return
	}
	if in.Type == "" {
		in.Type = "log"
	}
	evs := s.store.AppendEvents(id, in.TaskID, []model.Event{{Type: in.Type, Content: in.Content}})
	for _, ev := range evs {
		s.broker.publish(id, ev)
	}
	writeJSON(w, http.StatusCreated, evs)
}

// handleInternalPoll records the worker's git-poll schedule on the agent so
// the UI can count down to the next check.
func (s *Server) handleInternalPoll(w http.ResponseWriter, r *http.Request) {
	if !s.checkInternal(w, r, r.PathValue("id")) {
		return
	}
	id := r.PathValue("id")
	a := s.store.GetAgent(id)
	if a == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	var in struct {
		LastPollAt *time.Time `json:"lastPollAt"`
		NextPollAt *time.Time `json:"nextPollAt"`
	}
	if !decode(w, r, &in) {
		return
	}
	a.LastPollAt = in.LastPollAt
	a.NextPollAt = in.NextPollAt
	_ = s.store.UpsertAgent(a)
	writeJSON(w, http.StatusOK, toView(a))
}

type finishInput struct {
	Status string `json:"status"`
	Output string `json:"output"`
	Error  string `json:"error"`
}

func (s *Server) handleInternalFinishTask(w http.ResponseWriter, r *http.Request) {
	t := s.store.GetTask(r.PathValue("id"))
	if t == nil {
		writeErr(w, http.StatusNotFound, "task not found")
		return
	}
	if !s.checkInternal(w, r, t.AgentID) {
		return
	}
	var in finishInput
	if !decode(w, r, &in) {
		return
	}
	if in.Status == "" {
		in.Status = "done"
	}
	if err := s.store.FinishTask(r.PathValue("id"), in.Status, in.Output, in.Error); err != nil {
		writeErr(w, http.StatusNotFound, "task not found")
		return
	}
	t = s.store.GetTask(t.ID)
	evs := s.store.AppendEvents(t.AgentID, t.ID, []model.Event{{Type: "status", Content: "task " + in.Status + ": " + t.Title}})
	for _, ev := range evs {
		s.broker.publish(t.AgentID, ev)
	}
	writeJSON(w, http.StatusOK, t)

}

// ---------- static SPA ----------

func (s *Server) handleStatic(w http.ResponseWriter, r *http.Request) {
	if s.staticDir == "" {
		writeErr(w, http.StatusNotFound, "static UI not configured")
		return
	}
	rel := filepath.Clean("/" + r.URL.Path)
	p := filepath.Join(s.staticDir, rel)
	if rel != "/" {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			http.ServeFile(w, r, p)
			return
		}
	}
	http.ServeFile(w, r, filepath.Join(s.staticDir, "index.html"))
}

// ---------- misc ----------

func newAgentID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return time.Now().UTC().Format("20060102") + "-" + hex.EncodeToString(b)
}

func cleanList(in []string) []string {
	out := make([]string, 0, len(in))
	for _, s := range in {
		s = strings.TrimSpace(s)
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func firstLine(s string, max int) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > max {
		s = s[:max-1] + "…"
	}
	return s
}
