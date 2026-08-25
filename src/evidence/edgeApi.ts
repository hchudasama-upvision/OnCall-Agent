import type { EdgeEnvironment } from "./edgeEnvironments.js";

/**
 * Real Edge UI/Controller JSON API calls, reverse-engineered from the actual
 * network traffic of the logged-in Edge UI SPA (captured against
 * processing.prod1001.aiware.run on 2026-08-21) — not guessed. All three
 * confirmed to accept the same static Bearer token already used for
 * /proc/tasks/stats/engines.
 */

export interface RawTaskRecord {
  internalTaskID: string;
  internalJobID: string;
  internalOrganizationID: string;
  engineID: string;
  engineName: string;
  failureReason: string;
  failureDetail?: string;
  createdDateTime: string;
  completedDateTime: string;
  modifiedDateTime: string;
}

interface TasksListResponse {
  count: number;
  limit: number;
  result: RawTaskRecord[];
}

interface OrganizationsResponse {
  result?: Array<{ organizationID: string; name?: string }>;
}

function authHeaders(env: EdgeEnvironment): Record<string, string> {
  return { "Content-Type": "application/json", Authorization: `Bearer ${env.token}` };
}

async function getJson<T>(url: string, env: EdgeEnvironment): Promise<T> {
  const res = await fetch(url, { headers: authHeaders(env) });
  if (!res.ok) {
    throw new Error(`Edge UI API call failed (${env.key}): ${res.status} ${res.statusText} — ${url}`);
  }
  return (await res.json()) as T;
}

export async function fetchTasksByStatus(
  env: EdgeEnvironment,
  opts: { startTimeEpochSeconds: number; endTimeEpochSeconds: number; status: string; limit?: number },
): Promise<RawTaskRecord[]> {
  const url =
    `${env.baseUrl}/proc/tasks?modifiedAfter=${opts.startTimeEpochSeconds}` +
    `&modifiedBefore=${opts.endTimeEpochSeconds}&status=${opts.status}&limit=${opts.limit ?? 100}`;
  const body = await getJson<TasksListResponse>(url, env);
  return body.result ?? [];
}

export async function fetchTaskDetail(env: EdgeEnvironment, taskId: string): Promise<RawTaskRecord> {
  return getJson<RawTaskRecord>(`${env.baseUrl}/proc/task/${taskId}/detail`, env);
}

/** Returns undefined if the org has no `name` populated (observed on some real orgs) — never fabricated. */
export async function fetchOrganizationName(env: EdgeEnvironment, organizationId: string): Promise<string | undefined> {
  const body = await getJson<OrganizationsResponse>(
    `${env.baseUrl}/admin/organizations?organizationID=${organizationId}`,
    env,
  );
  return body.result?.[0]?.name;
}
