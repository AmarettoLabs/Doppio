package api

import (
	"context"
	"errors"
	"log"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/codexauth"
	"github.com/AmarettoLabs/Doppio/internal/model"
	"github.com/AmarettoLabs/Doppio/internal/runtime"
)

// ---------- Codex logins ----------
//
// A Codex account holds the auth.json codex writes after a ChatGPT login.
// ChatGPT refresh tokens are single-use, so the manager is the only one that
// refreshes it (RefreshCodexLogins, on a timer) and writes the current copy
// into every deployed agent using the account (UpdateAccount); each task
// copies it into its codex home. If an agent's codex refreshed it anyway (an
// expired token, say), the worker reports the new document back
// (handleInternalCodexAuth) and the manager adopts it, since the old refresh
// token no longer works.

// AccountUpdater rewrites a deployed agent's account credentials in place
// (*runtime.Runtime does).
type AccountUpdater interface {
	UpdateAccount(agentID string, secrets map[string]string) error
}

// CodexLoginer runs a device-code login and returns the auth.json it wrote
// (*runtime.Runtime does, in a container from the agent image).
type CodexLoginer interface {
	CodexLogin(ctx context.Context, onLine func(string)) (string, error)
}

// codexLogin is a device-code login in progress (or just ended) for one
// account, as the accounts page shows it.
type codexLogin struct {
	State     string `json:"state"` // waiting, done, failed, cancelled
	URL       string `json:"url,omitempty"`
	Code      string `json:"code,omitempty"`
	Error     string `json:"error,omitempty"`
	StartedAt string `json:"startedAt"`
	cancel    context.CancelFunc
	lastLine  string // codex's last output line: why a failed login failed
}

// codexState is the Server's Codex bookkeeping.
type codexState struct {
	// mu serializes every change to a Codex account's auth.json: a refresh,
	// a login and an agent's report must not interleave (two refreshes with
	// one single-use token lock the account out).
	mu     sync.Mutex
	logins map[string]*codexLogin // account id -> its login
	client *http.Client
}

func (s *Server) codex() *codexState {
	s.codexOnce.Do(func() {
		s.codexSt = &codexState{logins: map[string]*codexLogin{}, client: &http.Client{Timeout: 30 * time.Second}}
	})
	return s.codexSt
}

var (
	codexURLRE  = regexp.MustCompile(`https://\S+`)
	codexCodeRE = regexp.MustCompile(`\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b`)
)

// codexAccount returns the account if it is a Codex one, writing the error
// otherwise.
func (s *Server) codexAccount(w http.ResponseWriter, id string) *model.Account {
	acc := s.store.Accounts().Get(id)
	if acc == nil {
		writeErr(w, http.StatusNotFound, "account not found")
		return nil
	}
	if acc.Kind != model.AcctCodex {
		writeErr(w, http.StatusBadRequest, "only a Codex account logs in with ChatGPT")
		return nil
	}
	return acc
}

