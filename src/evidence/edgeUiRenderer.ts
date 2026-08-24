import type { EdgeUiTaskEvidence } from "./edgeUiClient.js";

const PAGE_STYLE = `
  body { margin: 0; font-family: -apple-system, Helvetica, Arial, sans-serif; background: #0f1117; color: #e6e8ef; padding: 24px; width: 900px; }
  h2 { font-size: 15px; font-weight: 600; margin: 0 0 16px; color: #9aa4b8; }
  .row { display: flex; gap: 16px; }
  .card { background: #171a23; border: 1px solid #2a2e3a; border-radius: 8px; padding: 16px; flex: 1; text-align: center; }
  .card .value { font-size: 32px; font-weight: 700; }
  .card .label { font-size: 12px; color: #9aa4b8; margin-top: 4px; }
  .completed .value { color: #4ade80; }
  .failed .value { color: #f87171; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 12px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2e3a; }
  th { color: #9aa4b8; font-weight: 500; }
  .badge { background: #3a1d1d; color: #f87171; border-radius: 4px; padding: 2px 8px; font-size: 11px; }
`;

export function renderTaskSummaryHtml(e: EdgeUiTaskEvidence): string {
  return `<!doctype html><html><head><style>${PAGE_STYLE}</style></head><body>
    <h2>${e.engineName} &middot; Processing Tasks &middot; last ${e.windowLabel}</h2>
    <div class="row">
      <div class="card completed"><div class="value">${e.completedPct}%</div><div class="label">${e.completedTasks} Completed tasks</div></div>
      <div class="card failed"><div class="value">${e.failedPct}%</div><div class="label">${e.failedTasks} Failed Tasks</div></div>
    </div>
  </body></html>`;
}

export function renderFailedTaskListHtml(e: EdgeUiTaskEvidence): string {
  const rows = Array.from({ length: Math.min(e.failedTasks, 10) }, (_, i) => `
    <tr>
      <td>task-${(i + 1).toString().padStart(3, "0")}</td>
      <td>${e.scopeOrgName}</td>
      <td>${e.engineName}</td>
      <td><span class="badge">${e.errorType}</span></td>
    </tr>`).join("");
  return `<!doctype html><html><head><style>${PAGE_STYLE}</style></head><body>
    <h2>Failed tasks &middot; organization: ${e.scopeOrgName} (${e.scopeOrgId})</h2>
    <table>
      <thead><tr><th>Job ID</th><th>Organization</th><th>Engine</th><th>Error Type</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </body></html>`;
}
