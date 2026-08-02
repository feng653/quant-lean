import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const TRANSIENT_BROWSER_READ_PATTERNS = [
  /\bFailed to fetch\b/i,
  /\bNetworkError\b.*\bfetch\b/i,
  /\bLoad failed\b/i,
  /\bExecution context was destroyed\b.*\bnavigation\b/i,
  /\bCannot find context with specified id\b/i,
  /\bframe was detached\b/i,
  /\bnet::ERR_(?:CONNECTION_(?:RESET|REFUSED|CLOSED)|TIMED_OUT|ABORTED)\b/i,
];

const TRANSIENT_REPORT_PATTERNS = [
  ...TRANSIENT_BROWSER_READ_PATTERNS,
  /\bpage write outcome ambiguous after exactly one \w+ POST\b/i,
  /\bRead-only preflight\b.*\bHTTP (?:408|425|429|500|502|503|504)\b/i,
  /\bECONNRESET\b/i,
  /\bsocket hang up\b/i,
];

function errorMessage(error) {
  return typeof error?.message === 'string' ? error.message : String(error ?? '');
}

export function isTransientBrowserReadError(error) {
  const message = errorMessage(error);
  return TRANSIENT_BROWSER_READ_PATTERNS.some((pattern) => pattern.test(message));
}

export function isTransientRunnerReportError(report) {
  if (!report || report.status !== 'failed') return false;
  const message = errorMessage(report.error);
  return TRANSIENT_REPORT_PATTERNS.some((pattern) => pattern.test(message));
}

export async function classifyRunnerReport(path) {
  let report;
  try {
    report = JSON.parse(await readFile(path, 'utf8'));
  } catch {
    return 'invalid';
  }
  return isTransientRunnerReportError(report) ? 'transient' : 'non-transient';
}

const invokedPath = process.argv[1]
  ? pathToFileURL(resolve(process.argv[1])).href
  : null;
if (invokedPath === import.meta.url) {
  const reportPath = process.argv[2];
  if (!reportPath) {
    process.stderr.write('usage: transient_failures.mjs <report.json>\n');
    process.exitCode = 64;
  } else {
    const classification = await classifyRunnerReport(reportPath);
    process.stdout.write(`${classification}\n`);
    process.exitCode = classification === 'transient' ? 0 : 2;
  }
}
