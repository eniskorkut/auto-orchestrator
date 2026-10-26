#!/usr/bin/env node
/* Cross-platform JS test runner. npm scripts cannot rely on shell glob
 * expansion on Windows, so enumerate the two naming conventions explicitly. */
import { readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here)
  .filter((name) => name.endsWith('.test.mjs') || /^test_.*\.mjs$/.test(name))
  .sort()
  .map((name) => join(here, name));
if (!files.length) {
  console.error('No JS test files found');
  process.exit(1);
}
const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' });
process.exit(result.status == null ? 1 : result.status);
