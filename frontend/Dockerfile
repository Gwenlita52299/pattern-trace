FROM node:20-alpine
WORKDIR /app

# 锁文件先行以复用依赖层；npm ci 保证可复现安装
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# NEXT_PUBLIC_* 在构建期内联进 bundle，运行时注入无效——必须走 build arg
ARG NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

EXPOSE 3000
# 生产跑 next start（SSR 生产服务器），不是 dev server
CMD ["npm", "run", "start"]
