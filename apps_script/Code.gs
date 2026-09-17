/**
 * SeaSearcher shared scrape coordinator.
 *
 * Bind this script to a Google Sheet, run setupCoordinator() once, then deploy
 * it as a Web App. Store the shared API token in Script Properties as
 * COORDINATOR_TOKEN. The Python client never stores SeaSearcher passwords here.
 */

const ACCOUNTS_SHEET = 'Accounts';
const JOBS_SHEET = 'Jobs';
const ACCOUNT_HEADERS = [
  'account_id', 'display_name', 'state', 'current_job_id', 'operator', 'host',
  'job_type', 'started_at', 'heartbeat_at', 'lease_until', 'completed', 'total',
  'progress_pct', 'eta_at', 'message'
];
const JOB_HEADERS = [
  'job_id', 'account_id', 'operator', 'host', 'pid', 'job_type', 'status',
  'created_at', 'last_seen_at', 'started_at', 'finished_at', 'completed', 'total',
  'progress_pct', 'eta_at', 'queue_position', 'message', 'error'
];

function setupCoordinator() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) {
    throw new Error('Run setupCoordinator() from a script bound to a Google Sheet.');
  }
  PropertiesService.getScriptProperties().setProperty(
    'COORDINATOR_SPREADSHEET_ID', spreadsheet.getId()
  );

  const accounts = ensureSheet_(spreadsheet, ACCOUNTS_SHEET, ACCOUNT_HEADERS);
  const jobs = ensureSheet_(spreadsheet, JOBS_SHEET, JOB_HEADERS);

  seedAccount_(accounts, 'account_1', 'SeaSearcher Account 1');
  seedAccount_(accounts, 'account_2', 'SeaSearcher Account 2');

  accounts.setFrozenRows(1);
  jobs.setFrozenRows(1);
  if (!accounts.getFilter()) accounts.getRange(1, 1, accounts.getLastRow(), ACCOUNT_HEADERS.length).createFilter();
  if (!jobs.getFilter()) jobs.getRange(1, 1, Math.max(2, jobs.getLastRow()), JOB_HEADERS.length).createFilter();

  accounts.autoResizeColumns(1, ACCOUNT_HEADERS.length);
  jobs.autoResizeColumns(1, JOB_HEADERS.length);

  const stateColumn = ACCOUNT_HEADERS.indexOf('state') + 1;
  const stateRange = accounts.getRange(2, stateColumn, Math.max(1, accounts.getMaxRows() - 1), 1);
  const rules = [
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('RUNNING')
      .setBackground('#d9ead3')
      .setRanges([stateRange])
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('IDLE')
      .setBackground('#eeeeee')
      .setRanges([stateRange])
      .build()
  ];
  accounts.setConditionalFormatRules(rules);

  return {
    spreadsheet_id: spreadsheet.getId(),
    accounts_sheet: ACCOUNTS_SHEET,
    jobs_sheet: JOBS_SHEET,
    message: 'Setup complete. Add COORDINATOR_TOKEN in Apps Script > Project Settings > Script Properties, then deploy as a Web App.'
  };
}

function doGet() {
  return jsonResponse_({ok: true, service: 'seasearcher-scrape-coordinator'});
}

function doPost(e) {
  let payload;
  try {
    payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return jsonResponse_({ok: false, error_code: 'invalid_json', error: String(err)});
  }

  try {
    authenticate_(payload.token);
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) {
      throw new Error('Coordinator is busy. Retry shortly.');
    }
    try {
      const action = String(payload.action || '').trim().toLowerCase();
      let result;
      switch (action) {
        case 'reserve': result = reserve_(payload); break;
        case 'poll': result = poll_(payload); break;
        case 'heartbeat': result = heartbeat_(payload); break;
        case 'release': result = release_(payload); break;
        case 'status': result = status_(payload); break;
        default:
          return jsonResponse_({ok: false, error_code: 'unknown_action', error: 'Unknown action: ' + action});
      }
      return jsonResponse_({ok: true, ...result});
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return jsonResponse_({
      ok: false,
      error_code: err && err.errorCode ? err.errorCode : 'coordinator_error',
      error: String(err && err.message ? err.message : err)
    });
  }
}

