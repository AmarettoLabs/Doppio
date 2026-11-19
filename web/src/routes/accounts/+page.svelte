<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { api, type AccountKindInfo, type AccountPayload, type AccountView, type CodexLogin } from '#lib/api';

	let accounts = $state<AccountView[]>([]);
	let kinds = $state<AccountKindInfo[]>([]);
	let error = $state('');
	let busy = $state('');
	let editing = $state<AccountView | null>(null);

	// form state
	let name = $state('');
	let kind = $state('');
	let notes = $state('');
	let secretValues = $state<Record<string, string>>({});

	async function load() {
		try {
			[accounts, kinds] = await Promise.all([api.accounts(), api.accountKinds()]);
			if (!kind && kinds.length) kind = kinds[0].id;
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		}
	}

	onMount(load);

	// A ChatGPT login for a Codex account: the manager runs `codex login
	// --device-auth` and shows its link and one-time code here; the page polls
	// until the code was entered (or the login failed).
	let login = $state<{ account: AccountView; status: CodexLogin } | null>(null);
	let loginTimer: ReturnType<typeof setTimeout> | undefined;
	onDestroy(() => clearTimeout(loginTimer));

	async function startLogin(a: AccountView) {
		busy = a.id;
		try {
			login = { account: a, status: await api.codexLoginStart(a.id) };
			error = '';
			pollLogin();
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = '';
		}
	}

	function pollLogin() {
		clearTimeout(loginTimer);
		if (!login || login.status.state !== 'waiting') return;
		loginTimer = setTimeout(async () => {
			if (!login) return;
			try {
				login.status = await api.codexLogin(login.account.id);
				if (login.status.state === 'done') await load();
			} catch (e) {
				error = String(e instanceof Error ? e.message : e);
			}
			pollLogin();
		}, 3000);
	}

	async function cancelLogin() {
		if (!login) return;
		clearTimeout(loginTimer);
		try {
			await api.codexLoginCancel(login.account.id);
		} catch {
			// gone already: nothing to cancel
		}
		login = null;
	}

	function startCreate() {
		editing = null;
		name = '';
		notes = '';
		secretValues = {};
		if (!kind && kinds.length) kind = kinds[0].id;
	}

	function startEdit(a: AccountView) {
		editing = a;
		name = a.name;
		kind = a.kind;
		notes = a.notes ?? '';
		secretValues = {}; // empty = keep the stored secrets
	}

	function onKindChange() {
		secretValues = {};
	}

	const selectedKind = $derived(kinds.find((k) => k.id === kind));

	async function submit() {
		if (!name.trim()) {
			error = 'Name is required.';
			return;
		}
		const payload: AccountPayload = { name: name.trim(), kind };
		// Only the fields that were typed are sent: on edit an untouched field
		// (empty input = "keep the stored value") is left out, so the store
		// keeps it instead of clearing it. Notes are sent only when they differ
		// from the stored ones, so a rename keeps them.
		if (!editing || notes.trim() !== (editing.notes ?? '')) payload.notes = notes.trim();
		payload.secrets = Object.fromEntries(Object.entries(secretValues).filter(([, v]) => v.trim() !== ''));
		busy = editing ? editing.id : 'new';
		try {
			if (editing) await api.updateAccount(editing.id, payload);
			else await api.createAccount(payload);
			startCreate();
			error = '';
			await load();
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = '';
		}
	}

	async function del(a: AccountView) {
		if (busy) return;
		// The button is disabled while an agent uses the account, so only this
		// confirm can run; the API still refuses with the agent list if one
		// appeared meanwhile.
		if (!confirm(`Delete the account "${a.name}" and its stored credentials?`)) return;
		busy = a.id;
		try {
			await api.deleteAccount(a.id);
			error = '';
		} catch (e) {
			// Refused while an agent uses it: the reply lists them.
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = '';
		}
		await load();
	}
</script>

<h1>Accounts</h1>
<p class="muted">
	Logins shared between agents: one account per provider, referenced by every agent using that engine.
	Credentials are stored once, never shown again.
