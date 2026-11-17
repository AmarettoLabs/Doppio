package api

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/runtime"
	"github.com/AmarettoLabs/Doppio/internal/store"
)

// fakeContainers records starts and stops, keeping a copy of the settings
// each container was started with.
type fakeContainers struct {
	mu      sync.Mutex
	running map[string]bool
	started []model.Agent
	stops   int

	// slow makes each Start and Stop take that long; overlaps counts the
	// calls that began while another was still in flight.
	slow     time.Duration
	inFlight int
	overlaps int
}

// enter marks a Start/Stop as in flight for f.slow; the returned func ends it.
func (f *fakeContainers) enter() func() {
	f.mu.Lock()
	if f.inFlight > 0 {
		f.overlaps++
	}
	f.inFlight++
	slow := f.slow
	f.mu.Unlock()
	time.Sleep(slow)
	return func() {
		f.mu.Lock()
		f.inFlight--
		f.mu.Unlock()
	}
}

func (f *fakeContainers) EnsureNetwork() error { return nil }
func (f *fakeContainers) Start(a *model.Agent) (string, error) {
	return f.StartWithAccounts(a, nil)
}

func (f *fakeContainers) StartWithAccounts(a *model.Agent, _ map[string]string) (string, error) {
	defer f.enter()()
	f.mu.Lock()
	defer f.mu.Unlock()
	f.running[a.ID] = true
	f.started = append(f.started, *a)
	return "c0ffee00c0ffee00", nil
}
func (f *fakeContainers) Stop(id string) error {
	defer f.enter()()
	f.mu.Lock()
	defer f.mu.Unlock()
	f.running[id] = false
	f.stops++
	return nil
}
func (f *fakeContainers) Status(id string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.running[id] {
		return "running"
	}
	return "not-found"
}
func (f *fakeContainers) RemoveState(string) error { return nil }

func (f *fakeContainers) starts() []model.Agent {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]model.Agent(nil), f.started...)
}

type applyFixture struct {
	t   *testing.T
	srv *Server
	st  *store.Store
	rt  *fakeContainers
	h   http.Handler
	id  string
}

// newApplyFixture creates an agent, starting it unless stopped is set.
func newApplyFixture(t *testing.T, stopped bool) *applyFixture {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	rt := &fakeContainers{running: map[string]bool{}}
	srv := NewServer(st, rt, "", "", testMaster)
	f := &applyFixture{t: t, srv: srv, st: st, rt: rt, h: srv.Handler()}
	rec := do(f.h, "POST", "/api/agents", "", agentJSON(`"ownerLogin":"me"`))
	var v AgentView
	if rec.Code != http.StatusCreated || json.Unmarshal(rec.Body.Bytes(), &v) != nil {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	f.id = v.ID
	if !stopped {
		if rec := do(f.h, "POST", "/api/agents/"+f.id+"/start", "", ""); rec.Code != http.StatusOK {
			t.Fatalf("start: %d %s", rec.Code, rec.Body)
		}
	}
	return f
}

func (f *applyFixture) save(repos string) AgentView {
	f.t.Helper()
	rec := do(f.h, "PATCH", "/api/agents/"+f.id, "", agentJSON(`"ownerLogin":"me","gitRepos":[`+repos+`]`))
	var v AgentView
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &v) != nil {
		f.t.Fatalf("save: %d %s", rec.Code, rec.Body)
	}
	return v
}

func (f *applyFixture) internal(method, path, body string) *http.Response {
	f.t.Helper()
	rec := do(f.h, method, path, runtime.AgentToken(testMaster, f.id), body)
	f.srv.restarts.Wait()
	return rec.Result()
}

func (f *applyFixture) events() string {
	var b strings.Builder
	for _, e := range f.st.EventsSince(f.id, 0) {
		b.WriteString(e.Content + "\n")
	}
	return b.String()
}

func TestSaveIdleAgentRestartsIt(t *testing.T) {
	f := newApplyFixture(t, false)
	v := f.save(`"o/r","o/second"`)
	starts := f.rt.starts()
	if len(starts) != 2 || len(starts[1].GitRepos) != 2 {
		t.Fatalf("want a restart with both repos, got %d starts: %+v", len(starts), starts)
	}
	if v.RestartPending || v.Status != "running" {
		t.Fatalf("view after save: pending=%v status=%s", v.RestartPending, v.Status)
	}
	if !strings.Contains(f.events(), "restarting to apply the new settings") {
		t.Fatalf("no restart event in:\n%s", f.events())
	}
}

