import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	trailingSlash: 'ignore',
	kit: {
		// Every page loads its data in the browser from the FastAPI backend,
		// so the frontend ships as a static single-page app.
		adapter: adapter({ fallback: 'index.html' })
	}
};

export default config;
