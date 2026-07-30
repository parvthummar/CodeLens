# Development image: it runs the Vite dev server, not a production build.
#
# Deliberate, for now. The API base URL is hardcoded in src/api/client.js, so a
# static build would bake in whatever host it was built against. Once that moves
# to a Vite env var (roadmap step 6) this becomes a two-stage build + nginx.
FROM node:22-slim

ENV NODE_ENV=development
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

USER node
EXPOSE 5173

# --host binds 0.0.0.0; the default 127.0.0.1 is unreachable from outside the
# container even with the port published.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
