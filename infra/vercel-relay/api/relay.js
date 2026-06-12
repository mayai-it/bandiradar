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
  "www.euroinfosicilia.it",   // sicilia — started blocking GitHub runners
  "fesr.regione.campania.it", // blocks even the relay (500) — kept for evidence probes
  "www.sviluppocampania.it",  // campania source — probe-first, route if runners are blocked
  "www.regione.fvg.it",       // wave-2 recon: blocked direct from runners
  "bandi.regione.veneto.it",  // veneto SIU — started blocking GitHub runners
  "portalebandi.regione.basilicata.it", // basilicata — blocks runners, EU datacenter OK
  "pr2127.regione.puglia.it", // puglia — started blocking runners (timeout; 200 from EU)
  "www.regione.abruzzo.it",   // wave-2 recon: blocked direct from runners
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

  // Un upstream che rifiuta/scade NON è un errore del relay: senza try/catch
  // Vercel risponderebbe 500 FUNCTION_INVOCATION_FAILED e STATUS non potrebbe
  // distinguere "l'upstream blocca anche il relay" (es. puglia droppa i big
  // cloud, fra1 incluso) da "il relay è rotto". 502 + messaggio esplicito.
  let response;
  try {
    response = await fetch(target, {
      headers: {
        "User-Agent": "bandiradar-relay (+https://github.com/mayai-it/bandiradar)",
        "Accept": "application/json, text/csv, */*",
      },
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    const detail = err?.cause?.message ?? err?.message ?? String(err);
    return res
      .status(502)
      .send(`relay: upstream connect failed (${detail})`);
  }

  const body = Buffer.from(await response.arrayBuffer());
  res.setHeader(
    "Content-Type",
    response.headers.get("content-type") ?? "application/octet-stream",
  );
  res.setHeader("X-Relay-Upstream-Status", String(response.status));
  return res.status(response.status).send(body);
}
