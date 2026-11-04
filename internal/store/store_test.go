package store

import "testing"

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
	if w := s.WorkingAgents(); len(w) != 0 {
		t.Fatalf("pending tasks only: got %v", w)
	}
	claimed := s.ClaimNextTask("a1")
	if w := s.WorkingAgents(); !w["a1"] || w["a2"] {
		t.Fatalf("after claim: got %v", w)
	}
	if err := s.FinishTask(claimed.ID, "done", "ok", ""); err != nil {
		t.Fatal(err)
	}
	if w := s.WorkingAgents(); len(w) != 0 {
		t.Fatalf("after finish: got %v", w)
	}
}
