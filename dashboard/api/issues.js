// GET /api/issues            : full baseline ranking from the database
// GET /api/issues?id=ISS-... : one issue + its memo claims + top evidence reviews (from stored records)
const { sql, activeRun, send } = require("./_db");

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    if (!run) return send(res, 404, { error: "no active run" });
    const id = req.query && req.query.id;
    if (!id) {
      const rows = await sql`SELECT rank, issue_id, code, topic, area, title, summary, complaint_count, severity_sum,
                                    mean_severity::text AS mean_severity, priority_score
                             FROM issues WHERE run_id = ${run} ORDER BY rank`;
      return send(res, 200, { run_id: run, issues: rows });
    }
    const [issue, claims, evidence, mix] = await Promise.all([
      sql`SELECT rank, issue_id, code, topic, area, title, summary, complaint_count, severity_sum,
                 mean_severity::text AS mean_severity, priority_score FROM issues WHERE run_id = ${run} AND issue_id = ${id}`,
      sql`SELECT claim_id, metric, value FROM claims WHERE run_id = ${run} AND issue_id = ${id} ORDER BY claim_id`,
      sql`SELECT review_id, intent, severity, sentiment, evidence_quote, review_date, review_rating, review_likes
          FROM records WHERE run_id = ${run} AND issue_id = ${id} AND cache_source_id IS NULL
          ORDER BY severity DESC, review_likes DESC, review_id LIMIT 12`,
      sql`SELECT severity, intent, COUNT(*)::int AS n FROM records WHERE run_id = ${run} AND issue_id = ${id}
          GROUP BY severity, intent ORDER BY severity DESC, intent`,
    ]);
    if (!issue.length) return send(res, 404, { error: "unknown issue" });
    send(res, 200, { run_id: run, issue: issue[0], claims, evidence, mix });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
};