</p>
{#if error}<p class="err">{error}</p>{/if}

<div class="panel">
	{#if accounts.length === 0}
		<p class="muted">No accounts yet.</p>
	{:else}
		<div class="table-wrap">
		<table class="tasks">
			<thead><tr><th>Name</th><th>Kind</th><th>Credentials</th><th>Status</th><th>Used by</th><th></th></tr></thead>
			<tbody>
				{#each accounts as a (a.id)}
					<tr>
						<td>{a.name}</td>
						<td>{a.kindLabel}</td>
						<td class="muted">{a.secretFields?.length ? a.secretFields.join(', ') + ' set' : 'none set'}</td>
						<td><span class="badge {a.status === 'configured' ? 'done' : 'error'}">{a.status}</span></td>
						<td class="muted">{a.agents?.length ? a.agents.join(', ') : '—'}</td>
						<td class="nowrap">
							{#if a.kind === 'codex'}
								<button class="ghost" onclick={() => startLogin(a)} disabled={!!busy || login?.status.state === 'waiting'}>Log in with ChatGPT</button>
							{/if}
							<button class="ghost" onclick={() => startEdit(a)} disabled={!!busy}>Edit</button>
							<button class="danger" onclick={() => del(a)} disabled={!!busy || (a.agents?.length ?? 0) > 0} title={a.agents?.length ? `used by ${a.agents.join(', ')}` : 'delete'}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
		</div>
	{/if}
</div>

{#if login}
	<h2>ChatGPT login for {login.account.name}</h2>
	<div class="panel">
		{#if login.status.state === 'waiting'}
			{#if login.status.url && login.status.code}
				<p>1. Open <a href={login.status.url} target="_blank" rel="noopener noreferrer">{login.status.url}</a> and sign in with the ChatGPT account the agents should use.</p>
				<p>2. Enter this one-time code (it expires in 15 minutes): <code class="otp">{login.status.code}</code></p>
				<p class="muted"><span class="spinner"></span> Waiting for the code to be entered…</p>
			{:else}
				<p class="muted"><span class="spinner"></span> Starting codex…</p>
			{/if}
			<button type="button" class="ghost" onclick={cancelLogin}>Cancel</button>
		{:else if login.status.state === 'done'}
			<p>Logged in. Agents using this account get the login with their next task, and the manager keeps it refreshed.</p>
			<button type="button" class="ghost" onclick={() => (login = null)}>Close</button>
		{:else}
			<p class="err">The login {login.status.state === 'cancelled' ? 'was cancelled' : 'failed'}{login.status.error ? ': ' + login.status.error : '.'}</p>
			<button type="button" class="ghost" onclick={() => (login = null)}>Close</button>
		{/if}
	</div>
{/if}

<h2>{editing ? `Edit ${editing.name}` : 'New account'}</h2>
<div class="panel">
	<form onsubmit={(e) => { e.preventDefault(); submit(); }}>
		<div class="field">
			<label for="a-name">Name</label>
			<input id="a-name" type="text" bind:value={name} placeholder="Work OpenAI" required />
		</div>
		<div class="field">
			<label for="a-kind">Kind</label>
			<select id="a-kind" bind:value={kind} onchange={onKindChange} disabled={!!editing}>
				{#each kinds as k}
					<option value={k.id}>{k.label}</option>
				{/each}
			</select>
			<p class="hint">{selectedKind?.hint ?? ''}</p>
		</div>
		{#each selectedKind?.secretFields ?? [] as f}
			<div class="field">
				<label for="a-{f}">{f}</label>
				<input id="a-{f}" type="password" bind:value={secretValues[f]} placeholder={editing ? 'keep the stored value' : ''} autocomplete="off" />
			</div>
		{/each}
		<div class="field">
			<label for="a-notes">Notes</label>
			<input id="a-notes" type="text" bind:value={notes} placeholder="optional, e.g. which subscription or server this is for" />
		</div>
		<div class="row">
			<button type="submit" disabled={!!busy}>
				{#if busy}<span class="spinner"></span>{editing ? 'Saving…' : 'Creating…'}{:else}{editing ? 'Save' : 'Create account'}{/if}
			</button>
			{#if editing}<button type="button" class="ghost" onclick={startCreate}>Cancel</button>{/if}
		</div>
	</form>
</div>