function reserve_(payload) {
  const state = loadState_();
  cleanupStale_(state);

  const jobId = requiredString_(payload.job_id, 'job_id');
  const accountId = requiredString_(payload.account_id, 'account_id');
  const account = state.accountsById[accountId];
  if (!account) throw codedError_('unknown_account', 'Unknown account_id: ' + accountId);

  let job = state.jobsById[jobId];
  if (!job) {
    const now = nowIso_();
    job = {
      job_id: jobId,
      account_id: accountId,
      operator: stringValue_(payload.operator),
      host: stringValue_(payload.host),
      pid: numberOrBlank_(payload.pid),
      job_type: stringValue_(payload.job_type),
      status: 'QUEUED',
      created_at: now,
      last_seen_at: now,
      started_at: '',
      finished_at: '',
      completed: 0,
      total: numberOrBlank_(payload.total),
      progress_pct: '',
      eta_at: '',
      queue_position: '',
      message: stringValue_(payload.message),
      error: ''
    };
    appendObject_(state.jobsSheet, JOB_HEADERS, job);
    reloadJobs_(state);
    job = state.jobsById[jobId];
  } else if (job.account_id !== accountId) {
    throw codedError_('job_conflict', 'Existing job_id belongs to a different account.');
  }

  touchJob_(state, jobId);
  promoteNext_(state, accountId);
  refreshQueuePositions_(state, accountId);
  SpreadsheetApp.flush();
  reloadStateRows_(state);
  return jobResponse_(state, jobId);
}

function poll_(payload) {
  const state = loadState_();
  cleanupStale_(state);
  const jobId = requiredString_(payload.job_id, 'job_id');
  const job = state.jobsById[jobId];
  if (!job) throw codedError_('job_not_found', 'Job not found: ' + jobId);

  touchJob_(state, jobId);
  promoteNext_(state, job.account_id);
  refreshQueuePositions_(state, job.account_id);
  SpreadsheetApp.flush();
  reloadStateRows_(state);
  return jobResponse_(state, jobId);
}

function heartbeat_(payload) {
  const state = loadState_();
  cleanupStale_(state);
  const jobId = requiredString_(payload.job_id, 'job_id');
  const job = state.jobsById[jobId];
  if (!job) throw codedError_('job_not_found', 'Job not found: ' + jobId);
  if (String(job.status).toUpperCase() !== 'RUNNING') {
    throw codedError_('job_not_running', 'Job is not running: ' + jobId);
  }

  const account = state.accountsById[job.account_id];
  if (!account || account.current_job_id !== jobId) {
    throw codedError_('lease_lost', 'This job no longer owns account ' + job.account_id);
  }

  const now = nowIso_();
  const leaseUntil = new Date(Date.now() + leaseSeconds_() * 1000).toISOString();
  const completed = payload.completed === null || payload.completed === undefined ? job.completed : numberOrBlank_(payload.completed);
  const total = payload.total === null || payload.total === undefined ? job.total : numberOrBlank_(payload.total);
  const progress = progressPct_(completed, total);

  updateJob_(state, jobId, {
    last_seen_at: now,
    completed: completed,
    total: total,
    progress_pct: progress,
    eta_at: stringValue_(payload.eta_at),
    message: stringValue_(payload.message)
  });
  updateAccount_(state, job.account_id, {
    state: 'RUNNING',
    heartbeat_at: now,
    lease_until: leaseUntil,
    completed: completed,
    total: total,
    progress_pct: progress,
    eta_at: stringValue_(payload.eta_at),
    message: stringValue_(payload.message)
  });

  SpreadsheetApp.flush();
  reloadStateRows_(state);
  return jobResponse_(state, jobId);
}

function release_(payload) {
  const state = loadState_();
  cleanupStale_(state);
  const jobId = requiredString_(payload.job_id, 'job_id');
  const job = state.jobsById[jobId];
  if (!job) throw codedError_('job_not_found', 'Job not found: ' + jobId);

  const requestedStatus = String(payload.status || 'COMPLETED').toUpperCase();
  const allowed = ['COMPLETED', 'FAILED', 'CANCELLED'];
  const finalStatus = allowed.indexOf(requestedStatus) >= 0 ? requestedStatus : 'COMPLETED';
  const now = nowIso_();
  const completed = payload.completed === null || payload.completed === undefined ? job.completed : numberOrBlank_(payload.completed);
  const total = payload.total === null || payload.total === undefined ? job.total : numberOrBlank_(payload.total);

  updateJob_(state, jobId, {
    status: finalStatus,
    last_seen_at: now,
    finished_at: now,
    completed: completed,
    total: total,
    progress_pct: progressPct_(completed, total),
    eta_at: stringValue_(payload.eta_at),
    message: stringValue_(payload.message),
    error: stringValue_(payload.error),
    queue_position: ''
  });

  const account = state.accountsById[job.account_id];
  if (account && account.current_job_id === jobId) {
    clearAccount_(state, job.account_id);
  }

  reloadStateRows_(state);
  promoteNext_(state, job.account_id);
  refreshQueuePositions_(state, job.account_id);
  SpreadsheetApp.flush();
  reloadStateRows_(state);
  return jobResponse_(state, jobId);
}

