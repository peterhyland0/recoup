FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN mkdir -p public
# Same origin as the API behind the load balancer, so no CORS in production.
ARG NEXT_PUBLIC_RECOUP_API=/api
ARG NEXT_PUBLIC_RECOUP_API_KEY
ENV NEXT_PUBLIC_RECOUP_API=$NEXT_PUBLIC_RECOUP_API NEXT_PUBLIC_RECOUP_API_KEY=$NEXT_PUBLIC_RECOUP_API_KEY
RUN npm run build

FROM node:22-alpine
WORKDIR /web
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /web/.next/standalone ./
COPY --from=build /web/.next/static ./.next/static
COPY --from=build /web/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
