import test from 'node:test';
import assert from 'node:assert/strict';
import {
  mkdtempSync, mkdirSync, realpathSync, rmSync, symlinkSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { helperCandidates, resolveHelperScript } from '../../adapters/opencode-v2/auto_orchestrator.js';

const touch = (path, text = '') => {
  mkdirSync(join(path, '..'), { recursive: true });
  writeFileSync(path, text);
};
const samePath = (left, right) => {
  const norm = (value) => {
    let result = realpathSync(value);
    if (process.platform === 'win32') {
      result = result.replace(/^\\\\\?\\/, '').toLowerCase();
    }
    return result;
  };
  return norm(left) === norm(right);
};

test('project-local plugin resolves helpers from project tools', () => {
  const root = mkdtempSync(join(tmpdir(), 'ao-project-helper-'));
  try {
    const project = join(root, 'project');
    const plugin = join(project, '.opencode', 'plugins', 'auto_orchestrator.js');
    const helper = join(project, 'tools', 'router.py');
    touch(plugin, '// plugin');
    touch(helper, '# router');
    assert.deepEqual(
      helperCandidates({ pluginPath: plugin, projectDirectory: project, name: 'router.py' }),
      [
        join(project, '.opencode', 'auto-orchestrator', 'tools', 'router.py'),
        helper,
      ],
    );
    const projectResolved = resolveHelperScript({ pluginPath: plugin, projectDirectory: project, name: 'router.py' });
    assert.ok(projectResolved && samePath(projectResolved, helper));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('global plugin uses its installed helper and never project-owned tools', () => {
  const root = mkdtempSync(join(tmpdir(), 'ao-global-helper-'));
  try {
    const home = join(root, 'home');
    const project = join(root, 'project');
    const plugin = join(home, '.config', 'opencode', 'plugins', 'auto_orchestrator.js');
    const installed = join(home, '.config', 'opencode', 'auto-orchestrator', 'tools', 'supervisor.py');
    const projectOwned = join(project, 'tools', 'supervisor.py');
    touch(plugin, '// plugin');
    touch(installed, '# installed');
    touch(projectOwned, '# hostile project script');
    assert.deepEqual(
      helperCandidates({ pluginPath: plugin, projectDirectory: project, name: 'supervisor.py' }),
      [installed],
    );
    const globalResolved = resolveHelperScript({ pluginPath: plugin, projectDirectory: project, name: 'supervisor.py' });
    assert.ok(globalResolved && samePath(globalResolved, installed));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('missing, escaping and symlinked helpers fail closed', () => {
  const root = mkdtempSync(join(tmpdir(), 'ao-helper-fail-'));
  try {
    const project = join(root, 'project');
    const plugin = join(project, '.opencode', 'plugins', 'auto_orchestrator.js');
    const tools = join(project, 'tools');
    const outside = join(root, 'outside.py');
    touch(plugin, '// plugin');
    touch(outside, '# outside');
    assert.equal(resolveHelperScript({ pluginPath: plugin, projectDirectory: project, name: 'router.py' }), null);

    mkdirSync(tools, { recursive: true });
    if (process.platform !== 'win32') {
      symlinkSync(outside, join(tools, 'router.py'));
      assert.equal(resolveHelperScript({ pluginPath: plugin, projectDirectory: project, name: 'router.py' }), null);
    }

    assert.deepEqual(
      helperCandidates({ pluginPath: plugin, projectDirectory: project, name: '../escape.py' }),
      [],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
