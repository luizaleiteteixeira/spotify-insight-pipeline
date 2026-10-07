// Shared database access for the serverless API. Read-only role; DATABASE_URL is a Vercel env var.
const { neon } = require("@neondatabase/serverless");

const sql = neon(process.env.DATABASE_URL);

async function activeRun(req) {
  const asked = req.query && req.query.run;
  if (asked) return asked;
  const rows = await sql`SELECT run_id FROM active_run WHERE id = 1`;
  return rows.length ? rows[0].run_id : null;
}

function send(res, status, body) {
  res.setHeader("Content-Type", "application/json");
  res.setHeader("Cache-Control", "s-maxage=60, stale-while-revalidate=300");
  res.status(status).send(JSON.stringify(body));
}

module.exports = { sql, activeRun, send };
