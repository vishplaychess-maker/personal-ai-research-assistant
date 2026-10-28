FROM node:20-alpine

WORKDIR /app

# The uid-1000 `node` user must own /app before npm writes node_modules, and
# the source copy must be node-owned too. This replaces a recursive chown,
# which is prohibitively slow on a populated node_modules tree.
RUN chown node:node /app

COPY --chown=node:node package.json package-lock.json* ./
USER node
RUN npm install

COPY --chown=node:node . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
