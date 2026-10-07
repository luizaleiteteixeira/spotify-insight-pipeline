// GET /api/summary : overall metrics (aggregated in SQL from stored records) + AI recommendation + run evidence.
const { sql, activeRun, send } = require("./_db");

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    if (!run) return send(res, 404, { error: "no active run" });
    const [status, topics, intents, severity, meta, rec, areas] = await Promise.all([
      sql`SELECT status, COUNT(*)::int AS n FROM records WHERE run_id = ${run} GROUP BY status ORDER BY n DESC`,
      sql`SELECT topic, COUNT(*)::int AS n FROM records WHERE run_id = ${run} AND status = 'completed'
          GROUP BY topic ORDER BY n DESC`,
      sql`SELECT intent, COUNT(*)::int AS n FROM records WHERE run_id = ${run} AND status = 'completed'
          GROUP BY intent ORDER BY n DESC`,
      sql`SELECT severity, COUNT(*)::int AS n FROM records WHERE run_id = ${run} AND status = 'completed'
          GROUP BY severity ORDER BY severity`,
      sql`SELECT verification, golden, cost, provenance, overview->'areas' AS areas, overview->'cache_reuse_rows' AS cache_reuse,
                 loaded_at FROM run_meta WHERE run_id = ${run}`,
      sql`SELECT title, recommendation, alternatives, check_result, model, prompt_version, generated_at
          FROM recommendation WHERE run_id = ${run}`,
      sql`SELECT i.area, SUM(i.complaint_count)::int AS complaint_count, SUM(i.severity_sum)::int AS severity_sum
          FROM issues i WHERE i.run_id = ${run} GROUP BY i.area ORDER BY severity_sum DESC`,
    ]);
    send(res, 200, { run_id: run, status, topics, intents, severity, areas, meta: meta[0] || null,
                     recommendation: rec[0] || null });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
};
