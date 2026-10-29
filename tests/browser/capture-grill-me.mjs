// Render docs/assets/grill-me.png from a real Grill-Me CLI session.
// The interview runs in a disposable Git project; the image is a terminal-style
// rendering of the captured JSON fields, not a product GUI.
import { chromium } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const OUT = fileURLToPath(new URL('../../docs/assets/grill-me.png', import.meta.url));
const python = process.env.AO_PYTHON || 'python3.11';
const project = await mkdtemp(join(tmpdir(), 'ao-grill-showcase-'));

function run(args) {
  return JSON.parse(execFileSync(python, [
    join(ROOT, 'tools/universal_teamwork.py'),
    '--root', project, '--platform', 'opencode', ...args,
  ], { cwd: ROOT, encoding: 'utf8' }));
}

function options(question) {
  return question.options.map((option) => {
    const mark = option.recommended ? '●' : '○';
    const suffix = option.recommended ? '   (recommended)' : '';
    return `    ${mark} ${option.label}${suffix}`;
  }).join('\n');
}

function english(value) {
  const [turkish, ...rest] = String(value).split(' / ');
  return rest.length ? rest.join(' / ') : turkish;
}

try {
  execFileSync('git', ['init', '-q', '-b', 'main', project]);
  await writeFile(join(project, 'package.json'),
    '{"name":"login-demo","version":"0.1.0"}\n');
  execFileSync('git', ['-C', project, 'add', '.']);
  execFileSync('git', ['-C', project, '-c', 'user.email=demo@example.com',
    '-c', 'user.name=Demo', 'commit', '-qm', 'init']);

  const started = run(['grill', 'start', '--request',
    'Add a user login system with sessions.']);
  const first = started.question;
  const answered = run(['grill', 'answer', '--question-id', first.question_id,
    '--option', 'cookie']);
  const second = answered.next;

  const transcript = [
    '$ python tools/universal_teamwork.py --root . --platform opencode grill start \\',
    '    --request "Add a user login system with sessions."',
    '',
    `GRILL-ME  ${started.interview.interview_id}  status: ${started.interview.status}`,
    '1 open critical question · one question at a time',
    '',
    `Q1  ${english(first.question)}`,
    options(first),
    '',
    `    why  ${english(first.why)}`,
    '',
    '$ python tools/universal_teamwork.py --root . --platform opencode grill answer \\',
    `    --question-id ${first.question_id} --option cookie`,
    '',
    `✓ Answer recorded  status: ${answered.question.status}`,
    `Open questions: 1 · next depends on ${second.dependency || '—'}`,
    '',
    `Q2  ${english(second.question)}`,
    options(second),
    '',
    `    why  ${english(second.why)}`,
  ].join('\n');

  const html = `<!doctype html><meta charset="utf-8">
<style>
  html,body{margin:0;width:1200px;height:760px;background:#0b0e14;color:#d7dce5;
    font:14px/1.55 "SFMono-Regular",Menlo,Consolas,monospace}
  .window{position:relative;box-sizing:border-box;height:100%;padding:26px 34px;background:
    radial-gradient(1200px 500px at 20% 0%,#151b26 0%,#0b0e14 60%)}
  .bar{display:flex;align-items:center;gap:8px;color:#8b95a7;font-size:12px;
    border-bottom:1px solid #222a38;padding-bottom:14px;margin-bottom:20px}
  .dot{width:11px;height:11px;border-radius:50%}
  .title{margin-left:10px}
  pre{margin:0;white-space:pre-wrap;font:inherit}
  .prompt{color:#7dd3fc}.cmd{color:#e6edf7}.meta{color:#fbbf24}
  .q{color:#f4f7fb;font-weight:600}.opt{color:#c9d2e0}
  .why{color:#8b95a7}.ok{color:#34d399}
  .foot{position:absolute;left:34px;right:34px;bottom:22px;color:#677184;
    font-size:11px;border-top:1px solid #1b2230;padding-top:10px}
</style>
<div class="window">
  <div class="bar">
    <span class="dot" style="background:#ff5f57"></span>
    <span class="dot" style="background:#febc2e"></span>
    <span class="dot" style="background:#28c840"></span>
    <span class="title">grill-me — real CLI output (abridged)</span>
  </div>
  <pre>${transcript.split('\n').map((line) => {
    const safe = line.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    if (line.startsWith('$')) return `<span class="prompt">$</span><span class="cmd">${safe.slice(1)}</span>`;
    if (line.startsWith('GRILL-ME')) return `<span class="meta">${safe}</span>`;
    if (/^Q\d/.test(line)) return `<span class="q">${safe}</span>`;
    if (line.startsWith('✓')) return `<span class="ok">${safe}</span>`;
    if (/^\s+[●○]/.test(line)) return `<span class="opt">${safe}</span>`;
    if (/^\s+why/.test(line)) return `<span class="why">${safe}</span>`;
    return safe;
  }).join('\n')}</pre>
  <div class="foot">Captured from a disposable demo project · local paths shortened · demo answers are not a real product interview</div>
</div>`;

  const browser = await chromium.launch({
    channel: process.env.AO_BROWSER_CHANNEL || 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 760 },
    deviceScaleFactor: 1 });
  await page.setContent(html);
  await page.screenshot({ path: OUT, type: 'png' });
  await browser.close();
  console.log('wrote', OUT);
} finally {
  await rm(project, { recursive: true, force: true });
}
