# kanban-react

A project management kanban board: create tickets and add new statuses (columns) as you need them. React 19 + Vite, plain JavaScript, tested with Vitest and React Testing Library.

The app is **generated** by the factory's `web-app` runbook from a spec. `src/App.jsx` and `src/App.css` are empty skeletons until a build has run, so `npm run dev` shows a blank page before that.

## Build the app

From the repo root (`a2a-poc/`):

```bash
uv sync
uv run factory run --runbook web-app --name kanban-react --rebuild \
    --brief "a project management kanban board: create tickets and add new statuses (columns)"
```

1. Answer the requirements agent's questions, then accept the spec (press Enter) or type the changes you want.
2. The factory writes the tests, builds the app and checks every acceptance criterion itself.
3. On success it prints `Done.`

The build writes `SPEC.json` and `src/`, plus `.factory/run.json` (the run log) and `.factory/agents/` (agent logs) in this directory. See the root `README.md` for the environment variables the factory needs.

## Start the app

```bash
cd apps/kanban-react
npm install        # first time only; the factory already does this
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
