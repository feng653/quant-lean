/**
 * Industry catalog parsing and client-side validation.
 *
 * Background: a production bug served industry entries whose human-readable
 * `name` was actually the East Money board code (e.g. { code: 'BK0477',
 * name: 'BK0477' }). Rendering or submitting those "names" is meaningless
 * and silently breaks universe filtering. This module is the fail-closed
 * client gate:
 *
 * - BK-style codes are never accepted as industry names;
 * - malformed entries are excluded from the selectable set and counted;
 * - only an explicit pool-scoped v2 readiness result may become selectable;
 * - selection helpers decide whether an experiment may be submitted.
 */

export interface IndustryEntry {
  code: string;
  name: string;
}

export interface IndustryCatalogMeta {
  classification: string;
  schemaVersion: string | null;
  source: string | null;
  reason: string | null;
  filterable: boolean;
  declaredCount: number | null;
  mappedStocks: number | null;
  requestedStocks: number | null;
  requestedMappedStocks: number | null;
  mapCoverage: number | null;
  coverageScope: string | null;
  minimumCoverage: number | null;
}

export type IndustryCatalogState =
  | {
      status: 'ready';
      entries: IndustryEntry[];
      /** Number of raw entries rejected by name validation. */
      invalidCount: number;
      meta: IndustryCatalogMeta;
    }
  | {
      status: 'unavailable';
      reason: string;
      invalidCount: number;
      meta: IndustryCatalogMeta;
    };

const BK_CODE_PATTERN = /^bk\d{3,}$/i;
const NUMERIC_CODE_PATTERN = /^\d{4,}$/;
const MAX_NAME_LENGTH = 64;

/**
 * An industry name is human-readable only when it is a non-empty label that
 * is not itself a board code. BK-prefixed codes and bare numeric codes are
 * never human-readable.
 */
export function isHumanReadableIndustryName(name: string, code: string): boolean {
  const trimmed = name.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_NAME_LENGTH) return false;
  if (trimmed === code.trim()) return false;
  if (BK_CODE_PATTERN.test(trimmed)) return false;
  if (NUMERIC_CODE_PATTERN.test(trimmed)) return false;
  return true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function parseMeta(payload: Record<string, unknown>, filterable: boolean): IndustryCatalogMeta {
  return {
    classification: optionalString(payload.classification) ?? 'unknown',
    schemaVersion: optionalString(payload.schema_version),
    source: optionalString(payload.source),
    reason: optionalString(payload.reason),
    filterable,
    declaredCount: optionalNumber(payload.count),
    mappedStocks: optionalNumber(payload.mapped_stocks),
    requestedStocks: optionalNumber(payload.requested_stocks),
    requestedMappedStocks: optionalNumber(payload.requested_mapped_stocks),
    mapCoverage: optionalNumber(payload.map_coverage),
    coverageScope: optionalString(payload.coverage_scope),
    minimumCoverage: optionalNumber(payload.minimum_coverage),
  };
}

/**
 * Parse any backend payload (v1 legacy or industry-catalog/v2) into a
 * fail-closed catalog state. Never throws: any structural problem degrades
 * to `unavailable` with an explicit reason.
 */
