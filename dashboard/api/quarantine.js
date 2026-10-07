// GET /api/quarantine : unresolved records by reason, with a few examples (never hidden).
const { sql, activeRun, send } = require("./_db");

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    const [reasons, examples] = await Promise.all([
      sql`SELECT reason, COUNT(*)::int AS n FROM records WHERE run_id = ${run} AND status = 'quarantined' GROUP BY reason ORDER BY n DESC`,
      sql`SELECT review_id, reason, review_date FROM records WHERE run_id = ${run} AND status = 'quarantined'
          ORDER BY md5(review_id) LIMIT 8`,
    ]);
    send(res, 200, { run_id: run, reasons, examples });
  } catch (e) { send(res, 500, { error: String(e.message || e) }); }
};
