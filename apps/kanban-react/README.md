# kanban-react

A project management kanban board: create tickets and add new statuses (columns) as you need them. React 19 + Vite, plain JavaScript, tested with Vitest and React Testing Library.

The app is **generated** by the A2A pipeline from a spec. `src/App.jsx` and `src/App.css` are empty skeletons until a build has run, so `npm run dev` shows a blank page before that.

## Build the app

From the repo root (`a2a-poc/`):

```bash
uv sync
uv run python src/sample/pipeline.py --app kanban-react --rebuild
```

1. Answer the requirements agent's questions, then accept the spec (`y`) or type the changes you want.
2. The pipeline writes the tests, builds the app and verifies every acceptance criterion.
3. On success it prints `Done: all N tests pass and every criterion is accepted.`

The build writes `SPEC.json` and `src/`, plus `.factory/run.json` (the run log) and `.factory/agents/` (agent logs) in this directory. See `src/sample/README.md` for the environment variables the pipeline needs.

## Start the app

```bash
cd apps/kanban-react
npm install        # first time only; the pipeline already does this
npm run dev        # http://localhost:5173
```

Open the URL Vite prints. Tickets and columns are kept in the browser's `localStorage`.

## Other commands

| Command | What it does |
|---|---|
| `npm run build` | production build into `dist/` |
| `npm run preview` | serve the production build |
| `npm test` | run the acceptance tests once |

## Files

| Path | Written by |
|---|---|
| `SPEC.json` | requirements agent, accepted by you |
| `src/App.test.jsx` | tests agent |
| `src/App.jsx`, `src/App.css`, `src/*` | implement agent |
| `package.json`, `vite.config.js`, `index.html`, `src/main.jsx`, `src/setupTests.js` | scaffold from `templates/react-vite`; no agent may change them |