func (s *Server) handleCodexLoginStart(w http.ResponseWriter, r *http.Request) {
	acc := s.codexAccount(w, r.PathValue("id"))
	if acc == nil {
		return
	}
	loginer, ok := s.rt.(CodexLoginer)
	if !ok {
		writeErr(w, http.StatusNotImplemented, "this manager can't run codex logins")
		return
	}
	cs := s.codex()
	cs.mu.Lock()
	if l := cs.logins[acc.ID]; l != nil && l.State == "waiting" {
		view := *l
		cs.mu.Unlock()
		writeJSON(w, http.StatusOK, view)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), runtime.CodexLoginTimeout)
	l := &codexLogin{State: "waiting", StartedAt: time.Now().UTC().Format(time.RFC3339), cancel: cancel}
	cs.logins[acc.ID] = l
	cs.mu.Unlock()

	// The link and code arrive on codex's first lines; answer once both
	// are there (or the login already ended), so the page can show them.
	shown := make(chan struct{})
	var once sync.Once
	show := func() { once.Do(func() { close(shown) }) }
	go func() {
		defer cancel()
		defer show()
		authJSON, err := loginer.CodexLogin(ctx, func(line string) {
			cs.mu.Lock()
			if t := strings.TrimSpace(line); t != "" && !strings.HasPrefix(t, "WARNING") {
				l.lastLine = t
			}
			if l.URL == "" {
				l.URL = codexURLRE.FindString(line)
			}
			if l.Code == "" && l.URL != "" {
				l.Code = codexCodeRE.FindString(line)
			}
			done := l.URL != "" && l.Code != ""
			cs.mu.Unlock()
			if done {
				show()
			}
		})
		s.finishCodexLogin(acc.ID, l, authJSON, err, ctx.Err())
	}()
	select {
	case <-shown:
	case <-time.After(30 * time.Second):
	}
	cs.mu.Lock()
	view := *l
	cs.mu.Unlock()
	writeJSON(w, http.StatusOK, view)
}

// finishCodexLogin stores the login's auth.json on the account and hands it
// to the agents using it, or records why the login failed.
func (s *Server) finishCodexLogin(accountID string, l *codexLogin, authJSON string, err, ctxErr error) {
	cs := s.codex()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if l.State == "cancelled" {
		return
	}
	if err == nil {
		_, err = codexauth.Parse(authJSON)
	}
	if err == nil {
		err = s.storeCodexAuthLocked(accountID, authJSON, "logged in with ChatGPT")
	}
	switch {
	case err == nil:
		l.State = "done"
	case errors.Is(ctxErr, context.DeadlineExceeded):
		l.State, l.Error = "failed", "the login timed out: the code was not entered within 15 minutes"
	default:
		l.State, l.Error = "failed", err.Error()
		if l.lastLine != "" && (l.Code == "" || !strings.Contains(l.lastLine, l.Code)) {
			l.Error += ": " + l.lastLine
		}
	}
	l.URL, l.Code = "", ""
}

func (s *Server) handleCodexLoginStatus(w http.ResponseWriter, r *http.Request) {
	acc := s.codexAccount(w, r.PathValue("id"))
	if acc == nil {
		return
	}
	cs := s.codex()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if l := cs.logins[acc.ID]; l != nil {
		writeJSON(w, http.StatusOK, *l)
		return
	}
	writeJSON(w, http.StatusOK, codexLogin{State: "none"})
}

func (s *Server) handleCodexLoginCancel(w http.ResponseWriter, r *http.Request) {
	acc := s.codexAccount(w, r.PathValue("id"))
	if acc == nil {
		return
	}
	cs := s.codex()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if l := cs.logins[acc.ID]; l != nil && l.State == "waiting" {
		l.State, l.URL, l.Code = "cancelled", "", ""
		l.cancel()
	}
	writeJSON(w, http.StatusOK, map[string]string{"state": "cancelled"})
}

// storeCodexAuthLocked saves a new auth.json on the account and writes it
// into every deployed agent using it (their next task picks it up). The
// caller holds codex().mu.
func (s *Server) storeCodexAuthLocked(accountID, authJSON, why string) error {
	acc, err := s.store.Accounts().Update(accountID, "", nil, map[string]string{"authJson": authJSON})
	if err != nil {
		return err
	}
	secrets, err := s.store.Accounts().Secrets(accountID)
	if err != nil {
		return err
	}
	up, _ := s.rt.(AccountUpdater)
	for _, name := range s.store.Accounts().AgentsUsing(accountID) {
		a := agentByName(s, name)
		if a == nil {
			continue
		}
		if up != nil {
			if err := up.UpdateAccount(a.ID, secrets); err != nil {
				log.Printf("codex account %s: could not update agent %s: %v", acc.Name, a.Name, err)
				s.event(a.ID, "error", "account "+acc.Name+" was "+why+" but this agent's copy could not be updated: restart the agent")
				continue
			}
		}
		s.event(a.ID, "status", "account "+acc.Name+" was "+why+"; the next task uses it")
	}
	return nil
}

