package api

import (
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

// ---------- accounts ----------

// AccountView is the account as exposed to the UI: secrets are never sent,
// only which fields are set.
type AccountView struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Kind         string   `json:"kind"`
	KindLabel    string   `json:"kindLabel"`
	SecretFields []string `json:"secretFields"` // set credential fields
	// HasSecrets is true when every field the kind needs is set; an account
	// missing one is "missing", not usable.
	HasSecrets bool     `json:"hasSecrets"`
	Notes      string   `json:"notes,omitempty"`
	Agents     []string `json:"agents"` // names of the agents using it
	Status     string   `json:"status"` // configured, missing, expired
	CreatedAt  string   `json:"createdAt"`
	UpdatedAt  string   `json:"updatedAt"`
}

// toAccountView builds the view; agentsUsing lists the agents referencing it.
func toAccountView(acc *model.Account, agentsUsing []string) AccountView {
	info, _ := model.LookupAccountKind(acc.Kind)
	v := AccountView{
		ID:           acc.ID,
		Name:         acc.Name,
		Kind:         string(acc.Kind),
		KindLabel:    info.Label,
		SecretFields: acc.SecretFields(),
		Notes:        acc.Notes,
		Agents:       agentsUsing,
		CreatedAt:    acc.CreatedAt.UTC().Format(time.RFC3339),
		UpdatedAt:    acc.UpdatedAt.UTC().Format(time.RFC3339),
	}
	v.HasSecrets = len(v.SecretFields) == len(info.SecretFields) && len(info.SecretFields) > 0
	v.Status = accountStatus(acc)
	return v
}

// accountStatus is "configured" when every needed credential is set,
// "missing" otherwise. "expired" is reserved for kinds where the manager can
// detect it (none can today).
func accountStatus(acc *model.Account) string {
	if len(acc.SecretFields()) == len(acc.RequiredSecretFields()) && len(acc.RequiredSecretFields()) > 0 {
		return "configured"
	}
	return "missing"
}

type accountInput struct {
	Name    string            `json:"name"`
	Kind    string            `json:"kind"`
	Notes   string            `json:"notes"`
	Secrets map[string]string `json:"secrets"` // nil on update = keep the stored ones
}

func (in *accountInput) validate(kindRequired bool) string {
	if in.Name == "" && !kindRequired {
		// keep the stored name on update
	} else if strings.TrimSpace(in.Name) == "" {
		return "name is required"
	}
	if in.Kind == "" && !kindRequired {
		return "" // keep the stored kind on update
	}
	if !model.ValidAccountKind(model.AccountKind(in.Kind)) {
		return "unknown account kind " + in.Kind
	}
	for f := range in.Secrets {
		info, _ := model.LookupAccountKind(model.AccountKind(in.Kind))
		known := false
		for _, sf := range info.SecretFields {
			if sf == f {
				known = true
			}
		}
		if !known {
			return "field " + f + " does not belong to a " + in.Kind + " account"
		}
	}
	return ""
}

func (s *Server) handleListAccounts(w http.ResponseWriter, _ *http.Request) {
	accs := s.store.Accounts().List()
	views := make([]AccountView, 0, len(accs))
	for _, acc := range accs {
		views = append(views, toAccountView(acc, s.store.Accounts().AgentsUsing(acc.ID)))
	}
	writeJSON(w, http.StatusOK, views)
}

func (s *Server) handleAccountKinds(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, model.AccountKinds())
}

func (s *Server) handleCreateAccount(w http.ResponseWriter, r *http.Request) {
	var in accountInput
	if !decode(w, r, &in) {
		return
	}
	if msg := in.validate(true); msg != "" {
		writeErr(w, http.StatusBadRequest, msg)
		return
	}
	acc, err := s.store.Accounts().Create(in.Name, model.AccountKind(in.Kind), in.Notes, in.Secrets)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toAccountView(acc, nil))
}

func (s *Server) handleGetAccount(w http.ResponseWriter, r *http.Request) {
	acc := s.store.Accounts().Get(r.PathValue("id"))
	if acc == nil {
		writeErr(w, http.StatusNotFound, "account not found")
		return
	}
	writeJSON(w, http.StatusOK, toAccountView(acc, s.store.Accounts().AgentsUsing(acc.ID)))
}

func (s *Server) handleUpdateAccount(w http.ResponseWriter, r *http.Request) {
	var in accountInput
	if !decode(w, r, &in) {
		return
	}
	id := r.PathValue("id")
	acc := s.store.Accounts().Get(id)
	if acc == nil {
		writeErr(w, http.StatusNotFound, "account not found")
		return
	}
	if msg := in.validate(false); msg != "" {
		writeErr(w, http.StatusBadRequest, msg)
		return
	}
	if in.Kind != "" && model.AccountKind(in.Kind) != acc.Kind {
		// The kind only changes together with its secrets; refuse a bare kind
		// switch so an account never ends up with another kind's fields.
		if in.Secrets == nil {
			writeErr(w, http.StatusBadRequest, "changing the kind needs its secrets too")
			return
		}
	}
	acc, err := s.store.Accounts().Update(id, in.Name, in.Notes, in.Secrets)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeErr(w, http.StatusNotFound, "account not found")
			return
		}
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	// Agents running with this account keep the old credentials until they are
	// restarted; note that in their stream.
	for _, name := range s.store.Accounts().AgentsUsing(id) {
		if a := agentByName(s, name); a != nil {
			s.event(a.ID, "status", "account "+acc.Name+" was changed; restart the agent to use it")
		}
	}
	writeJSON(w, http.StatusOK, toAccountView(acc, s.store.Accounts().AgentsUsing(acc.ID)))
}

func (s *Server) handleDeleteAccount(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	acc := s.store.Accounts().Get(id)
	if acc == nil {
		writeErr(w, http.StatusNotFound, "account not found")
		return
	}
	using := s.store.Accounts().AgentsUsing(id)
	if len(using) > 0 {
		writeJSON(w, http.StatusConflict, map[string]any{
			"error":  "account is used by " + strings.Join(using, ", "),
			"agents": using,
		})
		return
	}
	if err := s.store.Accounts().Delete(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
}

// agentByName returns the agent with this name (names are unique in the
// portal), or nil.
func agentByName(s *Server, name string) *model.Agent {
	for _, a := range s.store.Agents() {
		if a.Name == name {
			return a
		}
	}
	return nil
}
