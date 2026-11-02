import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../components/ui/Button';
import { agentExecutionRepository } from '../../services/runtime';
import type { ToolOperation, TerminalResult } from '../../types/domain';
export function terminalResult(operation: ToolOperation): TerminalResult | null {
  if (operation.tool_name !== 'terminal.run' || !operation.result_summary) return null;
  try {
    const result = JSON.parse(operation.result_summary) as TerminalResult;
    return typeof result.stdout === 'string' && typeof result.stderr === 'string'
      && (result.exit_code === null || Number.isInteger(result.exit_code)) ? result : null;
  } catch { return null; }
}
export function TerminalCommandCard({ operation, onResolved }: { operation: ToolOperation; onResolved(): void }) {
  const { t } = useTranslation('chat');
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const result = terminalResult(operation);
  async function resolve(approve: boolean) {
    if (busy) return; setBusy(true); setError(null);
    try { await agentExecutionRepository.resolveTerminal(operation.id, approve); onResolved(); }
    catch (value) { setError(String(value)); } finally { setBusy(false); }
  }
  return <section className="terminal-command-card change-set-card">
    <header><strong>PowerShell</strong><span>{t(`tools.operationStatus.${operation.status}`)}</span></header>
    <pre>{String(operation.arguments?.command ?? '')}</pre>
    <p>{t('terminal.cwd')}: <code>{String(operation.arguments?.cwd ?? '.')}</code> · {t('terminal.timeout')}: {Number(operation.arguments?.timeout_ms ?? 120000)} ms</p>
    {operation.status === 'waiting_user' ? <>
      <p>{t('terminal.authority')}</p><div className="change-set-card__actions">
        <Button disabled={busy} onClick={() => void resolve(false)}>{t('terminal.reject')}</Button>
        <Button variant="primary" disabled={busy} onClick={() => void resolve(true)}>{t('terminal.approve')}</Button>
      </div></> : null}
    {result ? <>
      <p>{result.timed_out ? t('terminal.timedOut') : result.cancelled ? t('terminal.cancelled') : result.exit_code === null ? t('terminal.unknown') : t('terminal.exit', { code: result.exit_code })} · {result.duration_ms} ms</p>
      {result.stdout ? <><strong>stdout</strong><pre>{result.stdout}</pre></> : null}
      {result.stderr ? <><strong>stderr</strong><pre>{result.stderr}</pre></> : null}
      {result.stdout_truncated || result.stderr_truncated ? <p>{t('terminal.truncated')}</p> : null}
    </> : ['interrupted', 'cancelled'].includes(operation.status) ? <p>{t('terminal.unknown')}</p> : null}
    {operation.error_code ? <p>{operation.error_code}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
