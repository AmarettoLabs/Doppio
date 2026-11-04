import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// The Go backend serves the built SPA, so build static files with an
			// SPA fallback for client-side routing.
			adapter: adapter({ fallback: 'index.html' })
		})
	],
	server: {
		proxy: {
			// In dev, forward API calls to the Go server.
			'/api': 'http://localhost:8080'
		}
	}
});
