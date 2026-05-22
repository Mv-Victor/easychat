#!/usr/bin/env node
// build.mjs - CentOS 7 / Node.js < 19 compatible build script
// Polyfills globalThis.crypto before vite starts, bypassing NODE_OPTIONS restrictions

import { webcrypto } from 'node:crypto'
if (!globalThis.crypto) {
  globalThis.crypto = webcrypto
}

// Dynamically import vite's build API
const { build } = await import('vite')
await build()
