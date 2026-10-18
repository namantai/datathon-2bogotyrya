FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/tsconfig.app.json frontend/tsconfig.node.json frontend/vite.config.ts ./
COPY frontend/src/ ./src/
COPY frontend/scripts/check-production.mjs ./scripts/check-production.mjs
RUN npm run build && npm run test:production

FROM nginx:stable-alpine AS runtime
COPY frontend/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/
# The custom config needs only /tmp to be writable; no root entrypoint scripts.
USER nginx
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
