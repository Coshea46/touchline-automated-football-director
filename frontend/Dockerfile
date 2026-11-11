FROM node:24-alpine AS build

WORKDIR /app/frontend

COPY frontend/package*.json ./

RUN npm ci

COPY frontend/ ./
COPY scripts/sync-match-catalog.cjs /app/scripts/sync-match-catalog.cjs
COPY backend/src/touchline/data/matches.json /app/backend/src/touchline/data/matches.json

ARG REACT_APP_WS_URL=ws://localhost:8000
ENV REACT_APP_WS_URL=$REACT_APP_WS_URL
ARG REACT_APP_API_URL=http://localhost:8031
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN npm run build

FROM nginx:stable-alpine
COPY --from=build /app/frontend/build /usr/share/nginx/html
EXPOSE 80
