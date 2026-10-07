// GET /api/review?id=<review_id> : trace one review (labels, cache provenance, issue membership, rank).
const { sql, activeRun, send } = require("./_db");

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    const id = req.query && req.query.id;
    if (!run || !id) return send(res, 400, { error: "need ?id=" });
    const rows = await sql`SELECT r.review_id, r.status, r.reason, r.topic, r.subtopic, r.intent, r.severity, r.sentiment,
                                  r.needs_review, r.evidence_quote, r.review_date, r.review_rating, r.app_version,
                                  r.cache_source_id, r.issue_id, i.rank AS issue_rank, i.title AS issue_title
                           FROM records r LEFT JOIN issues i ON i.run_id = r.run_id AND i.issue_id = r.issue_id
                           WHERE r.run_id = ${run} AND r.review_id = ${id}`;
    if (!rows.length) return send(res, 404, { error: "unknown review_id" });
    send(res, 200, { run_id: run, review: rows[0] });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
};
