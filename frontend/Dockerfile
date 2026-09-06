FROM node:24-alpine
WORKDIR /app

# 锁文件先行以复用依赖层；npm ci 保证可复现安装
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# rewrites 目的地在 next build 时烘焙进 routes-manifest——必须走 build arg
# （浏览器侧 API_BASE 固定同源 /api/v1，无 NEXT_PUBLIC_* 需求）
ARG API_PROXY_URL=http://localhost:8000
ENV API_PROXY_URL=$API_PROXY_URL

RUN npm run build

EXPOSE 3000
# 生产跑 next start（SSR 生产服务器），不是 dev server
CMD ["npm", "run", "start"]
