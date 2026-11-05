# Recoup operator UI

Next.js 16 (App Router, client components) over the Recoup API.

```bash
npm install
npm run dev     # http://localhost:3000, talks to the API at http://127.0.0.1:8000
npm run build   # production build (standalone output for the container image)
```

`.env.development` points local dev at the demo API key, `dev-key`. Production builds
bake in no key: each person signs in with their own, and it is kept in that browser tab
only.