// RefreshCodexLogins refreshes every Codex account whose access token
// expires soon (codexauth.NeedsRefresh) and hands the new tokens to its
// agents. A failure is logged and reported to the agents using the account
// (the owner may have to log in again).
func (s *Server) RefreshCodexLogins(ctx context.Context) {
	cs := s.codex()
	for _, acc := range s.store.Accounts().List() {
		if acc.Kind != model.AcctCodex || !acc.HasSecret("authJson") {
			continue
		}
		cs.mu.Lock()
		s.refreshCodexLocked(ctx, acc)
		cs.mu.Unlock()
	}
}

func (s *Server) refreshCodexLocked(ctx context.Context, acc *model.Account) {
	raw, err := s.store.Accounts().ReadSecret(acc.ID, "authJson")
	if err != nil {
		return
	}
	auth, err := codexauth.Parse(raw)
	if err != nil || !auth.NeedsRefresh(time.Now()) {
		return
	}
	fresh, err := auth.Refresh(ctx, s.codex().client, codexauth.TokenURL(), time.Now())
	if err != nil {
		log.Printf("codex account %s: %v", acc.Name, err)
		for _, name := range s.store.Accounts().AgentsUsing(acc.ID) {
			if a := agentByName(s, name); a != nil {
				s.event(a.ID, "error", "account "+acc.Name+": "+err.Error()+" (log in again on the Accounts page if this persists)")
			}
		}
		return
	}
	if err := s.storeCodexAuthLocked(acc.ID, fresh.String(), "refreshed"); err != nil {
		log.Printf("codex account %s: refreshed, but storing it failed: %v", acc.Name, err)
	}
}

// StartCodexRefresher runs RefreshCodexLogins now and then every interval
// until ctx ends.
func (s *Server) StartCodexRefresher(ctx context.Context, every time.Duration) {
	go func() {
		t := time.NewTicker(every)
		defer t.Stop()
		for {
			s.RefreshCodexLogins(ctx)
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
		}
	}()
}

type codexAuthReport struct {
	AuthJSON string `json:"authJson"`
}

// handleInternalCodexAuth verifies a reported refresh with the auth server
// before adopting its returned tokens for the same ChatGPT account.
func (s *Server) handleInternalCodexAuth(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.checkInternal(w, r, id) {
		return
	}
	var in codexAuthReport
	if !decode(w, r, &in) {
		return
	}
	a := s.store.GetAgent(id)
	if a == nil {
		writeErr(w, http.StatusNotFound, "agent not found")
		return
	}
	acc := s.store.Accounts().Get(a.AccountID)
	if acc == nil || acc.Kind != model.AcctCodex {
		writeErr(w, http.StatusBadRequest, "the agent has no Codex account")
		return
	}
	got, err := codexauth.Parse(in.AuthJSON)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	cs := s.codex()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	raw, err := s.store.Accounts().ReadSecret(acc.ID, "authJson")
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	stored, err := codexauth.Parse(raw)
	if err != nil || !got.NewerThan(stored) {
		writeJSON(w, http.StatusOK, map[string]bool{"adopted": false})
		return
	}
	// Finish the single-use exchange even if the reporting worker disconnects.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	verified, err := got.VerifyRefresh(ctx, cs.client, codexauth.TokenURL(), time.Now(), stored)
	if err != nil {
		log.Printf("codex account %s: reported refresh verification failed: %v", acc.Name, err)
		writeJSON(w, http.StatusOK, map[string]bool{"adopted": false})
		return
	}
	if err := s.storeCodexAuthLocked(acc.ID, verified.String(), "refreshed by agent "+a.Name); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"adopted": true})
}
