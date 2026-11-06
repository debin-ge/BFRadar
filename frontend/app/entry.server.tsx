import { randomBytes } from "node:crypto";
import { Writable } from "node:stream";

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

/** 让 Cloudflare 的 Rocket Loader 放过页面里的内联脚本。Rocket Loader 会把内联脚本改写后重新插入页面，新插入的脚本没有
 *  nonce，被上面的 CSP 拦下：水合脚本全部失效，页面只剩没有交互的 HTML（主题按钮要等水合完成才显示，也就不见了）。
 *  data-cfasync="false" 是 Cloudflare 规定的"这个脚本别动"标记，必须写在每个脚本上。React Router 自己输出的
 *  数据脚本只接受 nonce 一个属性，没法从外面加，所以在整页渲染完后统一替换；nonce 每个请求都是新的随机值，
 *  页面内容里不会撞上这个字符串。 */
function exemptFromRocketLoader(html: string, nonce: string) {
  return html.replaceAll(`<script nonce="${nonce}"`, `<script data-cfasync="false" nonce="${nonce}"`);
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

    // 一律等全部内容就绪再输出：Cloudflare 缓存与爬虫拿到的都是完整页面（也因此整页在内存里改写脚本标记）
    const { pipe, abort } = renderToPipeableStream(
      <NonceContext value={nonce}>
        <ServerRouter context={routerContext} url={request.url} nonce={nonce} />
      </NonceContext>,
      {
        nonce,
        onAllReady() {
          shellRendered = true;
          const status = responseStatusCode;
          const chunks: Buffer[] = [];
          const body = new Writable({
            write(chunk: Buffer, _encoding, callback) {
              chunks.push(chunk);
              callback();
            },
            final(callback) {
              clearTimeout(timeoutId);
              timeoutId = undefined;
              responseHeaders.set("Content-Type", "text/html; charset=utf-8");
              setSecurityHeaders(responseHeaders, nonce);
              resolve(new Response(exemptFromRocketLoader(Buffer.concat(chunks).toString("utf8"), nonce), { headers: responseHeaders, status }));
              callback();
            },
          });
          pipe(body);
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
