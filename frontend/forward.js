// 后端的公开地址经前端原样转给后端（D60）：接口、RSS、sitemap、robots、分享图、/healthz。
// 对外只有前端这一个入口（Cloudflare Tunnel 只接到前端），后端不开放端口；内部页面数据 /internal/* 不在其中，对外是页面 404。
// 判断用规范化之后的路径（/api/../internal、/api/%2e%2e/internal 都按 /internal 算），目标主机固定为后端，转不到别处。
import http from "node:http";

export const BACKEND_PATHS = /^\/(?:api|og)\/|^\/(?:en\/)?feed\.xml$|^\/(?:healthz|sitemap\.xml|robots\.txt)$/;

// 逐跳头只管这一跳，不往下传（RFC 9110 第 7.6.1 节）
const HOP_BY_HOP = ["connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade"];
const TIMEOUT_MS = 30_000;

/** @param {http.IncomingHttpHeaders} headers */
function endToEnd(headers) {
  const out = { ...headers };
  for (const name of HOP_BY_HOP) delete out[name];
  return out;
}

/**
 * @param {string} origin 后端地址（API_ORIGIN）
 * @returns {import("express").RequestHandler}
 */
export function forwardToBackend(origin) {
  const base = new URL(origin);
  return (req, res, next) => {
    let target;
    try {
      target = new URL(base.origin + req.originalUrl);
    } catch {
      return next();
    }
    if (!req.originalUrl.startsWith("/") || target.origin !== base.origin || !BACKEND_PATHS.test(target.pathname)) {
      return next();
    }
    const headers = endToEnd(req.headers);
    headers["x-forwarded-for"] = [req.headers["x-forwarded-for"], req.socket.remoteAddress].filter(Boolean).join(", ");
    headers["x-forwarded-proto"] ??= req.protocol;
    const upstream = http.request(target, { method: req.method, headers }, (response) => {
      res.writeHead(response.statusCode ?? 502, endToEnd(response.headers));
      response.pipe(res);
    });
    upstream.setTimeout(TIMEOUT_MS, () => upstream.destroy(new Error(`${TIMEOUT_MS / 1000} 秒没有响应`)));
    // 访客中途断开就不再等后端，也不记成后端的错
    let abandoned = false;
    res.on("close", () => {
      if (res.writableFinished) return;
      abandoned = true;
      upstream.destroy();
    });
    upstream.on("error", (error) => {
      if (abandoned) return;
      console.error(`后端不可用：${req.method} ${target.pathname}（${error.message}）`);
      if (res.headersSent) return res.destroy();
      res.status(502).set("Cache-Control", "no-store").type("text/plain").send("Bad Gateway");
    });
    req.pipe(upstream);
  };
}
