<script lang="ts">
	import { onMount } from 'svelte';
	import { api, splitList, TRIGGERS, type AgentPayload, type AgentView, type EngineInfo, type GitProviderInfo, type ProviderInfo } from './api';

	let {
		initial = null,
		onsubmit
	}: {
		initial?: AgentView | null;
		onsubmit: (payload: AgentPayload) => Promise<void>;
	} = $props();

	let providers = $state<ProviderInfo[]>([]);
	let gitProviders = $state<GitProviderInfo[]>([]);
	let engines = $state<EngineInfo[]>([{ id: 'doppio', label: 'Doppio loop', available: true }]);

	let name = $state(initial?.name ?? '');
	let provider = $state(initial?.provider ?? 'openai');
	let model = $state(initial?.model ?? '');
	let customModel = $state(false);
	let apiBase = $state(initial?.apiBase ?? '');
	let apiToken = $state('');
	let persona = $state(initial?.persona ?? '');
	let task = $state(initial?.task ?? '');
	let emails = $state((initial?.emails ?? []).join(', '));
	let gitProvider = $state(initial?.gitProvider ?? 'none');
	let gitBaseURL = $state(initial?.gitBaseURL ?? '');
	let gitToken = $state('');
	let gitLogin = $state(initial?.gitLogin ?? '');
	let gitRepos = $state((initial?.gitRepos ?? []).join(', '));
	let ownerLogin = $state(initial?.ownerLogin ?? '');
	let peerLogins = $state((initial?.peerLogins ?? []).join(', '));
	let triggers = $state<string[]>(initial?.triggers ?? TRIGGERS.map((t) => t.id));
	let pollSeconds = $state(initial?.pollSeconds ?? 1800);
	let maxTurns = $state(initial?.maxTurns ?? 25);
	let engine = $state(initial?.engine ?? 'doppio');
	let taskMinutes = $state(initial?.taskMinutes ?? 30);

	let error = $state('');
	let saving = $state(false);

	onMount(async () => {
		const p = await api.providers();
		providers = p.llm;
		gitProviders = p.git;
		if (p.engines?.length) engines = p.engines;
		if (!initial) {
			model = providers.find((x) => x.id === provider)?.models[0] ?? '';
			apiBase = providers.find((x) => x.id === provider)?.apiBase ?? '';
		} else {
			customModel = !providers.find((x) => x.id === provider)?.models.includes(model);
		}
	});

	function onProviderChange() {
		const p = providers.find((x) => x.id === provider);
		apiBase = p?.apiBase ?? '';
		if (!customModel) model = p?.models[0] ?? '';
	}

	async function submit() {
		error = '';
		if (gitProvider !== 'none' && triggers.length === 0) {
			error = 'Pick at least one trigger.';
			return;
		}
		saving = true;
		try {
			await onsubmit({
				name,
				persona,
				task,
				provider,
				model,
				apiBase,
				apiToken, // empty on edit = keep existing
				emails: splitList(emails),
				gitProvider,
				gitBaseURL,
				gitToken,
				gitLogin,
				gitRepos: splitList(gitRepos),
				ownerLogin,
				peerLogins: splitList(peerLogins),
				triggers: TRIGGERS.map((t) => t.id).filter((id) => triggers.includes(id)),
				pollSeconds: Number(pollSeconds) || 1800,
				maxTurns: Number(maxTurns) || 25,
				engine,
				taskMinutes: Number(taskMinutes) || 30
			});
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			saving = false;
		}
	}
</script>

