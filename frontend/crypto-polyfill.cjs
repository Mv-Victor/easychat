// crypto-polyfill.cjs
// Loaded via NODE_OPTIONS='--require ./crypto-polyfill.cjs' BEFORE any module initializes.
// This ensures globalThis.crypto is set before Vite's internal crypto$2 = globalThis.crypto runs.
const { webcrypto } = require('crypto')
if (!global.crypto) {
  global.crypto = webcrypto
}
