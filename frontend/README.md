# Frontend

The React 18 + TypeScript + Vite PWA of SPEC section 11 arrives with **M4**.
Milestone M1 is instrument control; there is nothing to render yet.

The directory layout under `src/` follows SPEC section 6 — `components/`,
`views/` (Plan, Live, Agent, Archive), `locales/{en,fr,es}/`, `theme/`.

Two things are settled before the first line is written:

- **Night mode is the default theme**, not an option. Red on black, no white
  surfaces (SPEC section 11.5). At 03:00 a white UI costs half an hour of dark
  adaptation.
- **Every user-facing string is a translation key.** A literal string in a
  rendered component is a CI failure (SPEC section 12, CLAUDE.md section 6).
