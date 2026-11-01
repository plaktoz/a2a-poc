import Fastify, { type FastifyInstance } from 'fastify'

/** A fresh app with its own state. Tests call this for every test; server.ts calls it once. */
export function buildApp(): FastifyInstance {
  const app = Fastify()
  app.get('/api/health', async () => ({ status: 'ok' }))
  return app
}
