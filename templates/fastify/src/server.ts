import { buildApp } from './app.js'

const port = Number(process.env.PORT ?? 8000)
await buildApp().listen({ host: '127.0.0.1', port })
