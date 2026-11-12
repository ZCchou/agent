import { useEffect, useRef, useState } from 'react';
import { ArrowRightLeft, FolderOpen, RefreshCw, RotateCcw, XCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Card } from '../../components/ui/Card';
import { Button, ButtonGroup } from '../../components/ui/Button';
import { useApp } from '../../contexts/AppContext';
import { profileMigrationRepository } from '../../services/runtime';
import type { LegacyProfile, MigrationState } from '../../types/profileMigration';
import './profileMigration.css';

export function MigrationProgress({ state }: { state: MigrationState }) {
  const { t } = useTranslation('common');
  return <div className="profile-migration-progress" aria-live="polite">
    <p role="status">{t(`settings.migration.phases.${state.phase || state.status}`, { defaultValue: state.phase || state.status })}</p>
    {state.current_source && <p>{state.current_source}</p>}
    {state.current_model && <p>{state.current_model}</p>}
    {state.current_file && <p>{state.current_file}</p>}
    {state.status === 'running' && <progress aria-label={t('settings.migration.progress')}
      value={state.total_bytes ? state.completed_bytes : undefined} max={state.total_bytes || undefined} />}
    {state.error && <p role="alert">{state.error.message || t(`settings.migration.errors.${state.error.code}`, { defaultValue: state.error.code })}</p>}
    {state.preview && <section aria-label={t('settings.migration.previewTitle')}>
      <p>{t('settings.migration.conflictEstimate', { count: state.preview.conflicting_groups })}</p>
      {state.preview.storage.map(item => <div key={item.purpose}>
        <p>{t(`settings.migration.storagePurpose.${item.purpose}`)} · {item.path}</p>
        <p>{t('settings.migration.storageEstimate', {
          required: (item.required_bytes / 1024 ** 3).toFixed(2), available: (item.available_bytes / 1024 ** 3).toFixed(2),
        })}</p>
        {!item.sufficient && <p role="alert">{t('settings.migration.spaceInsufficient')}</p>}
      </div>)}
    </section>}
    {!!state.warnings?.length && <details><summary>{t('settings.migration.warnings', { count: state.warnings.length })}</summary>
      <ul>{state.warnings.map((warning, index) => <li key={index}>{t(`settings.migration.warningsByCode.${warning.code}`, { defaultValue: warning.code })}
        {warning.name || warning.model ? ` · ${warning.name || warning.model}` : ''}</li>)}</ul>
    </details>}
  </div>;
}

