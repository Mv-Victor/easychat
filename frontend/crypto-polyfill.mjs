// crypto-polyfill.mjs
// Polyfill globalThis.crypto for Node.js < 19 (e.g., CentOS 7 with Node.js 16)
// This file must be loaded via NODE_OPTIONS='--import ./crypto-polyfill.mjs'
// BEFORE Vite starts, so that globalThis.crypto is available during resolveConfig.
import { webcrypto } from 'node:crypto'
if (!globalThis.crypto) {
  globalThis.crypto = webcrypto
}
