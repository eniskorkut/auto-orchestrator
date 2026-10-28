import { expect, test } from '@playwright/test';
import { DEMO, clickAgent, demoProjectId, openOffice, trackPageErrors, waitForSettled } from './helpers.mjs';

let errors = [];

test.beforeEach(async ({ page }) => {
  errors = trackPageErrors(page);
});

test.afterEach(() => {
  expect(errors, 'browser console, page and HTTP errors').toEqual([]);
});

test('milestone panel shows verified progress and filters the task board', async ({ page }) => {
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  await expect(page.locator('.milestone-card')).toHaveCount(3);
  await expect(page.locator('#board-milestones')).toContainText('DEMO DATA');
  await expect(page.locator('#board-milestones')).toContainText('1/3 verified');
  await expect(page.locator('#board-milestones')).toContainText('No estimated progress is shown');
  await expect(page.locator('#board-milestones .mode-chip', { hasText: 'BALANCED' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .execution-chip')).toContainText('demo');
  await expect(page.locator('#board-milestones .cost-chip')).toContainText('cost unknown');
  await expect(page.locator('#board-milestones .token-chip')).toContainText('tokens unknown');
  await expect(page.locator('#board-milestones .recovery-chip', { hasText: 'ACTIVE' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .scheduler-chip')).toContainText('running');

  await page.locator('.milestone-card').filter({ hasText: 'Build and verify' }).click();
  await expect(page.locator('.milestone-card.selected')).toHaveCount(1);
  await expect(page.locator('.milestone-card.selected')).toContainText('M2');
  await expect(page.locator('.task-card')).toHaveCount(2);

  await page.getByRole('button', { name: 'Clear milestone filter' }).click();
  await expect(page.locator('.milestone-card.selected')).toHaveCount(0);
  await expect(page.locator('.task-card')).toHaveCount(5);
});

test('teamwork chips expose SOLO, provider, contract and measured usage honestly', async ({ page }) => {
  await page.route('**/api/projects/*/milestones', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const run = payload.runs[0];
    run.work_mode = 'auto';
    run.mode_label = 'solo';
    run.execution = {
      provider: 'opencode', origin_platform: 'opencode',
      native_or_managed: 'managed', native_session_claimed: false,
      cross_ide_spawn: false, plan_only: false,
    };
    run.contract_status = 'partially_verified';
    run.contract_fully_verified = false;
    run.contract_not_verified = 2;
    run.contract_verification = {
      contract_id: 'rc_aaaaaaaaaaaaaaaa', contract_version: 4,
      status: 'partially_verified', fully_verified: false,
      passed: 3, not_verified: 2, reasons: ['manual review remains'],
    };
    run.requirements = {
      status: 'partially_verified', fully_verified: false,
      verified_completion: false, critical_unverified: 1,
      criteria: [], reasons: ['independent review remains'],
    };
    run.requirements_status = 'partially_verified';
    run.verified_completion = false;
    run.usage = {
      cost_total_usd: 0.5, cost_measured: true, cost_unknown: false,
      tokens_measured: true, tokens_unknown: false,
      tokens: { input: 120, output: 30, cache_read: 0 }, by_provider: [],
    };
    await route.fulfill({ response, json: payload });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  await expect(page.locator('#board-milestones .mode-chip')).toHaveText('SOLO');
  await expect(page.locator('#board-milestones .execution-chip')).toContainText('managed · opencode');
  await expect(page.locator('#board-milestones .contract-chip:not(.requirements-chip)')).toContainText('partial');
  await expect(page.locator('#board-milestones .contract-chip:not(.requirements-chip)')).toContainText('2 not verified');
  await expect(page.locator('#board-milestones .contract-chip[data-requirements-status="partially_verified"]')).toContainText('1 critical open');
  await expect(page.locator('#board-milestones .cost-chip')).toContainText('measured $0.5000');
  await expect(page.locator('#board-milestones .token-chip')).toContainText('120 in / 30 out');
});

test('project memory shows interview age and bounded handoff history', async ({ page }) => {
  await page.route('**/api/projects/*/memory', async (route) => {
    await route.fulfill({
      json: {
        available: true, project_id: 'demo', summary: 'History',
        state: { last_platform: 'codex', stale: false },
        last_ide: 'codex',
        active_interview: {
          interview_id: 'iv_aaaaaaaaaaaaaaaa', status: 'open',
          age_seconds: 90, request_summary: 'login feature',
          question: { question: 'How should sessions work?', options: [] },
        },
        contract_status: 'not_run', contract_not_verified: null,
        handoffs: [
          { milestone_id: 'M3', platform: 'opencode', created_at: 0, verified: true },
          { milestone_id: 'M2', platform: 'codex', created_at: 0, verified: false },
        ],
        handoff: { verified: true, payload: { context_summary: 'M3 passed' } },
        stale: { is_stale: false, reasons: [] },
      },
    });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.project-memory');
  await expect(page.locator('.memory-interview-meta')).toContainText('id iv_aaaaaaa');
  await expect(page.locator('.memory-handoff-history')).toContainText('IDE handoff history');
  await expect(page.locator('.memory-handoff-history li')).toHaveCount(2);
});

test('disconnected runner shows recovery and orphan state without working animation', async ({ page }) => {
  await page.route('**/api/projects/*/milestones', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const run = payload.runs[0];
    run.recovery_state = 'runner_disconnected';
    run.owner_connected = false;
    run.orphaned_managed_sessions = 1;
    run.scheduler = { ...(run.scheduler || {}), active_sessions: 0 };
    run.managed_sessions = [{
      task_id: 'backend', attempt: 1, provider: 'opencode', status: 'running',
      managed_status: 'orphaned', session_hash: 'orphan123', tool_count: 0,
    }];
    await route.fulfill({ response, json: payload });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  await expect(page.locator('#board-milestones .recovery-chip',
    { hasText: 'RUNNER DISCONNECTED' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .orphan-chip',
    { hasText: '1 orphaned' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .scheduler-chip')).toContainText('0 running');
});

test('project memory panel shows decisions and cross-IDE context', async ({ page }) => {
  await page.route('**/api/projects/*/memory', async (route) => {
    await route.fulfill({
      json: {
        available: true, project_id: 'demo', summary: 'Confirmed project memory',
        state: { last_platform: 'codex', stale: true,
                 stale_reasons: ['head_mismatch'] },
        last_ide: 'codex', last_milestone: 'M2',
        confirmed_decisions: [{ topic: 'auth_session', decision: 'JWT', status: 'confirmed' }],
        derived_decisions: [{ topic: 'db', decision: 'SQLite', status: 'code_derived' }],
        requirements: [
          ...Array.from({ length: 6 }, (_, index) => ({
            text: `Resolved requirement ${index}`, status: 'confirmed',
          })),
          { text: 'Use secure cookies', status: 'unresolved_blocking' },
        ],
        interviews: [{ status: 'open', origin_platform: 'codex',
                       request_summary: 'login feature' }],
        active_interview: {
          status: 'open', origin_platform: 'codex', request_summary: 'login feature',
          question: { question: 'How should sessions be stored?',
                      options: [{ label: 'Secure cookie', recommended: true },
                                { label: 'JWT', recommended: false }] },
        },
        approved_requirements: {
          contract_id: 'rc_aaaaaaaaaaaaaaaa', contract_version: 2,
        },
        handoff: { verified: false,
                   payload: { context_summary: 'M1 passed with verified tests' } },
        verification: { kind: 'verification', test_evidence: '11 tests passed' },
        stale: { is_stale: true, reasons: ['head_mismatch'] },
      },
    });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.project-memory');
  await expect(page.locator('.project-memory')).toContainText('Project memory');
  await expect(page.locator('.project-memory')).toContainText('last IDE: codex');
  await expect(page.locator('.project-memory')).toContainText('last milestone: M2');
  await expect(page.locator('.project-memory')).toContainText('contract v2');
  await expect(page.locator('.project-memory')).toContainText('auth_session');
  await expect(page.locator('.project-memory')).toContainText('How should sessions be stored?');
  await expect(page.locator('.project-memory')).toContainText('Use secure cookies');
  await expect(page.locator('.project-memory')).not.toContainText('Resolved requirement 0');
  await expect(page.locator('.project-memory')).toContainText('M1 passed with verified tests');
  await expect(page.locator('.project-memory')).toContainText('Secure cookie — recommended');
  await expect(page.locator('.memory-stale')).toContainText('head_mismatch');
  await expect(page.locator('.memory-handoff-state')).toHaveText('unverified');
  await expect(page.locator('.memory-verification')).toContainText('11 tests passed');
});

test('project memory shows blockers and a verified handoff only when evidence exists', async ({ page }) => {
  await page.route('**/api/projects/*/memory', async (route) => {
    await route.fulfill({
      json: {
        available: true, project_id: 'demo', summary: 'Verified project memory',
        state: { last_platform: 'opencode', stale: false },
        last_ide: 'opencode', last_milestone: 'M3',
        confirmed_decisions: [{ topic: 'auth_session', decision: 'JWT', status: 'confirmed' }],
        requirements: [{ text: 'Resolved legacy item', status: 'confirmed' }],
        blockers: [{ text: 'Rotate secrets', status: 'unresolved_blocking' }],
        approved_requirements: { contract_version: 4 },
        handoff: { verified: true,
                   payload: { context_summary: 'M3 passed with tests' } },
        stale: { is_stale: false, reasons: [] },
      },
    });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.project-memory');
  await expect(page.locator('.project-memory')).toContainText('Rotate secrets');
  await expect(page.locator('.project-memory')).not.toContainText('Resolved legacy item');
  await expect(page.locator('.memory-handoff-state')).toHaveText('verified');
  await expect(page.locator('.memory-stale')).toHaveCount(0);
});

test('office inspector keeps task, evidence and contract in one panel', async ({ page }) => {
  await page.route('**/api/projects/*/agents/*', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.contract = {
      contract: { contract_id: 'rc_aaaaaaaaaaaaaaaa', contract_version: 5 },
      verification: {
        contract_id: 'rc_aaaaaaaaaaaaaaaa', contract_version: 5,
        status: 'partially_verified', fully_verified: false,
        passed: 2, not_verified: 1, reasons: [],
      },
      status: 'partially_verified', fully_verified: false, not_verified: 1,
    };
    await route.fulfill({ response, json: payload });
  });
  await openOffice(page, { clearLayout: true });
  await waitForSettled(page);
  await clickAgent(page, 'Ada');
  await expect(page.locator('.inspector-contract')).toContainText('contract v5');
  await expect(page.locator('.inspector-contract')).toContainText('partially verified');
  await expect(page.locator('.inspector-contract')).toContainText('1 criteria not verified');
  await expect(page.locator('#inspector-content')).toContainText('Task');
});

test('office milestone strip opens the matching board milestone', async ({ page }) => {
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/office`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.office-milestone-strip');
  await expect(page.locator('.office-milestone-strip')).toContainText('M2');
  await expect(page.locator('.office-milestone-strip')).toContainText('1/3 verified');
  await page.locator('.office-milestone-open').click();
  await expect(page).toHaveURL(/#\/p\/.*\/board/);
  await expect(page.locator('.milestone-card.selected')).toContainText('M2');
});

test('IDE ownership panel separates active, previous and last IDE without leaking owner secrets', async ({ page }) => {
  await page.route('**/api/projects/*/milestones', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.ide = {
      available: true, source: 'ide_handoff', demo: false,
      active_ide: 'cursor', previous_ide: 'codex', last_ide: 'cursor',
      ownership: {
        state: 'handoff', record_present: true,
        owner: { platform: 'cursor', host_hash: 'host12345678', session_hash: 'sess12345678' },
        previous_owner: { platform: 'codex', host_hash: 'host12345678', session_hash: 'olds12345678' },
        fence: 4, generation: 9,
      },
      handoff: {
        source: 'codex', target: 'cursor', digest: 'abcdef1234567890',
        drift: true, age_seconds: 125,
      },
      current: {
        run_id: 'demo-project', run_status: 'running', milestone_id: 'M2',
        milestone_status: 'verifying', task_id: 'api', task_state: 'completed',
      },
      next_action: { code: 'review_handoff_drift', label: 'Review the handoff drift before continuing' },
      requirements_completion: {
        status: 'review_needed', fully_verified: false, not_verified: 2,
      },
      grill_me: {
        question: 'Which IDE should own M3?',
        why: 'Ownership must be explicit before a native handoff.',
        rationale: 'A fenced owner record prevents two supervisors.',
        alternatives: ['Stay in Cursor', 'Return to Codex'],
      },
      recovery: { state: 'handoff_prepared', blocked: false, owner_process: 'alive' },
      stale_context: { is_stale: true, reasons: ['head_mismatch'] },
      native: { connected: null, evidence: 'unknown', claimed_live: false },
      unknowns: ['generation_unknown', 'native_connection_not_observed'],
    };
    await route.fulfill({ response, json: payload });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.ide-ownership');
  await expect(page.locator('.ide-active')).toHaveText('active IDE: cursor');
  await expect(page.locator('.ide-previous')).toHaveText('previous IDE: codex');
  await expect(page.locator('.ide-last')).toHaveText('last IDE: cursor');
  await expect(page.locator('.ide-fence')).toContainText('fence 4');
  await expect(page.locator('.ide-fence')).toContainText('generation 9');
  await expect(page.locator('.ide-owner')).toContainText('host12345678');
  await expect(page.locator('.ide-owner')).toContainText('sess12345678');
  await expect(page.locator('.ide-handoff-route')).toContainText('codex → cursor');
  await expect(page.locator('.ide-handoff-age')).toContainText('2m');
  await expect(page.locator('.ide-handoff-drift')).toContainText('context drift detected');
  await expect(page.locator('.ide-current-milestone')).toContainText('M2');
  await expect(page.locator('.ide-current-task')).toContainText('api');
  await expect(page.locator('.ide-next')).toContainText('Review the handoff drift');
  await expect(page.locator('.ide-requirements')).toContainText('review needed');
  await expect(page.locator('.ide-question')).toContainText('Which IDE should own M3?');
  await expect(page.locator('.ide-question-why')).toContainText('Ownership must be explicit');
  await expect(page.locator('.ide-question-alternatives')).toContainText('Stay in Cursor');
  await expect(page.locator('.ide-stale')).toContainText('head_mismatch');
  await expect(page.locator('.ide-native')).toContainText('Native connection unknown');
  await expect(page.locator('.ide-ownership')).not.toContainText('owner_token');
  await expect(page.locator('.ide-ownership')).not.toContainText('ses_raw');
});

test('demo IDE ownership stays labelled and explicitly unknown', async ({ page }) => {
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.ide-ownership');
  await expect(page.locator('.ide-ownership')).toContainText('DEMO DATA');
  await expect(page.locator('.ide-active')).toHaveText('active IDE: unknown');
  await expect(page.locator('.ide-previous')).toHaveText('previous IDE: unknown');
  await expect(page.locator('.ide-native')).toContainText('Native connection unknown');
  await expect(page.locator('.ide-recovery')).toContainText('Recovery: demo');
  await expect(page.locator('.ide-fence')).toHaveText('fence / generation unknown');
});

test('milestone panel stays usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(page.locator('.milestone-card')).toHaveCount(3);
  await page.screenshot({ path: 'tests/browser/screenshots/office-milestones-390x844.png', fullPage: false });
});
