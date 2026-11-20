FROM node:22-slim

WORKDIR /app

COPY package.json ./
RUN npm install --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
COPY config ./config

RUN npm run build

ENV NODE_ENV=production

CMD ["node", "dist/index.js"]
