// 前端服务：Express 5 + React Router SSR，也是站点唯一的入口：后端的公开地址原样转给后端（forward.js）。
// 开发时挂 Vite 中间件，生产时提供构建产物。压缩在 Cloudflare 边缘做；PORT 为 0 时随机分配端口（测试用），
// 启动后打印实际地址；SIGTERM 时关掉连接再退出。
import express from "express";

import { archiveAssets } from "./assets-archive.js";
import { forwardToBackend } from "./forward.js";

const BUILD_PATH = "./build/server/index.js";
const DEVELOPMENT = process.env.NODE_ENV === "development";
const PORT = Number.parseInt(process.env.PORT ?? "3000", 10);
const ARCHIVE = process.env.ASSETS_ARCHIVE;
const API_ORIGIN = process.env.API_ORIGIN ?? "http://127.0.0.1:8000"; // 与 app/lib/api.server.ts 相同

const app = express();
app.disable("x-powered-by");
app.set("trust proxy", "loopback, linklocal, uniquelocal");
app.get("/_health", (_req, res) => res.type("text/plain").send("ok"));
if (!DEVELOPMENT) {
  // 浏览器只经 HTTPS 访问（Cloudflare）；明文的本机访问不受影响，浏览器只认 HTTPS 响应里的这个头
  app.use((_req, res, next) => {
    res.set("Strict-Transport-Security", "max-age=31536000");
    next();
  });
}
app.use(forwardToBackend(API_ORIGIN));

if (DEVELOPMENT) {
  const vite = await import("vite").then((m) => m.createServer({ server: { middlewareMode: true } }));
  app.use(vite.middlewares);
  app.use(async (req, res, next) => {
    try {
      const source = await vite.ssrLoadModule("./server/app.ts");
      return await source.app(req, res, next);
    } catch (error) {
      if (error instanceof Error) vite.ssrFixStacktrace(error);
      next(error);
    }
  });
} else {
  // 带内容哈希的文件长缓存；其余（favicon）缓存 1 小时。设了 ASSETS_ARCHIVE 时，本版本没有的文件再到归档里找：
  // 发布前的旧页面还会加载旧版本的分块（归档失败只记日志，不影响出页面）。都没有就直接 404：不走页面渲染，
  // 不进错误日志（探测的请求会刷屏），也不让 Cloudflare 缓存（发布过程中可能只是暂时没有）
  const assets = { immutable: true, maxAge: "1y" };
  const archived = [];
  if (ARCHIVE) {
    await archiveAssets("build/client/assets", ARCHIVE).catch((error) => console.error("assets archive:", error));
    archived.push(express.static(ARCHIVE, assets));
  }
  app.use("/assets", express.static("build/client/assets", assets), ...archived, (_req, res) => {
    res.status(404).set("Cache-Control", "no-store").type("text/plain").send("Not Found");
  });
  app.use(express.static("build/client", { maxAge: "1h" }));
  app.use(await import(BUILD_PATH).then((mod) => mod.app));
}

const server = app.listen(PORT, () => {
  const address = server.address();
  console.log(`listening on http://127.0.0.1:${typeof address === "object" && address ? address.port : PORT}`);
});
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 10_000).unref();
  });
}
