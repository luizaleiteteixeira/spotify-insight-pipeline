// GET /api/examples?intent=praise&topic=other : deterministic sample of how the model labeled real reviews.
const { sql, activeRun, send } = require("./_db");
const INTENTS = ["praise", "complaint", "cancellation", "request", "unclear"];

module.exports = async (req, res) => {
  try {
    const run = await activeRun(req);
    const intent = INTENTS.includes(req.query.intent) ? req.query.intent : "complaint";
    const topic = req.query.topic || null;
    const rows = topic
      ? await sql`SELECT review_id, topic, subtopic, intent, severity, sentiment, needs_review, evidence_quote, review_rating
                  FROM records WHERE run_id = ${run} AND status = 'completed' AND intent = ${intent} AND topic = ${topic}
                    AND cache_source_id IS NULL AND length(evidence_quote) BETWEEN 25 AND 220
                  ORDER BY md5(review_id) LIMIT 6`
      : await sql`SELECT review_id, topic, subtopic, intent, severity, sentiment, needs_review, evidence_quote, review_rating
                  FROM records WHERE run_id = ${run} AND status = 'completed' AND intent = ${intent}
                    AND cache_source_id IS NULL AND length(evidence_quote) BETWEEN 25 AND 220
                  ORDER BY md5(review_id) LIMIT 6`;
    send(res, 200, { run_id: run, intent, topic, examples: rows });
  } catch (e) { send(res, 500, { error: String(e.message || e) }); }
};
