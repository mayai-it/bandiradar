/**
 * bandiradar relay — Vercel serverless function, regione pinnata EU (fra1).
 *
 * Stesso contratto del worker Cloudflare:
 *   GET /api/relay?u=<url-originale-urlencoded>
 *   header richiesto: X-Relay-Token: <RELAY_TOKEN>
 *
 * Perché Vercel e non Cloudflare: i Worker CF girano sull'edge vicino al
 * CHIAMANTE (runner GitHub US → egress US → incentivi.gov.it geo-blocca → 522).
 * Qui la funzione gira SEMPRE a Francoforte (vercel.json "regions") → egress EU.
 */

const ALLOWED_HOSTS = [
  "www.incentivi.gov.it",
];

export default async function handler(req, res) {
  if (req.method !== "GET") {
    return res.status(405).send("method not allowed");
  }
  if (req.headers["x-relay-token"] !== process.env.RELAY_TOKEN) {
    return res.status(403).send("forbidden");
  }

  const upstream = req.query.u;
  if (!upstream) return res.status(400).send("missing ?u=<url>");

  let target;
  try {
    target = new URL(upstream);
  } catch {
    return res.status(400).send("invalid url");
  }
  if (target.protocol !== "https:" || !ALLOWED_HOSTS.includes(target.host)) {
    return res.status(403).send("host not allowed");
  }

  const response = await fetch(target, {
    headers: {
      "User-Agent": "bandiradar-relay (+https://github.com/mayai-it/bandiradar)",
      "Accept": "application/json, text/csv, */*",
    },
    signal: AbortSignal.timeout(30_000),
  });

  const body = Buffer.from(await response.arrayBuffer());
  res.setHeader(
    "Content-Type",
    response.headers.get("content-type") ?? "application/octet-stream",
  );
  res.setHeader("X-Relay-Upstream-Status", String(response.status));
  return res.status(response.status).send(body);
}
