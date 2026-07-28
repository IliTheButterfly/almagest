/// <reference types="vite/client" />

// Pulls in Vite's declarations for asset imports. The one this build needs is
// `*?url`, used to bundle `zxing_reader.wasm` rather than fetch it from a CDN —
// see `lib/scan/decoder.ts`.