function status_() {
  const state = loadState_();
  cleanupStale_(state);
  Object.keys(state.accountsById).forEach(accountId => {
    promoteNext_(state, accountId);
    refreshQueuePositions_(state, accountId);
  });
  SpreadsheetApp.flush();
  reloadStateRows_(state);

  const accounts = Object.values(state.accountsById).map(publicAccount_);
  const queued = Object.values(state.jobsById)
    .filter(job => String(job.status).toUpperCase() === 'QUEUED')
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .map(publicJob_);
  return {accounts: accounts, queued_jobs: queued};
}

function cleanupStale_(state) {
  const nowMs = Date.now();
  const queueCutoff = nowMs - queueTtlSeconds_() * 1000;

  Object.values(state.accountsById).forEach(account => {
    if (String(account.state).toUpperCase() !== 'RUNNING' || !account.current_job_id) return;
    const leaseMs = parseDateMs_(account.lease_until);
    if (leaseMs !== null && leaseMs < nowMs) {
      const jobId = account.current_job_id;
      if (state.jobsById[jobId] && String(state.jobsById[jobId].status).toUpperCase() === 'RUNNING') {
        updateJob_(state, jobId, {
          status: 'EXPIRED',
          finished_at: nowIso_(),
          error: 'Heartbeat lease expired',
          queue_position: ''
        });
      }
      clearAccount_(state, account.account_id);
    }
  });

  Object.values(state.jobsById).forEach(job => {
    if (String(job.status).toUpperCase() !== 'QUEUED') return;
    const seenMs = parseDateMs_(job.last_seen_at || job.created_at);
    if (seenMs !== null && seenMs < queueCutoff) {
      updateJob_(state, job.job_id, {
        status: 'ABANDONED',
        finished_at: nowIso_(),
        error: 'Queue polling stopped',
        queue_position: ''
      });
    }
  });

  reloadStateRows_(state);
}

function promoteNext_(state, accountId) {
  const account = state.accountsById[accountId];
  if (!account) return;
  if (String(account.state).toUpperCase() === 'RUNNING' && account.current_job_id) return;

  const queued = Object.values(state.jobsById)
    .filter(job => job.account_id === accountId && String(job.status).toUpperCase() === 'QUEUED')
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  if (!queued.length) {
    clearAccount_(state, accountId);
    return;
  }

  const job = queued[0];
  const now = nowIso_();
  const leaseUntil = new Date(Date.now() + leaseSeconds_() * 1000).toISOString();
  updateJob_(state, job.job_id, {
    status: 'RUNNING',
    last_seen_at: now,
    started_at: job.started_at || now,
    queue_position: ''
  });
  updateAccount_(state, accountId, {
    state: 'RUNNING',
    current_job_id: job.job_id,
    operator: job.operator,
    host: job.host,
    job_type: job.job_type,
    started_at: job.started_at || now,
    heartbeat_at: now,
    lease_until: leaseUntil,
    completed: job.completed || 0,
    total: job.total,
    progress_pct: job.progress_pct,
    eta_at: job.eta_at,
    message: job.message
  });
  reloadStateRows_(state);
}

function refreshQueuePositions_(state, accountId) {
  const queued = Object.values(state.jobsById)
    .filter(job => job.account_id === accountId && String(job.status).toUpperCase() === 'QUEUED')
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  queued.forEach((job, index) => updateJob_(state, job.job_id, {queue_position: index + 1}));
  reloadJobs_(state);
}

function touchJob_(state, jobId) {
  updateJob_(state, jobId, {last_seen_at: nowIso_()});
  reloadJobs_(state);
}

function clearAccount_(state, accountId) {
  updateAccount_(state, accountId, {
    state: 'IDLE', current_job_id: '', operator: '', host: '', job_type: '',
    started_at: '', heartbeat_at: '', lease_until: '', completed: '', total: '',
    progress_pct: '', eta_at: '', message: ''
  });
  reloadAccounts_(state);
}

function jobResponse_(state, jobId) {
  const job = state.jobsById[jobId];
  if (!job) throw codedError_('job_not_found', 'Job not found: ' + jobId);
  const account = state.accountsById[job.account_id] || null;
  let currentJob = null;
  if (account && account.current_job_id && state.jobsById[account.current_job_id]) {
    currentJob = publicJob_(state.jobsById[account.current_job_id]);
  }
  return {
    job: publicJob_(job),
    account: account ? publicAccount_(account) : null,
    current_job: currentJob,
    queue_position: job.queue_position === '' ? null : Number(job.queue_position)
  };
}

function publicAccount_(account) {
  return copyKeys_(account, ACCOUNT_HEADERS);
}

function publicJob_(job) {
  return copyKeys_(job, JOB_HEADERS);
}
