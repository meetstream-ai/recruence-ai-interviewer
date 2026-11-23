// Serves the demo company site: node examples/demo-company-site/serve.js [port]  (npm run demo:site)
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const root = __dirname;
const port = Number(process.argv[2] || process.env.PORT || 8300);
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };

http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
  const file = path.join(root, name);
  if (!file.startsWith(root + path.sep) || !types[path.extname(file)] || !fs.existsSync(file)) {
    res.writeHead(404, { "Content-Type": "text/plain" }); res.end("Not found"); return;
  }
  res.writeHead(200, { "Content-Type": types[path.extname(file)], "Cache-Control": "no-cache" });
  fs.createReadStream(file).pipe(res);
}).listen(port, "127.0.0.1", () => console.log(`Demo company site (test only): http://127.0.0.1:${port}`));
