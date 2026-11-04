<script lang="ts">
	import '../app.css';

	let { children }: { children: import('svelte').Snippet } = $props();
</script>

<svelte:head>
	<link rel="icon" href="/assets/doppio-logo.png" />
	<title>Doppio</title>
</svelte:head>

<nav>
	<a class="brand" href="/"><img src="/assets/doppio-logo.png" alt="Doppio" width="28" height="28" /> Doppio</a>
	<span class="spacer"></span>
	<a class="btn" href="/agents/new">+ New agent</a>
</nav>
<main>{@render children()}</main>