func TestSaveWorkingAgentRestartsOnceWhenIdle(t *testing.T) {
	f := newApplyFixture(t, false)
	task, err := f.st.CreateTask(f.id, "review", "a review", "", "")
	if err != nil {
		t.Fatal(err)
	}
	f.st.ClaimNextTask(f.id)

	f.save(`"o/r","o/second"`)
	v := f.save(`"o/r","o/second","o/third"`)
	if !v.RestartPending || !v.Working {
		t.Fatalf("want pending restart while working: %+v", v)
	}
	if n := len(f.rt.starts()); n != 1 {
		t.Fatalf("container touched while working: %d starts", n)
	}

	// Finishing the task alone doesn't restart: the worker may still be
	// saving its own state.
	f.internal("POST", "/api/internal/tasks/"+task.ID+"/finish", `{"status":"done"}`)
	if n := len(f.rt.starts()); n != 1 {
		t.Fatalf("restarted before the worker was idle: %d starts", n)
	}
	// A task queued meanwhile waits for the new container.
	if _, err := f.st.CreateTask(f.id, "mention", "next", "", ""); err != nil {
		t.Fatal(err)
	}
	res := f.internal("GET", "/api/internal/agents/"+f.id+"/next-task", "")
	var got any
	_ = json.NewDecoder(res.Body).Decode(&got)
	if got != nil {
		t.Fatalf("a task was handed to the old worker: %v", got)
	}
	starts := f.rt.starts()
	if len(starts) != 2 || len(starts[1].GitRepos) != 3 {
		t.Fatalf("want one restart with the latest settings, got %+v", starts)
	}
	if a := f.st.GetAgent(f.id); a.RestartPending {
		t.Fatal("restart still pending after the restart")
	}
	// The new worker gets the queued task, and nothing restarts again.
	f.internal("GET", "/api/internal/agents/"+f.id+"/next-task", "")
	if n := len(f.rt.starts()); n != 2 {
		t.Fatalf("restarted again: %d starts", n)
	}
	if !strings.Contains(f.events(), "restart pending") {
		t.Fatalf("no pending event in:\n%s", f.events())
	}
}

func TestSaveDuringGitCheckWaits(t *testing.T) {
	f := newApplyFixture(t, false)
	now := time.Now().UTC()
	f.internal("POST", "/api/internal/agents/"+f.id+"/poll", `{"lastPollAt":"`+now.Format(time.RFC3339)+`","nextPollAt":null}`)
	if v := f.save(`"o/r","o/second"`); !v.RestartPending {
		t.Fatal("restart not deferred during a git check")
	}
	if n := len(f.rt.starts()); n != 1 {
		t.Fatalf("restarted during a git check: %d starts", n)
	}
}

func TestSaveStoppedAgentDoesNotStartIt(t *testing.T) {
	f := newApplyFixture(t, true)
	v := f.save(`"o/r","o/second"`)
	if n := len(f.rt.starts()); n != 0 || v.RestartPending {
		t.Fatalf("stopped agent: %d starts, pending=%v", n, v.RestartPending)
	}
}

func TestStopClearsPendingRestart(t *testing.T) {
	f := newApplyFixture(t, false)
	if _, err := f.st.CreateTask(f.id, "review", "a review", "", ""); err != nil {
		t.Fatal(err)
	}
	f.save(`"o/r","o/second"`)
	do(f.h, "POST", "/api/agents/"+f.id+"/stop", "", "")
	if f.st.GetAgent(f.id).RestartPending {
		t.Fatal("stop left the restart pending")
	}
}

// Restarts, starts and stops from the portal and from applying settings run
// one at a time: two at once raced on the container name in production
// (docker: "the container name ... is already in use").
func TestContainerOperationsDoNotOverlap(t *testing.T) {
	f := newApplyFixture(t, false)
	f.rt.mu.Lock()
	f.rt.slow = 20 * time.Millisecond
	f.rt.mu.Unlock()
	var wg sync.WaitGroup
	codes := make(chan int, 4)
	for _, path := range []string{"/restart", "/restart", "/restart", "/stop"} {
		wg.Add(1)
		go func(path string) {
			defer wg.Done()
			codes <- do(f.h, "POST", "/api/agents/"+f.id+path, "", "").Code
		}(path)
	}
	wg.Add(1)
	go func() { defer wg.Done(); f.save(`"o/other"`) }()
	wg.Wait()
	f.srv.restarts.Wait()
	close(codes)
	for c := range codes {
		if c != http.StatusOK {
			t.Errorf("got HTTP %d", c)
		}
	}
	f.rt.mu.Lock()
	defer f.rt.mu.Unlock()
	if f.rt.overlaps != 0 {
		t.Fatalf("%d container operations overlapped", f.rt.overlaps)
	}
}
