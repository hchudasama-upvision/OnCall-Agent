import "dotenv/config";
import { loadEdgeEnvironments } from "../src/evidence/edgeEnvironments.js";
import { fetchEngineTaskStats, activeTaskTotal } from "../src/evidence/engineTaskStats.js";

/**
 * Read-only smoke test for the EDGE_* credentials in .env: queries the real
 * /proc/tasks/stats/engines endpoint (same one engine_task_stats.sh uses)
 * over the last 15 minutes for every configured environment and reports the
 * top engines by active task count.
 */
async function main() {
  const environments = loadEdgeEnvironments();
  const keys = Object.keys(environments);
  if (keys.length === 0) {
    console.error("No Edge environments configured in .env");
    process.exitCode = 1;
    return;
  }

  const endTime = Math.floor(Date.now() / 1000);
  const startTime = endTime - 15 * 60;

  for (const key of keys) {
    const env = environments[key];
    try {
      const engines = await fetchEngineTaskStats(env, startTime, endTime);
      const top = [...engines]
        .sort((a, b) => activeTaskTotal(b) - activeTaskTotal(a))
        .slice(0, 5);

      console.log(`\n[${key}] ${env.baseUrl} — ${engines.length} engines with activity`);
      for (const engine of top) {
        console.log(`  ${engine.engineName} (${engine.engineId}) -> ${JSON.stringify(engine.counts)}`);
      }
    } catch (err) {
      console.log(`\n[${key}] ${env.baseUrl} — FAILED: ${(err as Error).message}`);
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
