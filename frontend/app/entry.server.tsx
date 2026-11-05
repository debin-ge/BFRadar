import { randomBytes } from "node:crypto";
import { PassThrough } from "node:stream";

import { createReadableStreamFromReadable } from "@react-router/node";
import { renderToPipeableStream } from "react-dom/server";
import type { EntryContext } from "react-router";
import { ServerRouter } from "react-router";

import { NonceContext } from "./lib/nonce";

export const streamTimeout = 5_000;

const PRODUCTION = process.env.NODE_ENV === "production";

/** CSP（D60）：React Router 的水合脚本是内联的，靠每个请求的 nonce 放行。开发模式 Vite 会注入内联样式，所以只在生产下发。 */
function setSecurityHeaders(headers: Headers, nonce: string) {
  if (PRODUCTION) {
    headers.set(
      "Content-Security-Policy",
      `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self' 'nonce-${nonce}'; ` +
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    );
  }
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
}

export default function handleRequest(
  request: Request,
  responseStatusCode: number,
  responseHeaders: Headers,
  routerContext: EntryContext,
) {
  // https://httpwg.org/specs/rfc9110.html#HEAD
  if (request.method.toUpperCase() === "HEAD") {
    return new Response(null, { status: responseStatusCode, headers: responseHeaders });
  }

  const nonce = randomBytes(16).toString("base64");
  return new Promise((resolve, reject) => {
    let shellRendered = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined = setTimeout(() => abort(), streamTimeout + 1000);

    // 一律等全部内容就绪再输出：Cloudflare 缓存与爬虫拿到的都是完整页面
    const { pipe, abort } = renderToPipeableStream(
      <NonceContext value={nonce}>
        <ServerRouter context={routerContext} url={request.url} nonce={nonce} />
      </NonceContext>,
      {
        nonce,
        onAllReady() {
          shellRendered = true;
          const body = new PassThrough({
            final(callback) {
              clearTimeout(timeoutId);
              timeoutId = undefined;
              callback();
            },
          });
          responseHeaders.set("Content-Type", "text/html; charset=utf-8");
          setSecurityHeaders(responseHeaders, nonce);
          pipe(body);
          resolve(
            new Response(createReadableStreamFromReadable(body), { headers: responseHeaders, status: responseStatusCode }),
          );
        },
        onShellError(error: unknown) {
          reject(error);
        },
        onError(error: unknown) {
          responseStatusCode = 500;
          // 外壳渲染时的错误由 handleDocumentRequest 记录，这里只记之后的
          if (shellRendered) {
            console.error(error);
          }
        },
      },
    );
  });
}
