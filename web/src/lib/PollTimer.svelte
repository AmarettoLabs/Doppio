<script lang="ts">
	// Countdown to the agent's next git poll: two concentric rings — the outer
	// ring sweeps the whole poll interval (minutes scale), the inner ring
	// sweeps the seconds of the current minute.
	let { nextPollAt, pollSeconds = 0, running = true, textOnly = false }: { nextPollAt?: string; pollSeconds?: number; running?: boolean; textOnly?: boolean } = $props();

	let now = $state(Date.now());
	$effect(() => {
		const t = setInterval(() => (now = Date.now()), 500);
		return () => clearInterval(t);
	});

	const remainingMs = $derived(
		nextPollAt && running ? Math.max(0, new Date(nextPollAt).getTime() - now) : null
	);
	const totalMs = $derived(Math.max(1, (pollSeconds || 1) * 1000));
	// outer ring: fraction of the whole interval left; inner ring: seconds within the current minute
	const outerFrac = $derived(remainingMs === null ? 0 : remainingMs / totalMs);
	const innerFrac = $derived(remainingMs === null ? 0 : (remainingMs % 60000) / 60000);

	const R_OUT = 17;
	const R_IN = 11.5;
	const C_OUT = 2 * Math.PI * R_OUT;
	const C_IN = 2 * Math.PI * R_IN;

	// minutes left, shown in the center of the wheel
	const minutesLeft = $derived(remainingMs === null ? null : Math.ceil(remainingMs / 60000));

	function fmt(ms: number): string {
		const s = Math.ceil(ms / 1000);
		const m = Math.floor(s / 60);
		const sec = s % 60;
		return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`;
	}
</script>

{#if remainingMs === null}
	<span class="muted">—</span>
{:else if textOnly}
	<span class="poll-label" title="next git check">{fmt(remainingMs)} left</span>
{:else}
	<span class="poll-timer" title="next git check">
		<svg viewBox="0 0 40 40" width="88" height="88">
			<circle cx="20" cy="20" r={R_OUT} fill="none" stroke="#2a2f3a" stroke-width="3.5" />
			<circle
				cx="20" cy="20" r={R_OUT} fill="none" stroke="var(--accent)" stroke-width="3.5"
				stroke-linecap="round" stroke-dasharray={C_OUT}
				stroke-dashoffset={C_OUT * (1 - outerFrac)}
				transform="rotate(-90 20 20)"
			/>
			<circle cx="20" cy="20" r={R_IN} fill="none" stroke="#2a2f3a" stroke-width="3" />
			<circle
				cx="20" cy="20" r={R_IN} fill="none" stroke="var(--warn)" stroke-width="3"
				stroke-linecap="round" stroke-dasharray={C_IN}
				stroke-dashoffset={C_IN * (1 - innerFrac)}
				transform="rotate(-90 20 20)"
			/>
			<text x="20" y="21" text-anchor="middle" dominant-baseline="middle" class="wheel-minutes">
				{minutesLeft ?? ''}
			</text>
		</svg>
	</span>
{/if}

<style>
	.poll-timer {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
	}
	.poll-label {
		font-variant-numeric: tabular-nums;
		color: var(--text);
		font-size: 0.8rem;
		min-width: 4.2em;
	}
	.wheel-minutes {
		fill: var(--muted);
		font-size: 9.75px; /* 25% smaller than before */
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
</style>
