// Package store persists agents, tasks and events to a JSON file in the data
// directory. The file holds secrets, so it is written with mode 0600.
package store

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

// Data is the persisted state.
type Data struct {
	Agents      map[string]*model.Agent `json:"agents"`
	Tasks       map[string]*model.Task  `json:"tasks"`
	Events      []model.Event           `json:"events"`
	NextEventID int64                   `json:"nextEventID"`
	// Accounts are the shared logins; their secrets live in files under
	// <dir>/accounts/<id>/, only the SecretSet flags are persisted here.
	Accounts map[string]*model.Account `json:"accounts,omitempty"`
}

// Store is a goroutine-safe JSON-file store.
type Store struct {
	mu   sync.Mutex
	dir  string
	path string
	data Data
	keep int // max events kept per agent; 0 = unlimited
}

// Dir is the directory the store was opened in (secrets live under it).
func (s *Store) Dir() string { return s.dir }

// SetEventRetention caps how many events are kept per agent (0 = unlimited).
// The oldest events are pruned on append.
func (s *Store) SetEventRetention(keep int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.keep = keep
}

// Open loads (or creates) the store in dir.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	s := &Store{
		dir:  dir,
		path: filepath.Join(dir, "store.json"),
		data: Data{Agents: map[string]*model.Agent{}, Tasks: map[string]*model.Task{}, NextEventID: 1},
	}
	b, err := os.ReadFile(s.path)
	switch {
	case err == nil && len(b) > 0:
		if err := json.Unmarshal(b, &s.data); err != nil {
			return nil, err
		}
	case err != nil && !os.IsNotExist(err):
		return nil, err
	}
	if s.data.Agents == nil {
		s.data.Agents = map[string]*model.Agent{}
	}
	if s.data.Tasks == nil {
		s.data.Tasks = map[string]*model.Task{}
	}
	if s.data.NextEventID == 0 {
		s.data.NextEventID = 1
	}
	if s.data.Accounts == nil {
		s.data.Accounts = map[string]*model.Account{}
	}
	return s, nil
}

func newID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (s *Store) saveLocked() error {
	b, err := json.MarshalIndent(s.data, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// Agents returns all agents sorted by creation time.
func (s *Store) Agents() []*model.Agent {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*model.Agent, 0, len(s.data.Agents))
	for _, a := range s.data.Agents {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

// GetAgent returns the agent or nil.
func (s *Store) GetAgent(id string) *model.Agent {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.data.Agents[id]
}

// UpsertAgent inserts or replaces an agent.
func (s *Store) UpsertAgent(a *model.Agent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data.Agents[a.ID] = a
	return s.saveLocked()
}

// DeleteAgent removes the agent, its tasks and its events.
func (s *Store) DeleteAgent(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.data.Agents, id)
	for tid, t := range s.data.Tasks {
		if t.AgentID == id {
			delete(s.data.Tasks, tid)
		}
	}
	evs := s.data.Events[:0]
	for _, e := range s.data.Events {
		if e.AgentID != id {
			evs = append(evs, e)
		}
	}
	s.data.Events = evs
	return s.saveLocked()
}

// TasksFor returns an agent's tasks, newest first.
func (s *Store) TasksFor(agentID string) []*model.Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*model.Task, 0)
	for _, t := range s.data.Tasks {
		if t.AgentID == agentID {
			out = append(out, t)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	return out
}

// WorkingAgents returns the IDs of agents with a task running or waiting to
// be picked up (their worker claims pending tasks within seconds).
func (s *Store) WorkingAgents() map[string]bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]bool{}
	for _, t := range s.data.Tasks {
		if t.Status == "running" || t.Status == "pending" {
			out[t.AgentID] = true
		}
	}
	return out
}

// GetTask returns a task or nil.
func (s *Store) GetTask(id string) *model.Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.data.Tasks[id]
}

// CreateTask appends a pending task.
func (s *Store) CreateTask(agentID, kind, title, input, source string) (*model.Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := &model.Task{
		ID:        newID(),
		AgentID:   agentID,
		Kind:      kind,
		Title:     title,
		Input:     input,
		Source:    source,
		Status:    "pending",
		CreatedAt: time.Now().UTC(),
	}
	s.data.Tasks[t.ID] = t
	return t, s.saveLocked()
}

// ClaimNextTask atomically marks the oldest pending task of the agent as
// running and returns it, or nil if there is none.
func (s *Store) ClaimNextTask(agentID string) *model.Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	var oldest *model.Task
	for _, t := range s.data.Tasks {
		if t.AgentID == agentID && t.Status == "pending" {
			if oldest == nil || t.CreatedAt.Before(oldest.CreatedAt) {
				oldest = t
			}
		}
	}
	if oldest == nil {
		return nil
	}
	now := time.Now().UTC()
	oldest.Status = "running"
	oldest.StartedAt = &now
	_ = s.saveLocked()
	return oldest
}

// FinishTask sets the final state of a task.
func (s *Store) FinishTask(id, status, output, errMsg string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.data.Tasks[id]
	if t == nil {
		return os.ErrNotExist
	}
	now := time.Now().UTC()
	t.Status = status
	t.Output = output
	t.Error = errMsg
	t.FinishedAt = &now
	return s.saveLocked()
}

// AppendEvents appends events, assigning IDs and timestamps.
func (s *Store) AppendEvents(agentID, taskID string, evs []model.Event) []model.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]model.Event, 0, len(evs))
	for i := range evs {
		ev := evs[i]
		ev.ID = s.data.NextEventID
		s.data.NextEventID++
		ev.AgentID = agentID
		if taskID != "" {
			ev.TaskID = taskID
		}
		if ev.TS.IsZero() {
			ev.TS = time.Now().UTC()
		}
		s.data.Events = append(s.data.Events, ev)
		out = append(out, ev)
	}
	if s.keep > 0 {
		count := 0
		for _, e := range s.data.Events {
			if e.AgentID == agentID {
				count++
			}
		}
		if count > s.keep {
			drop := count - s.keep
			trimmed := make([]model.Event, 0, len(s.data.Events))
			for _, e := range s.data.Events {
				if e.AgentID == agentID && drop > 0 {
					drop--
					continue
				}
				trimmed = append(trimmed, e)
			}
			s.data.Events = trimmed
		}
	}
	_ = s.saveLocked()
	return out
}

// RequeueRunningTasks marks tasks stuck in "running" as pending again (their
// worker is gone after a manager/container restart). The LLM context of a
// task is not persisted: a requeued task starts fresh.
func (s *Store) RequeueRunningTasks() []*model.Task {
	return s.requeueRunning("")
}

// RequeueRunningTasksFor requeues only one agent's running tasks (used when
// that agent's container is (re)started).
func (s *Store) RequeueRunningTasksFor(agentID string) []*model.Task {
	return s.requeueRunning(agentID)
}

func (s *Store) requeueRunning(agentID string) []*model.Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*model.Task
	for _, t := range s.data.Tasks {
		if t.Status == "running" && (agentID == "" || t.AgentID == agentID) {
			t.Status = "pending"
			t.StartedAt = nil
			out = append(out, t)
		}
	}
	if len(out) > 0 {
		_ = s.saveLocked()
	}
	return out
}

// EventsSince returns the agent's events with ID > since, in order.
func (s *Store) EventsSince(agentID string, since int64) []model.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]model.Event, 0)
	for _, e := range s.data.Events {
		if e.AgentID == agentID && e.ID > since {
			out = append(out, e)
		}
	}
	return out
}
