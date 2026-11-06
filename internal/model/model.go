// Package model defines the domain types of Doppio.
package model

import "time"

// LLMProvider is a supported model company.
type LLMProvider string

const (
	ProvOpenAI    LLMProvider = "openai"
	ProvAnthropic LLMProvider = "anthropic"
	ProvMistral   LLMProvider = "mistral"
)

// GitProvider is a supported git hosting service.
type GitProvider string

const (
	GitNone   GitProvider = "none"
	GitGitHub GitProvider = "github"
	GitGitLab GitProvider = "gitlab"
)

// Trigger is a kind of git activity an agent responds to.
type Trigger string

const (
	TrigReviewRequests Trigger = "review_requests" // review a PR/MR the owner asked it to review
	TrigMentions       Trigger = "mentions"        // answer the owner's mentions
	TrigAssignedIssues Trigger = "assigned_issues" // implement issues the owner assigned to it
	TrigAssignedPRs    Trigger = "assigned_prs"    // follow up on PRs/MRs the owner assigned to it
)

// AllTriggers lists every trigger, in display order.
func AllTriggers() []Trigger {
	return []Trigger{TrigReviewRequests, TrigMentions, TrigAssignedIssues, TrigAssignedPRs}
}

// ValidTrigger reports whether t is a known trigger.
func ValidTrigger(t Trigger) bool {
	for _, k := range AllTriggers() {
		if k == t {
			return true
		}
	}
	return false
}

// Engine runs an agent's tasks: Doppio's own tool loop over a model API, or
// a coding CLI logged in with an account.
type Engine string

const (
	EngineDoppio     Engine = "doppio"
	EngineClaudeCode Engine = "claude-code"
	EngineCodex      Engine = "codex"
	EngineVibe       Engine = "vibe"
	EngineQwenCode   Engine = "qwen-code"
)

// EngineInfo describes an engine for the UI.
type EngineInfo struct {
	ID    Engine `json:"id"`
	Label string `json:"label"`
	// Available: the worker can run it. The CLI engines are being added one
	// by one; until then they are listed but can't be picked.
	Available bool `json:"available"`
}

// Engines lists every engine, the default (Doppio loop) first.
func Engines() []EngineInfo {
	return []EngineInfo{
		{EngineDoppio, "Doppio loop", true},
		{EngineClaudeCode, "Claude Code", false},
		{EngineCodex, "Codex CLI", false},
		{EngineVibe, "Mistral Vibe", false},
		{EngineQwenCode, "Qwen Code", false},
	}
}

// LookupEngine returns the engine's info, ok=false if unknown.
func LookupEngine(e Engine) (EngineInfo, bool) {
	for _, info := range Engines() {
		if info.ID == e {
			return info, true
		}
	}
	return EngineInfo{}, false
}

// DefaultTaskMinutes is the time limit of one task when the agent has none.
const DefaultTaskMinutes = 30

// Agent is a configured coding agent. Deploying it starts one Docker
// container running the generic worker with this configuration.
type Agent struct {
	ID          string      `json:"id"`
	Name        string      `json:"name"`
	Persona     string      `json:"persona"` // system prompt defining who the agent is and how it behaves
	Task        string      `json:"task"`    // standing task / instructions
	Provider    LLMProvider `json:"provider"`
	Model       string      `json:"model"`
	APIBase     string      `json:"apiBase,omitempty"` // optional override, e.g. a local OpenAI-compatible server
	APIToken    string      `json:"apiToken,omitempty"`
	Emails      []string    `json:"emails,omitempty"`
	GitProvider GitProvider `json:"gitProvider,omitempty"`
	GitBaseURL  string      `json:"gitBaseURL,omitempty"` // e.g. self-hosted GitLab
	GitToken    string      `json:"gitToken,omitempty"`
	GitLogin    string      `json:"gitLogin,omitempty"`   // the agent's own account on the git host
	GitRepos    []string    `json:"gitRepos,omitempty"`   // e.g. "AmarettoLabs/Palazzo"
	OwnerLogin  string      `json:"ownerLogin,omitempty"` // the only account whose requests the agent acts on
	PeerLogins  []string    `json:"peerLogins,omitempty"`
	// Triggers are the kinds of git activity the agent responds to. Empty
	// (agents created before triggers existed) means all of them.
	Triggers    []Trigger `json:"triggers,omitempty"`
	PollSeconds int       `json:"pollSeconds"`
	MaxTurns    int       `json:"maxTurns"` // tool-loop turns per task; -1 = unlimited (until the model's context limit)
	// Engine runs the agent's tasks; empty (agents created before engines
	// existed) means the Doppio loop.
	Engine Engine `json:"engine,omitempty"`
	// TaskMinutes caps one task's run time; 0 means DefaultTaskMinutes.
	TaskMinutes int `json:"taskMinutes,omitempty"`

	Status string `json:"status"` // created, running, stopped, error
	// RestartPending: the agent's settings were saved while it was busy; its
	// container is restarted with them once it's idle. Cleared by any start
	// or stop.
	RestartPending bool   `json:"restartPending,omitempty"`
	ContainerID    string `json:"containerId,omitempty"`
	Error          string `json:"error,omitempty"`
	// Git poll scheduling, reported by the worker so the UI can show a
	// countdown to the next check.
	LastPollAt *time.Time `json:"lastPollAt,omitempty"`
	NextPollAt *time.Time `json:"nextPollAt,omitempty"`
	CreatedAt  time.Time  `json:"createdAt"`
	UpdatedAt  time.Time  `json:"updatedAt"`
}

