# Build stage
FROM node:22-alpine as build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Serve stage
FROM nginx:alpine
# Copy custom nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Copy built assets to Nginx
COPY --from=build /app/dist /usr/share/nginx/html
# Expose port 80 inside the container
EXPOSE 80
# Run nginx
CMD ["nginx", "-g", "daemon off;"]
