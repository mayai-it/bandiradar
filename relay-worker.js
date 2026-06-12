/**
 * bandiradar relay — Cloudflare Worker
 *
 * Inoltra richieste GET verso una allowlist di host pubblici della PA italiana
 * che bloccano gli IP datacenter dei runner CI (es. incentivi.gov.it).
 * Usato SOLO dal monitor GitHub Actions di bandiradar; protetto da token.
 *
 * Uso:   GET https://<worker>/?u=<url-originale-urlencoded>
 *        header richiesto: X-Relay-Token: <RELAY_TOKEN>
 *
 * Setup su Cloudflare (dash.cloudflare.com):
 *   1. Workers & Pages → Create → Worker → incolla questo file → Deploy
 *   2. Worker → Settings → Variables and Secrets → Add:
 *        nome: RELAY_TOKEN   tipo: Secret   valore: (genera con `openssl rand -hex 24`)
 *   3. Copia l'URL del worker (https://<nome>.<account>.workers.dev)
 *
 * Poi su GitHub (repo bandiradar → Settings → Secrets → Actions):
 *   - BANDIRADAR_RELAY_URL   = URL del worker
 *   - BANDIRADAR_RELAY_TOKEN = lo stesso token
 */

// Host raggiungibili tramite il relay. Estendere SOLO con host pubblici
// necessari al monitor (mai endpoint privati).
const ALLOWED_HOSTS = [
  "www.incentivi.gov.it",
];

export default {
  async fetch(request, env) {
    // Solo GET: il relay legge dati pubblici, non scrive nulla.
    if (request.method !== "GET") {
      return new Response("method not allowed", { status: 405 });
    }

    // Token condiviso: senza, il worker non inoltra niente a nessuno.
    if (request.headers.get("X-Relay-Token") !== env.RELAY_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }

    const upstream = new URL(request.url).searchParams.get("u");
    if (!upstream) {
      return new Response("missing ?u=<url>", { status: 400 });
    }

    let target;
    try {
      target = new URL(upstream);
    } catch {
      return new Response("invalid url", { status: 400 });
    }

    if (target.protocol !== "https:" || !ALLOWED_HOSTS.includes(target.host)) {
      return new Response("host not allowed", { status: 403 });
    }

    const response = await fetch(target, {
      headers: {
        "User-Agent": "bandiradar-relay (+https://github.com/mayai-it/bandiradar)",
        "Accept": "application/json, text/csv, */*",
      },
      // Timeout difensivo: Cloudflare tronca comunque, ma evita hang lunghi.
      signal: AbortSignal.timeout(30_000),
    });

    // Risposta passata com'è (status + body + content-type).
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/octet-stream",
        "X-Relay-Upstream-Status": String(response.status),
      },
    });
  },
};
