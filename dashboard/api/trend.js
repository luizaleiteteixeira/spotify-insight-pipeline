// GET /api/trend : monthly complaint/cancellation share by decision area, computed in SQL over stored records.
const { sql, activeRun, send } = require("./_db");

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    const rows = await sql`
      SELECT to_char(review_date, 'YYYY-MM') AS month,
             COUNT(*) FILTER (WHERE status = 'completed')::int AS completed,
             COUNT(*) FILTER (WHERE status = 'completed' AND intent IN ('complaint','cancellation') AND topic = 'access')::int AS access,
             COUNT(*) FILTER (WHERE status = 'completed' AND intent IN ('complaint','cancellation') AND topic = 'usability')::int AS usability,
             COUNT(*) FILTER (WHERE status = 'completed' AND intent IN ('complaint','cancellation') AND topic IN ('playback','downloads'))::int AS playback,
             COUNT(*) FILTER (WHERE status = 'completed' AND intent IN ('complaint','cancellation') AND topic IN ('billing','support'))::int AS billing_support,
             COUNT(*) FILTER (WHERE status = 'completed' AND issue_id = 'ISS-BILLING-PREMIUM_ONLY_CONTROLS')::int AS top_issue
      FROM records WHERE run_id = ${run} GROUP BY 1 ORDER BY 1`;
    send(res, 200, { run_id: run, partial_months: ["2022-05", "2023-11"], months: rows });
  } catch (e) { send(res, 500, { error: String(e.message || e) }); }
};
