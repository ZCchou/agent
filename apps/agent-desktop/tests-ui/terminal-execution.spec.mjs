import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';
const pending = {id:'terminal-op',run_id:'terminal-run',sequence:1,tool_name:'terminal.run',status:'waiting_user',error_code:null,
 arguments:{command:'./test.ps1',cwd:'tests',timeout_ms:5000},result_summary:null};
for (const approve of [true,false]) test(`terminal ${approve ? 'approval' : 'rejection'} uses operation identity and displays Host outcome`,async ({page})=>{
 const {work}=await installTauriMock(page, {list_agent_change_sets:[],get_conversation_model_run:{run:null},list_conversation_tool_operations:{operations:[pending]}});
 await page.addInitScript(({pending,approve})=>{
  const invoke=window.__TAURI_INTERNALS__.invoke; let operation=pending; window.__terminalDecisions=[];
  window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
   if(command==='list_conversation_tool_operations') return {operations:[operation]};
   if(command==='resolve_terminal_operation') {window.__terminalDecisions.push(args);operation={...operation,status:approve?'completed':'rejected',result_summary:approve?JSON.stringify({stdout:'actual test output',stderr:'assertion failed',exit_code:7,duration_ms:231,timed_out:false,cancelled:false,stdout_truncated:true,stderr_truncated:false}):null}; return null;}
   return invoke(command,args);
  };
 },{pending,approve});
 await page.goto('http://127.0.0.1:5173'); await page.getByRole('button',{name:work.title,exact:true}).click();
 const card=page.locator('.terminal-command-card'); await expect(card).toContainText('./test.ps1'); await expect(card).toContainText('tests'); await expect(card).toContainText('5000');
 await card.getByRole('button',{name:approve?'Run command':'Reject command',exact:true}).click();
 await expect(card.getByRole('button',{name:'Run command',exact:true})).toHaveCount(0);
 if(approve) {await expect(card).toContainText('Exit code: 7');await expect(card).toContainText('actual test output');await expect(card).toContainText('assertion failed');await expect(card).toContainText('Output truncated');}
 else await expect(card).toContainText('Rejected');
 expect(await page.evaluate(()=>window.__terminalDecisions)).toEqual([{operationId:'terminal-op',approve}]);
});
test('completed terminal output is loaded from persisted audit on reload',async({page})=>{
 const result={stdout:'persisted test evidence',stderr:'',exit_code:0,duration_ms:111,timed_out:false,cancelled:false,stdout_truncated:false,stderr_truncated:false};
 const {work}=await installTauriMock(page,{list_agent_change_sets:[],get_conversation_model_run:{run:null},list_conversation_tool_operations:{operations:[{...pending,status:'completed',result_summary:JSON.stringify(result)}]}});
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:work.title,exact:true}).click();
 await page.locator('.run-activity__summary').click();await expect(page.locator('.terminal-command-card')).toContainText('persisted test evidence');
});

test('delayed terminal audit refresh cannot install another conversation approval card', async ({ page }) => {
  const { work } = await installTauriMock(page, { list_agent_change_sets: [], get_conversation_model_run: { run: null } });
  const other = { ...work, id: 'work-2', title: 'Second work' };
  await page.addInitScript(({ work, other, pending }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    let delayRefresh = false;
    window.__terminalDecisions = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === 'list_conversations') return [work, other];
      if (command === 'get_conversation') return args.conversationId === work.id ? work : other;
      if (command === 'resolve_terminal_operation') {
        window.__terminalDecisions.push(args);
        delayRefresh = true;
        return null;
      }
      if (command === 'list_conversation_tool_operations') {
        if (args.conversationId === other.id) return { operations: [{ ...pending, id: 'b-op', run_id: 'b-run', arguments: { command: 'B command' } }] };
        if (delayRefresh) return new Promise((resolve) => {
          window.__releaseOldAudit = () => resolve({ operations: [{ ...pending, arguments: { command: 'A stale command' } }] });
        });
        return { operations: [pending] };
      }
      return invoke(command, args);
    };
  }, { work, other, pending });
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button', { name: work.title, exact: true }).click();
  await page.locator('.terminal-command-card').getByRole('button', { name: 'Run command', exact: true }).click();
  await page.waitForFunction(() => typeof window.__releaseOldAudit === 'function');
  await page.getByRole('button', { name: other.title, exact: true }).click();
  await expect(page.locator('.terminal-command-card')).toContainText('B command');
  await page.evaluate(async () => {
    window.__releaseOldAudit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await expect(page.locator('.terminal-command-card')).toContainText('B command');
  await expect(page.locator('.terminal-command-card').filter({ hasText: 'A stale command' })).toHaveCount(0);
  expect(await page.evaluate(() => window.__terminalDecisions)).toEqual([{ operationId: 'terminal-op', approve: true }]);
});

test('older terminal audit refresh cannot resurrect an approval after a newer result', async ({ page }) => {
  const { work } = await installTauriMock(page, { list_agent_change_sets: [], get_conversation_model_run: { run: null } });
  await page.addInitScript((pending) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === 'plugin:event|listen') listeners.set(args.event, args.handler);
      if (command === 'list_conversation_tool_operations') {
        if (window.__auditPhase === 'old') return new Promise((resolve) => {
          window.__releaseOldAudit = () => resolve({ operations: [pending] });
        });
        if (window.__auditPhase === 'new') return { operations: [{ ...pending, status: 'completed', result_summary: JSON.stringify({ stdout: 'newest evidence', stderr: '', exit_code: 0, duration_ms: 1, timed_out: false, cancelled: false, stdout_truncated: false, stderr_truncated: false }) }] };
        return { operations: [pending] };
      }
      return invoke(command, args);
    };
    window.__refreshAudit = (phase) => {
      window.__auditPhase = phase;
      const handler = listeners.get('agent-change-set');
      if (!handler) throw new Error('Missing change-set subscription');
      window[`_${handler}`]({ event: 'agent-change-set', id: 1, payload: {} });
    };
  }, pending);
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button', { name: work.title, exact: true }).click();
  await expect(page.locator('.terminal-command-card')).toContainText('./test.ps1');
  await page.evaluate(() => window.__refreshAudit('old'));
  await page.waitForFunction(() => typeof window.__releaseOldAudit === 'function');
  await page.evaluate(() => window.__refreshAudit('new'));
  await expect(page.locator('.terminal-command-card')).toContainText('newest evidence');
  await page.evaluate(async () => {
    window.__releaseOldAudit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await expect(page.locator('.terminal-command-card')).toContainText('newest evidence');
  await expect(page.getByRole('button', { name: 'Run command', exact: true })).toHaveCount(0);
});
