# CTS-G Project Guide

Independent BingX pulse desks with a full indication / Set / coordination stack and a desk UI.

## Project Structure
- `src/`: Desk UI (React/Vite)
- `server/pulse/`: Engine (Python)
  - `pulse_trader.py`: Main entry point for the engine
  - `pulse_http.py`: Sidecar for stats/control (port :3015)
  - `indication_engine.py`, `set_engine.py`, `coord_engine.py`, `block_engine.py`, `dca_engine.py`, `exit_engine.py`: Core logic modules
  - `overlay-bingx-x01.json` (Live), `overlay-bingx-x02.json` (VST): Connection overlays
- `deploy/`: Linux installation and update scripts
- `scripts/`: Test, smoke, and helper scripts

## Common Commands

### UI (Desk)
- `npm install`: Install dependencies
- `npm run dev`: Start development server (port :8080)
- `npm run build`: Build for production
- `npm run typecheck`: Run TypeScript type checking
- `npm test`: Run JS/TS tests

### Engine (Pulse)
- `python3 scripts/engine-test.py`: Run engine-level tests (sets, indications, rank, etc.)
- `sudo systemctl restart grok-pulse@bingx-x01`: Restart Live engine
- `sudo systemctl restart grok-pulse@bingx-x02`: Restart VST engine
- `sudo systemctl restart grok-desk`: Restart Desk UI service
- `sudo systemctl restart grok-pulse-http`: Restart Pulse HTTP sidecar

### General
- `npm run lint`: Lint UI code
- `npm run format`: Format UI code

## Development Guidelines
- **UI**: Follow React 19 / TanStack patterns. Use Tailwind CSS 4 for styling.
- **Engine**: Maintain Python 3.13 compatibility. Ensure Redis key consistency for connection info.
- **Deployment**: Always verify deployments with `scripts/engine-test.py` and browser checks.
- **Secrets**: Never commit API keys or private identities to Git. Use Redis for runtime keys.
