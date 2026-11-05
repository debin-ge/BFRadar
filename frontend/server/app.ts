import { createRequestHandler } from "@react-router/express";
import express, { type Express } from "express";

export const app: Express = express();
app.disable("x-powered-by"); // 挂在 server.js 的应用下面，设置不继承，要单独关

app.use(
  createRequestHandler({
    build: () => import("virtual:react-router/server-build"),
  }),
);
