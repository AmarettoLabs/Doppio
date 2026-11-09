// Account storage: one directory per account under <dir>/accounts/<id>/,
// each holding the secrets in 0600 files (never in store.json, never in the
// API). Only which fields are set is kept in the store.
package store

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/AmarettoLabs/Doppio/internal/model"
)

// AccountsDir is where account credentials live, relative to the data dir.
const AccountsDir = "accounts"

// Accounts is the account part of a Store: the JSON metadata plus the secret
// files on disk. The same Store serves agents and accounts.
type Accounts struct {
	s *Store
}

// Accounts returns the account store bound to s.
func (s *Store) Accounts() *Accounts { return &Accounts{s: s} }

// accountsRoot is the base directory of all account dirs.
func (a *Accounts) accountsRoot() string {
	return filepath.Join(a.s.dir, AccountsDir)
}

// accountDir validates id and returns its directory.
func (a *Accounts) accountDir(id string) (string, error) {
	if id == "" || strings.ContainsAny(id, `/\`) || id == "." || id == ".." {
		return "", fmt.Errorf("invalid account id %q", id)
	}
	return filepath.Join(a.accountsRoot(), id), nil
}

// secretPath is the file holding one credential field of an account.
func (a *Accounts) secretPath(id, field string) (string, error) {
	dir, err := a.accountDir(id)
	if err != nil {
		return "", err
	}
	if field == "" || strings.ContainsAny(field, `/\`) || field == "." || field == ".." {
		return "", fmt.Errorf("invalid secret field %q", field)
	}
	return filepath.Join(dir, field+".secret"), nil
}

// NewAccountID returns a fresh account id.
func NewAccountID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// The store never hands out its own *model.Account: List, Get, Create and
// Update return clones, and Update works on a clone it swaps in when the
// update succeeded. Callers read accounts without the store lock, so a shared
// pointer would race with the next update (and a concurrent read/write of
// SecretSet is a fatal error, not just a stale read).

// List returns copies of all accounts sorted by creation time.
func (a *Accounts) List() []*model.Account {
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	out := make([]*model.Account, 0, len(a.s.data.Accounts))
	for _, acc := range a.s.data.Accounts {
		out = append(out, acc.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

// Get returns a copy of the account, or nil.
func (a *Accounts) Get(id string) *model.Account {
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	if acc := a.s.data.Accounts[id]; acc != nil {
		return acc.Clone()
	}
	return nil
}

// Create makes a new account with the given secrets (field -> value; empty
// values are dropped) and persists it plus its secret files.
func (a *Accounts) Create(name string, kind model.AccountKind, notes string, secrets map[string]string) (*model.Account, error) {
	acc := &model.Account{
		ID:        NewAccountID(),
		Name:      strings.TrimSpace(name),
		Kind:      kind,
		Notes:     strings.TrimSpace(notes),
		SecretSet: map[string]bool{},
		CreatedAt: time.Now().UTC(),
		UpdatedAt: time.Now().UTC(),
	}
	if msg := acc.Validate(); msg != "" {
		return nil, fmt.Errorf("%s", msg)
	}
	// Under the store lock so the secret files and the metadata change
	// together.
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	if err := a.writeSecrets(acc, secrets); err != nil {
		return nil, err
	}
	if a.s.data.Accounts == nil {
		a.s.data.Accounts = map[string]*model.Account{}
	}
	a.s.data.Accounts[acc.ID] = acc
	if err := a.s.saveLocked(); err != nil {
		// Roll the secret files back so nothing is left behind.
		delete(a.s.data.Accounts, acc.ID)
		_ = a.removeSecretsLocked(acc)
		return nil, err
	}
	return acc.Clone(), nil
}

// Update changes an account's name, notes and/or secrets, and returns a copy
// of the result.
//
// A nil secrets map keeps every stored field; a non-nil map touches only the
// fields it contains (an empty value clears that field, a missing field is
// left alone). An empty name keeps the stored name; notes is a pointer:
// nil keeps the stored notes, a non-nil value (possibly "") replaces them.
//
// It runs under the store lock and changes a clone, which replaces the stored
// account only once the secrets and store.json are written: a failed update
// leaves the stored account as it was.
func (a *Accounts) Update(id, name string, notes *string, secrets map[string]string) (*model.Account, error) {
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	old := a.s.data.Accounts[id]
	if old == nil {
		return nil, os.ErrNotExist
	}
	acc := old.Clone()
	if strings.TrimSpace(name) != "" {
		acc.Name = strings.TrimSpace(name)
	}
	if notes != nil {
		acc.Notes = strings.TrimSpace(*notes)
	}
	if secrets != nil {
		if err := a.writeSecrets(acc, secrets); err != nil {
			return nil, err
		}
	}
	acc.UpdatedAt = time.Now().UTC()
	a.s.data.Accounts[id] = acc
	if err := a.s.saveLocked(); err != nil {
		a.s.data.Accounts[id] = old
		return nil, err
	}
	return acc.Clone(), nil
}

// Delete removes the account, its metadata and its secret files.
func (a *Accounts) Delete(id string) error {
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	if a.s.data.Accounts[id] == nil {
		return os.ErrNotExist
	}
	delete(a.s.data.Accounts, id)
	if err := a.s.saveLocked(); err != nil {
		return err
	}
	dir, err := a.accountDir(id)
	if err != nil {
		return err
	}
	return os.RemoveAll(dir)
}

// AgentsUsing returns the names of the agents referencing account id.
func (a *Accounts) AgentsUsing(id string) []string {
	a.s.mu.Lock()
	defer a.s.mu.Unlock()
	var out []string
	for _, ag := range a.s.data.Agents {
		if ag.AccountID == id {
			out = append(out, ag.Name)
		}
	}
	sort.Strings(out)
	return out
}

// writeSecrets writes the given secret fields to the account's dir with mode
// 0600 and updates the account's SecretSet flags. Only the fields present in
// the map are touched: a field sent as "" is cleared, a field missing from the
// map keeps its stored value (so an update that renames the account, or fills
// only one of several fields, never wipes the others). Unknown fields for the
// account's kind are rejected. The caller holds the store lock.
func (a *Accounts) writeSecrets(acc *model.Account, secrets map[string]string) error {
	info, ok := model.LookupAccountKind(acc.Kind)
	if !ok {
		return fmt.Errorf("unknown account kind %q", acc.Kind)
	}
	allowed := map[string]bool{}
	for _, f := range info.SecretFields {
		allowed[f] = true
	}
	dir, err := a.accountDir(acc.ID)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for field, value := range secrets {
		if !allowed[field] {
			return fmt.Errorf("field %q does not belong to a %s account", field, acc.Kind)
		}
		value = strings.TrimSpace(value)
		p, err := a.secretPath(acc.ID, field)
		if err != nil {
			return err
		}
		if value == "" {
			acc.UnsetSecret(field)
			if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
				return err
			}
			continue
		}
		if err := os.WriteFile(p, []byte(value), 0o600); err != nil {
			return err
		}
		acc.SetSecret(field)
	}
	return nil
}

// removeSecretsLocked deletes every secret file of the account (caller holds
// the store lock).
func (a *Accounts) removeSecretsLocked(acc *model.Account) error {
	dir, err := a.accountDir(acc.ID)
	if err != nil {
		return err
	}
	return os.RemoveAll(dir)
}

// ReadSecret returns one credential field of an account ("" when unset).
func (a *Accounts) ReadSecret(id, field string) (string, error) {
	p, err := a.secretPath(id, field)
	if err != nil {
		return "", err
	}
	b, err := os.ReadFile(p)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimRight(string(b), "\n"), nil
}

// Secrets returns every set credential field of an account. Only the manager
// (to pass them into a container) may call this; the API never sends them.
func (a *Accounts) Secrets(id string) (map[string]string, error) {
	acc := a.Get(id)
	if acc == nil {
		return nil, os.ErrNotExist
	}
	out := map[string]string{}
	for _, f := range acc.SecretFields() {
		v, err := a.ReadSecret(id, f)
		if err != nil {
			return nil, err
		}
		out[f] = v
	}
	return out, nil
}