export function ProfileMigrationCard() {
  const { t } = useTranslation('common');
  const [profiles, setProfiles] = useState<LegacyProfile[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [modelRoot, setModelRoot] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [state, setState] = useState<MigrationState | null>(null);
  const [working, setWorking] = useState(false);
  const [recoveryRequired, setRecoveryRequired] = useState(false);
  const [activated, setActivated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restarting = useRef(false);
  const processing = !!state && ['queued', 'running', 'publishing'].includes(state.status);

  async function discover() {
    const result = await profileMigrationRepository.discover();
    setProfiles(result.profiles);
    setSelected(result.profiles.map(profile => profile.id));
    setModelRoot(result.model_root);
    setJobId(result.latest_job_id);
    setRecoveryRequired(result.recovery_required);
  }

  useEffect(() => { void discover().catch(cause => setError(String(cause))); }, []);
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    async function poll() {
      try {
        const result = await profileMigrationRepository.status(jobId!);
        if (!cancelled) {
          setState(result.state);
          if (typeof result.recovery_required === 'boolean') setRecoveryRequired(result.recovery_required);
          if (typeof result.activated === 'boolean') setActivated(result.activated);
        }
      } catch (cause) { if (!cancelled) setError(String(cause)); }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [jobId]);
  useEffect(() => {
    if (state?.status === 'awaiting_restart' && !restarting.current) {
      restarting.current = true;
      void profileMigrationRepository.restart().catch(cause => { restarting.current = false; setError(String(cause)); });
    }
  }, [state?.status]);

  async function perform(action: () => Promise<unknown>) {
    setWorking(true);
    setError(null);
    try { await action(); } catch (cause) { setError(String(cause)); } finally { setWorking(false); }
  }

  return <Card className="profile-migration-card">
    <h3>{t('settings.migration.title')}</h3>
    <p>{t('settings.migration.description')}</p>
    <fieldset disabled={processing || working}><legend>{t('settings.migration.sources')}</legend>
      {profiles.map(profile => <label key={profile.id}><input type="checkbox" checked={selected.includes(profile.id)}
        onChange={event => setSelected(current => event.target.checked ? [...current, profile.id] : current.filter(id => id !== profile.id))} />
        <span>{profile.label}<small>{profile.root}</small></span></label>)}
      {!profiles.length && <p>{t('settings.migration.noSources')}</p>}
    </fieldset>
    <p>{t('settings.migration.modelStorage')}<span className="profile-migration-path">{modelRoot || t('settings.migration.chooseStorageFirst')}</span></p>
    <ButtonGroup label={t('settings.migration.title')}>
      <Button icon={<FolderOpen />} disabled={working || processing || recoveryRequired}
        disabledReason={recoveryRequired ? t('settings.migration.errors.migration_restart_required') : undefined} onClick={() => void perform(async () => {
        const result = await profileMigrationRepository.chooseStorage();
        if (result) { setModelRoot(result.model_root); setState(null); setJobId(null); }
      })}>{t('settings.migration.chooseStorage')}</Button>
      <Button icon={<ArrowRightLeft />} variant="primary" disabled={working || processing || recoveryRequired || !modelRoot || !selected.length}
        disabledReason={recoveryRequired ? t('settings.migration.errors.migration_restart_required') : !modelRoot ? t('settings.migration.chooseStorageFirst') : !selected.length ? t('settings.migration.selectSource') : undefined}
        onClick={() => void perform(async () => {
          const result = await profileMigrationRepository.prepare(selected);
          setJobId(result.job_id); setState({ status: 'queued', phase: 'preparing' });
        })}>{t('settings.migration.prepare')}</Button>
      <Button icon={<RefreshCw />} disabled={working || processing} onClick={() => void perform(discover)}>{t('settings.migration.refresh')}</Button>
    </ButtonGroup>
    {state && <MigrationProgress state={state} />}
    {error && <p role="alert">{t(`settings.migration.errors.${error.split(':')[0]}`, { defaultValue: error })}</p>}
    {jobId && <ButtonGroup label={t('settings.migration.actions')}>
      {state?.status === 'preview_ready' && <Button icon={<ArrowRightLeft />} variant="primary"
        disabled={working || !state.preview?.can_prepare}
        disabledReason={!state.preview?.can_prepare ? t('settings.migration.spaceInsufficient') : undefined}
        onClick={() => void perform(async () => {
          await profileMigrationRepository.confirmPrepare(jobId);
          setState({ status: 'queued', phase: 'preparing' });
        })}>{t('settings.migration.confirmPrepare')}</Button>}
      {activated && !processing && !recoveryRequired && <Button icon={<RefreshCw />} disabled={working}
        onClick={() => void perform(() => profileMigrationRepository.finalize(jobId))}>{t('settings.migration.finishModels')}</Button>}
      {state?.status === 'prepared' && <Button icon={<ArrowRightLeft />} variant="primary" busy={working} onClick={() => {
        if (window.confirm(t('settings.migration.activateConfirm'))) void perform(() => profileMigrationRepository.activate(jobId));
      }}>{t('settings.migration.activate')}</Button>}
      {processing && state?.status !== 'publishing' && <Button icon={<XCircle />} disabled={working} onClick={() => {
        if (window.confirm(t('settings.migration.cancelConfirm'))) void perform(() => profileMigrationRepository.cancel(jobId));
      }}>{t('settings.migration.cancel')}</Button>}
      {['failed', 'interrupted', 'awaiting_restart', 'rolled_back'].includes(state?.status || '') && <Button icon={<RotateCcw />} disabled={working}
        onClick={() => void perform(() => profileMigrationRepository.restart())}>{t('settings.migration.restart')}</Button>}
      {['failed', 'interrupted', 'completed', 'cleanup_pending'].includes(state?.status || '') && <Button icon={<RotateCcw />} disabled={working} onClick={() => {
        if (window.confirm(t('settings.migration.rollbackConfirm'))) void perform(() => profileMigrationRepository.rollback(jobId));
      }}>{t('settings.migration.rollback')}</Button>}
    </ButtonGroup>}
  </Card>;
}

export function ProfileMigrationBanner() {
  const { t } = useTranslation('common');
  const { currentPage, setCurrentPage } = useApp();
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void profileMigrationRepository.discover().then(value => {
      if (!cancelled) setVisible(value.recovery_required || (!value.latest_job_id && value.profiles.length > 0));
      if (!cancelled && value.recovery_required) setCurrentPage('settings');
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [setCurrentPage]);
  return visible && currentPage !== 'settings' ? <div className="app-update-banner" role="status"><span>{t('settings.migration.found')}</span>
    <Button onClick={() => setCurrentPage('settings')}>{t('settings.migration.open')}</Button></div> : null;
}