<form onsubmit={(e) => { e.preventDefault(); submit(); }}>
	<div class="field">
		<label for="f-name">Name</label>
		<input id="f-name" type="text" bind:value={name} placeholder="agent-1" required />
	</div>

	<h2>Model</h2>
	<div class="field">
		<label for="f-engine">Engine</label>
		<select id="f-engine" bind:value={engine}>
			{#each engines as e}
				<option value={e.id} disabled={!e.available}>{e.label}{e.available ? '' : ' (coming soon)'}</option>
			{/each}
		</select>
		<p class="hint">What runs the agent's tasks: Doppio's own tool loop over the model below, or a coding CLI.</p>
	</div>
	<div class="row">
		<div class="field" style="flex:1">
			<label for="f-provider">Company</label>
			<select id="f-provider" bind:value={provider} onchange={onProviderChange}>
				{#each providers as p}
					<option value={p.id}>{p.name}</option>
				{/each}
			</select>
		</div>
		<div class="field" style="flex:1">
			<label for="f-model">Model</label>
			{#if customModel}
				<input id="f-model" type="text" bind:value={model} placeholder="custom model id" required />
			{:else}
				<select id="f-model" bind:value={model}>
					{#each providers.find((x) => x.id === provider)?.models ?? [] as m}
						<option value={m}>{m}</option>
					{/each}
					<option value="__custom__" onclick={() => (customModel = true)}>(custom…)</option>
				</select>
			{/if}
		</div>
	</div>
	<div class="field">
		<label for="f-apibase">API base URL (optional override, e.g. local OpenAI-compatible server)</label>
		<input id="f-apibase" type="text" bind:value={apiBase} placeholder="https://api.openai.com/v1" />
	</div>
	<div class="field">
		<label for="f-apitoken">API token for this agent {initial ? '(leave empty to keep the stored token)' : ''}</label>
		<input id="f-apitoken" type="password" bind:value={apiToken} placeholder="sk-…" autocomplete="off" />
	</div>

	<h2>Persona & task</h2>
	<div class="field">
		<label for="f-persona">Persona (system prompt — who the agent is, how it writes)</label>
		<textarea id="f-persona" bind:value={persona} required placeholder="You are …"></textarea>
	</div>
	<div class="field">
		<label for="f-task">Standing task (what the agent works on)</label>
		<textarea id="f-task" bind:value={task} required placeholder="Review pull requests and answer mentions on …"></textarea>
	</div>

	<h2>Contact</h2>
	<div class="field">
		<label for="f-emails">Email addresses assigned to this agent (comma-separated)</label>
		<input id="f-emails" type="text" bind:value={emails} placeholder="agent-1@example.com" />
	</div>

	<h2>Git account (the agent's own user on the git host)</h2>
	<div class="row">
		<div class="field" style="flex:1">
			<label for="f-gitprovider">Provider</label>
			<select id="f-gitprovider" bind:value={gitProvider}>
				{#each gitProviders as g}
					<option value={g.id}>{g.name}</option>
				{/each}
			</select>
		</div>
		<div class="field" style="flex:1">
			<label for="f-gitlogin">Agent's git login</label>
			<input id="f-gitlogin" type="text" bind:value={gitLogin} placeholder="my-agent-bot" />
		</div>
	</div>
	{#if gitProvider !== 'none'}
		<div class="field">
			<label for="f-gitbase">Git base URL (GitHub: leave empty; self-hosted GitLab: https://gitlab.example.com)</label>
			<input id="f-gitbase" type="text" bind:value={gitBaseURL} placeholder="https://gitlab.com" />
		</div>
		<div class="field">
			<label for="f-gittoken">Access token of the agent's git account {initial ? '(leave empty to keep the stored token)' : ''}</label>
			<input id="f-gittoken" type="password" bind:value={gitToken} autocomplete="off" />
		</div>
		<div class="field">
			<label for="f-gitrepos">Repositories to watch (owner/repo, comma-separated)</label>
			<input id="f-gitrepos" type="text" bind:value={gitRepos} placeholder="AmarettoLabs/Palazzo" />
		</div>
		<div class="row">
			<div class="field" style="flex:1">
				<label for="f-owner">Owner login (the agent only acts on the owner's requests)</label>
				<input id="f-owner" type="text" bind:value={ownerLogin} placeholder="your-username" required />
			</div>
			<div class="field" style="flex:1">
				<label for="f-peers">Peer agent logins (context only, comma-separated)</label>
				<input id="f-peers" type="text" bind:value={peerLogins} placeholder="teammate, other-agent-bot" />
			</div>
		</div>
		<fieldset class="field triggers">
			<legend>Responds to (only when the owner asks)</legend>
			{#each TRIGGERS as t}
				<label class="trigger">
					<input type="checkbox" value={t.id} bind:group={triggers} />
					<span>{t.label}</span>
					<span class="muted">— {t.hint}</span>
				</label>
			{/each}
		</fieldset>
		<div class="field">
			<label for="f-poll">Poll interval for git checks (seconds)</label>
			<input id="f-poll" type="number" min="5" bind:value={pollSeconds} />
		</div>
	{/if}
	<div class="field">
		<label for="f-turns">Max tool turns per task (-1 = unlimited, until the model's context limit)</label>
		<input id="f-turns" type="number" min="-1" bind:value={maxTurns} />
	</div>
	<div class="field">
		<label for="f-minutes">Time limit per task (minutes)</label>
		<input id="f-minutes" type="number" min="1" max="1440" bind:value={taskMinutes} />
		<p class="hint">A task still running then is stopped and retried at a later git check.</p>
	</div>

	{#if error}<p class="err">{error}</p>{/if}
	<div class="row">
		<button type="submit" disabled={saving}>{saving ? 'Saving…' : initial ? 'Save changes' : 'Create agent'}</button>
		<a class="btn ghost" href="/">Cancel</a>
	</div>
</form>

<style>
	.triggers {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.5rem 0.8rem 0.7rem;
	}
	.triggers legend {
		padding: 0 0.3rem;
		color: var(--muted);
		font-size: 0.85rem;
	}
	.trigger {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		margin: 0.3rem 0;
		font-weight: normal;
	}
	.trigger input {
		width: auto;
		margin: 0;
	}
</style>
