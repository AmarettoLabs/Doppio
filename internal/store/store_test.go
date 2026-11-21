package store

import (
	"strings"
	"testing"
	"time"
)

func TestWorkingAgents(t *testing.T) {
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.CreateTask("a1", "manual", "t", "in", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CreateTask("a2", "manual", "t", "in", ""); err != nil {
		t.Fatal(err)
	}
	if w := s.WorkingAgents(); !w["a1"] || !w["a2"] {
		t.Fatalf("pending tasks: got %v", w)
	}
	claimed := s.ClaimNextTask("a1")
	if w := s.WorkingAgents(); !w["a1"] {
		t.Fatalf("after claim: got %v", w)
	}
	if err := s.FinishTask(claimed.ID, "done", "ok", ""); err != nil {
		t.Fatal(err)
	}
	if w := s.WorkingAgents(); w["a1"] || !w["a2"] {
		t.Fatalf("after finish: got %v", w)
	}
}

func TestTaskRetention(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	finish := func(agent, title string) {
		t.Helper()
		if _, err := s.CreateTask(agent, "manual", title, "in", ""); err != nil {
			t.Fatal(err)
		}
		c := s.ClaimNextTask(agent)
		if err := s.FinishTask(c.ID, "done", "", ""); err != nil {
			t.Fatal(err)
		}
	}
	titles := func(agent string) []string {
		var out []string
		for _, t := range s.TasksFor(agent) {
			out = append(out, t.Title+":"+t.Status)
		}
		return out
	}
	// No cap yet: everything is kept.
	for _, n := range []string{"f1", "f2", "f3", "f4"} {
		finish("a1", n)
		time.Sleep(time.Millisecond)
	}
	finish("a2", "other")
	if _, err := s.CreateTask("a1", "manual", "queued", "in", ""); err != nil {
		t.Fatal(err)
	}

	// Setting the cap prunes the oldest finished tasks; queued ones stay.
	if err := s.SetTaskRetention(2); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(titles("a1"), " "); got != "queued:pending f4:done f3:done" {
		t.Fatalf("after cap: %s", got)
	}
	if got := strings.Join(titles("a2"), " "); got != "other:done" {
		t.Fatalf("other agent: %s", got)
	}

	// A new finish prunes as it happens, and the pruning is saved.
	time.Sleep(time.Millisecond)
	c := s.ClaimNextTask("a1")
	if err := s.FinishTask(c.ID, "failed", "", "boom"); err != nil {
		t.Fatal(err)
	}
	s2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	s = s2
	if got := strings.Join(titles("a1"), " "); got != "queued:failed f4:done" {
		t.Fatalf("after finish: %s", got)
	}
}
