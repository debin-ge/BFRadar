# 前端镜像（M3.8）：pnpm 构建 React Router SSR，运行时只带生产依赖与构建产物；Node 24、非 root。
# 构建上下文为仓库根（docker-compose.yml 里 context: .）：界面字典在 backend/app/i18n（与 RSS 共用），构建时打进包里。
# 上下文只放行 frontend/ 与字典，见 Dockerfile.dockerignore。
ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-slim AS base
ARG PNPM_VERSION=11.6.0
RUN npm install --global pnpm@${PNPM_VERSION} && npm cache clean --force
WORKDIR /src/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./

# 依赖单独一层，只改代码时不重装；pnpm 的下载缓存放在构建缓存里，不进镜像
FROM base AS build
RUN --mount=type=cache,id=bfradar-pnpm,target=/pnpm-store pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY backend/app/i18n /src/backend/app/i18n
COPY frontend ./
RUN pnpm run build

FROM base AS prod-deps
RUN --mount=type=cache,id=bfradar-pnpm,target=/pnpm-store pnpm install --frozen-lockfile --prod --store-dir /pnpm-store

FROM node:${NODE_VERSION}-slim
ENV NODE_ENV=production \
    PORT=3000
WORKDIR /app
COPY --from=prod-deps /src/frontend/node_modules ./node_modules
COPY --from=build /src/frontend/build ./build
# 运行时只要 server.js 和它导入的同级模块（*.js；配置文件是 .ts，不进镜像）
COPY frontend/package.json frontend/*.js ./
# 旧版本静态文件的归档目录（ASSETS_ARCHIVE），由卷挂载；卷第一次挂上时沿用这里的属主
RUN mkdir assets-archive && chown node:node assets-archive

USER node
EXPOSE 3000
CMD ["node", "server.js"]
