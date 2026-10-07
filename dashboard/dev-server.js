// Local preview only: serves public/ and the api/*.js handlers like Vercel does. Reads DASHBOARD_DATABASE_URL from ../.env.
const http = require("http"), fs = require("fs"), path = require("path"), url = require("url");
for (const l of fs.readFileSync(path.join(__dirname, "..", ".env"), "utf8").split("\n")) {
  const m = l.match(/^DASHBOARD_DATABASE_URL=(.*)$/); if (m) process.env.DATABASE_URL = m[1].trim();
}
const port = Number(process.env.PORT || 3017);
http.createServer(async (req, res) => {
  const u = url.parse(req.url, true);
  if (u.pathname.startsWith("/api/")) {
    const f = path.join(__dirname, "api", u.pathname.slice(5).replace(/\.js$/, "") + ".js");
    if (!fs.existsSync(f)) { res.statusCode = 404; return res.end("not found"); }
    req.query = u.query;
    res.status = c => { res.statusCode = c; return res; };
    res.send = b => res.end(b);
    return require(f)(req, res);
  }
  const file = path.join(__dirname, "public", u.pathname === "/" ? "index.html" : u.pathname);
  if (!file.startsWith(path.join(__dirname, "public")) || !fs.existsSync(file)) { res.statusCode = 404; return res.end("not found"); }
  res.setHeader("Content-Type", file.endsWith(".html") ? "text/html; charset=utf-8" : "application/octet-stream");
  fs.createReadStream(file).pipe(res);
}).listen(port, () => console.log(`dashboard dev server on http://localhost:${port}`));