export function parseIndustryCatalog(payload: unknown): IndustryCatalogState {
  if (!isRecord(payload)) {
    return {
      status: 'unavailable',
      reason: '行业目录响应格式无效，无法用于筛选。',
      invalidCount: 0,
      meta: parseMeta({}, false),
    };
  }

  const declaredFilterable = payload.filterable === true;
  const meta = parseMeta(payload, declaredFilterable);

  if (meta.schemaVersion !== 'industry-catalog/v2') {
    return {
      status: 'unavailable',
      reason: '行业目录缺少 v2 安全契约，无法确认股票池级筛选条件。',
      invalidCount: 0,
      meta,
    };
  }

  if (payload.filterable !== true) {
    return {
      status: 'unavailable',
      reason: meta.reason ?? '服务端标记行业目录当前不可用于筛选。',
      invalidCount: 0,
      meta,
    };
  }

  const readinessIsPoolScoped =
    meta.coverageScope === 'requested_codes' &&
    meta.requestedStocks !== null &&
    meta.requestedStocks > 0 &&
    meta.requestedMappedStocks !== null &&
    meta.requestedMappedStocks >= 0 &&
    meta.requestedMappedStocks <= meta.requestedStocks &&
    meta.mapCoverage !== null &&
    meta.mapCoverage >= 0 &&
    meta.mapCoverage <= 1 &&
    meta.minimumCoverage !== null &&
    meta.minimumCoverage >= 0 &&
    meta.minimumCoverage <= 1 &&
    Math.abs(
      meta.mapCoverage -
        meta.requestedMappedStocks / meta.requestedStocks,
    ) < 1e-9 &&
    meta.mapCoverage >= meta.minimumCoverage;

  if (!readinessIsPoolScoped) {
    return {
      status: 'unavailable',
      reason:
        meta.reason ??
        '行业目录缺少完整且达标的股票池覆盖证据，已按不可筛选处理。',
      invalidCount: 0,
      meta,
    };
  }

  const rawIndustries = Array.isArray(payload.industries) ? payload.industries : [];
  const seenNames = new Set<string>();
  const entries: IndustryEntry[] = [];
  let invalidCount = 0;

  for (const raw of rawIndustries) {
    if (!isRecord(raw)) {
      invalidCount += 1;
      continue;
    }
    const code = optionalString(raw.code);
    const name = optionalString(raw.name);
    if (!code || !name || !isHumanReadableIndustryName(name, code)) {
      invalidCount += 1;
      continue;
    }
    const normalizedName = name.trim();
    if (seenNames.has(normalizedName)) continue;
    seenNames.add(normalizedName);
    entries.push({ code: code.trim(), name: normalizedName });
  }

  if (entries.length === 0) {
    const reason =
      meta.reason ??
      (invalidCount > 0
        ? `行业目录返回了 ${invalidCount} 条无法校验的条目（行业名称不可读），已按不可用处理。`
        : '行业目录为空，当前无法按行业筛选。');
    return { status: 'unavailable', reason, invalidCount, meta };
  }

  return { status: 'ready', entries, invalidCount, meta };
}

export interface IndustrySelectionPartition {
  /** Selected names confirmed present in the validated catalog. */
  valid: string[];
  /**
   * Selected names that cannot be confirmed — inherited from a source
   * experiment/preset, or entered while the catalog was unavailable.
   * These must be explicitly cleared; they are never silently submitted.
   */
  invalid: string[];
}

export function partitionIndustrySelection(
  selected: string[],
  catalog: IndustryCatalogState | null,
): IndustrySelectionPartition {
  if (!catalog || catalog.status !== 'ready') {
    return { valid: [], invalid: [...selected] };
  }
  const validNames = new Set(catalog.entries.map((entry) => entry.name));
  const valid: string[] = [];
  const invalid: string[] = [];
  for (const name of selected) {
    (validNames.has(name) ? valid : invalid).push(name);
  }
  return { valid, invalid };
}

export interface IndustrySubmitGuard {
  ok: boolean;
  reason: string | null;
}

/**
 * Fail-closed submit guard for experiment creation. An empty selection is
 * allowed (it means "all industries"); any non-empty selection must be
 * fully validated against a ready catalog.
 */
export function canSubmitIndustries(
  selected: string[],
  catalog: IndustryCatalogState | null,
): IndustrySubmitGuard {
  if (selected.length === 0) {
    return { ok: true, reason: null };
  }
  if (!catalog || catalog.status !== 'ready') {
    return {
      ok: false,
      reason: '行业目录不可用，已选择的行业无法校验。请清除行业选择或等待目录恢复后再提交。',
    };
  }
  const { invalid } = partitionIndustrySelection(selected, catalog);
  if (invalid.length > 0) {
    return {
      ok: false,
      reason: `存在 ${invalid.length} 个无法校验的行业选择（${invalid.join('、')}）。请显式清除后再提交。`,
    };
  }
  return { ok: true, reason: null };
}

const CLASSIFICATION_LABELS: Record<string, string> = {
  eastmoney: '东方财富行业',
  cninfo_008001: '巨潮 008001 行业',
  shenwan_l1: '申万一级行业',
  citic: '中信证券行业',
  gics: 'GICS 行业',
  unknown: '未知分类',
};

export function industryClassificationLabel(classification: string): string {
  return CLASSIFICATION_LABELS[classification] ?? classification;
}
