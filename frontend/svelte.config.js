import adapter from '@sveltejs/adapter-vercel';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	trailingSlash: 'ignore',
	kit: {
		adapter: adapter()
	}
};

export default config;