// EffectiveEngine returns the agent's engine, the Doppio loop when unset.
func (a *Agent) EffectiveEngine() Engine {
	if a.Engine == "" {
		return EngineDoppio
	}
	return a.Engine
}

// EffectiveTaskMinutes returns the agent's task time limit in minutes.
func (a *Agent) EffectiveTaskMinutes() int {
	if a.TaskMinutes <= 0 {
		return DefaultTaskMinutes
	}
	return a.TaskMinutes
}

// EffectiveTriggers returns the agent's triggers, all of them when unset.
func (a *Agent) EffectiveTriggers() []Trigger {
	if len(a.Triggers) == 0 {
		return AllTriggers()
	}
	return a.Triggers
}

// Task is one unit of work for an agent: a manual task from the portal, a
// git mention, or an assigned issue/MR.
type Task struct {
	ID         string     `json:"id"`
	AgentID    string     `json:"agentId"`
	Kind       string     `json:"kind"` // manual, mention, assigned
	Title      string     `json:"title"`
	Input      string     `json:"input"`
	Source     string     `json:"source,omitempty"` // e.g. "github:AmarettoLabs/Palazzo#12"
	Status     string     `json:"status"`           // pending, running, done, failed
	Output     string     `json:"output,omitempty"`
	Error      string     `json:"error,omitempty"`
	CreatedAt  time.Time  `json:"createdAt"`
	StartedAt  *time.Time `json:"startedAt,omitempty"`
	FinishedAt *time.Time `json:"finishedAt,omitempty"`
}

// Event is one entry in an agent's live stream: status, log lines, the
// discussion with the model (user/assistant), actions, errors.
type Event struct {
	ID      int64     `json:"id"`
	AgentID string    `json:"agentId"`
	TaskID  string    `json:"taskId,omitempty"`
	TS      time.Time `json:"ts"`
	Type    string    `json:"type"` // status, log, user, assistant, action, error
	Content string    `json:"content"`
}

// ProviderInfo describes one model company in the catalog.
type ProviderInfo struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	APIBase string   `json:"apiBase"`
	Models  []string `json:"models"`
}

// Providers is the catalog of supported companies/models.
func Providers() []ProviderInfo {
	return []ProviderInfo{
		{ID: string(ProvOpenAI), Name: "OpenAI", APIBase: "https://api.openai.com/v1", Models: []string{
			"gpt-6.1-sol", "gpt-5.2", "gpt-5.2-pro", "gpt-5-mini", "gpt-4.1", "gpt-4o",
		}},
		{ID: string(ProvAnthropic), Name: "Anthropic", APIBase: "https://api.anthropic.com", Models: []string{
			"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
		}},
		{ID: string(ProvMistral), Name: "Mistral AI", APIBase: "https://api.mistral.ai/v1", Models: []string{
			"mistral-large-4", "mistral-medium-3", "codestral-2",
		}},
	}
}

// GitProviders lists the git host options for the UI.
func GitProviders() []map[string]string {
	return []map[string]string{
		{"id": string(GitNone), "name": "None"},
		{"id": string(GitGitHub), "name": "GitHub"},
		{"id": string(GitGitLab), "name": "GitLab"},
	}
}
