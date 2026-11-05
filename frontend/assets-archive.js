// 发布后保留旧版本的 /assets 文件（D60）：Cloudflare 与浏览器里缓存的旧页面还会按旧文件名加载分块和样式，
// 新镜像里没有这些文件就是 404。ASSETS_ARCHIVE 指向一个持久卷：启动时把本版本的文件复制进去，在 .builds.json 里
// 记下各版本的文件清单与被替换的时间；被替换满 7 天的版本从记录里去掉，不再被任何版本用到的文件随之删除。
import { copyFile, mkdir, readdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const KEEP_MS = 7 * 24 * 3600 * 1000;
const RECORD = ".builds.json";

/**
 * @param {string} source 本版本的 build/client/assets
 * @param {string} archive 归档目录（持久卷）
 */
export async function archiveAssets(source, archive, now = Date.now()) {
  await mkdir(archive, { recursive: true });
  const files = (await readdir(source, { withFileTypes: true }))
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name)
    .sort();
  // 每次构建的路由清单（manifest-<哈希>.js）文件名都不同，用它标识版本
  const build = files.find((name) => name.startsWith("manifest-")) ?? files.join(" ");
  const builds = await readFile(path.join(archive, RECORD), "utf8")
    .then(JSON.parse)
    .catch(() => ({}));
  for (const [id, entry] of Object.entries(builds)) {
    if (id !== build) entry.replaced ??= now;
    if (now - (entry.replaced ?? now) > KEEP_MS) delete builds[id];
  }
  builds[build] = { files, replaced: null };

  const present = new Set(await readdir(archive));
  for (const name of files) {
    if (present.has(name)) continue;
    // 先写临时文件再改名：中途退出不会留下半个文件（文件按一年不变缓存）
    const tmp = path.join(archive, `.${name}.tmp`);
    await copyFile(path.join(source, name), tmp);
    await rename(tmp, path.join(archive, name));
  }
  const keep = new Set([RECORD, ...Object.values(builds).flatMap((entry) => entry.files)]);
  for (const name of present) {
    if (!keep.has(name)) await rm(path.join(archive, name), { force: true, recursive: true });
  }
  await writeFile(path.join(archive, `${RECORD}.tmp`), JSON.stringify(builds));
  await rename(path.join(archive, `${RECORD}.tmp`), path.join(archive, RECORD));
}
