function loadState_() {
  const spreadsheet = coordinatorSpreadsheet_();
  const accountsSheet = spreadsheet.getSheetByName(ACCOUNTS_SHEET);
  const jobsSheet = spreadsheet.getSheetByName(JOBS_SHEET);
  if (!accountsSheet || !jobsSheet) {
    throw new Error('Coordinator sheets are missing. Run setupCoordinator() first.');
  }
  const state = {spreadsheet, accountsSheet, jobsSheet};
  reloadStateRows_(state);
  return state;
}

function reloadStateRows_(state) {
  reloadAccounts_(state);
  reloadJobs_(state);
}

function reloadAccounts_(state) {
  const data = readObjects_(state.accountsSheet, ACCOUNT_HEADERS);
  state.accountsById = {};
  data.forEach(obj => {
    obj.account_id = String(obj.account_id || '');
    if (obj.account_id) state.accountsById[obj.account_id] = obj;
  });
}

function reloadJobs_(state) {
  const data = readObjects_(state.jobsSheet, JOB_HEADERS);
  state.jobsById = {};
  data.forEach(obj => {
    obj.job_id = String(obj.job_id || '');
    obj.account_id = String(obj.account_id || '');
    if (obj.job_id) state.jobsById[obj.job_id] = obj;
  });
}

function readObjects_(sheet, headers) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const values = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
  return values.map((row, i) => {
    const obj = {_row: i + 2};
    headers.forEach((header, j) => obj[header] = normalizeCellValue_(row[j]));
    return obj;
  });
}

function updateAccount_(state, accountId, changes) {
  const row = state.accountsById[accountId];
  if (!row) throw codedError_('unknown_account', 'Unknown account_id: ' + accountId);
  writeChanges_(state.accountsSheet, row._row, ACCOUNT_HEADERS, changes);
  Object.assign(row, changes);
}

function updateJob_(state, jobId, changes) {
  const row = state.jobsById[jobId];
  if (!row) throw codedError_('job_not_found', 'Job not found: ' + jobId);
  writeChanges_(state.jobsSheet, row._row, JOB_HEADERS, changes);
  Object.assign(row, changes);
}

function writeChanges_(sheet, rowNumber, headers, changes) {
  Object.keys(changes).forEach(key => {
    const index = headers.indexOf(key);
    if (index >= 0) sheet.getRange(rowNumber, index + 1).setValue(changes[key]);
  });
}

function appendObject_(sheet, headers, obj) {
  sheet.appendRow(headers.map(header => obj[header] === undefined ? '' : obj[header]));
}

function ensureSheet_(spreadsheet, name, headers) {
  let sheet = spreadsheet.getSheetByName(name);
  if (!sheet) sheet = spreadsheet.insertSheet(name);
  if (sheet.getMaxColumns() < headers.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), headers.length - sheet.getMaxColumns());
  }
  const currentHeaders = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const mismatch = headers.some((header, i) => currentHeaders[i] !== header);
  if (mismatch) {
    if (sheet.getLastRow() > 1) {
      throw new Error('Sheet ' + name + ' already contains data with an incompatible header.');
    }
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold');
  }
  return sheet;
}

function seedAccount_(sheet, accountId, displayName) {
  const rows = readObjects_(sheet, ACCOUNT_HEADERS);
  if (rows.some(row => row.account_id === accountId)) return;
  const account = {
    account_id: accountId,
    display_name: displayName,
    state: 'IDLE',
    current_job_id: '', operator: '', host: '', job_type: '', started_at: '',
    heartbeat_at: '', lease_until: '', completed: '', total: '', progress_pct: '',
    eta_at: '', message: ''
  };
  appendObject_(sheet, ACCOUNT_HEADERS, account);
}

function coordinatorSpreadsheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('COORDINATOR_SPREADSHEET_ID');
  if (!id) throw new Error('COORDINATOR_SPREADSHEET_ID is missing. Run setupCoordinator() once.');
  return SpreadsheetApp.openById(id);
}

function authenticate_(providedToken) {
  const expected = PropertiesService.getScriptProperties().getProperty('COORDINATOR_TOKEN');
  if (!expected) throw new Error('COORDINATOR_TOKEN is not configured in Script Properties.');
  if (String(providedToken || '') !== String(expected)) {
    throw codedError_('unauthorized', 'Invalid coordinator token.');
  }
}

function leaseSeconds_() {
  const raw = PropertiesService.getScriptProperties().getProperty('LEASE_SECONDS');
  const value = Number(raw || 900);
  return Number.isFinite(value) && value >= 120 ? value : 900;
}

function queueTtlSeconds_() {
  const raw = PropertiesService.getScriptProperties().getProperty('QUEUE_TTL_SECONDS');
  const value = Number(raw || 300);
  return Number.isFinite(value) && value >= 120 ? value : 300;
}

function progressPct_(completed, total) {
  const c = Number(completed);
  const t = Number(total);
  if (!Number.isFinite(c) || !Number.isFinite(t) || t <= 0) return '';
  return Math.max(0, Math.min(100, Math.round((c / t) * 1000) / 10));
}

function parseDateMs_(value) {
  if (!value) return null;
  if (value instanceof Date) return value.getTime();
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? ms : null;
}

function normalizeCellValue_(value) {
  if (value instanceof Date) return value.toISOString();
  return value;
}

function numberOrBlank_(value) {
  if (value === null || value === undefined || value === '') return '';
  const num = Number(value);
  return Number.isFinite(num) ? num : '';
}

function stringValue_(value) {
  return value === null || value === undefined ? '' : String(value);
}

function requiredString_(value, name) {
  const text = stringValue_(value).trim();
  if (!text) throw codedError_('invalid_request', name + ' is required');
  return text;
}

function nowIso_() {
  return new Date().toISOString();
}

function copyKeys_(obj, keys) {
  const out = {};
  keys.forEach(key => out[key] = obj[key] === undefined ? '' : obj[key]);
  return out;
}

function codedError_(code, message) {
  const err = new Error(message);
  err.errorCode = code;
  return err;
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
