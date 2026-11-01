import { buildApp } from './app.js'

const host = process.env.HOST ?? '0.0.0.0'
const port = Number(process.env.PORT ?? 8000)
await buildApp().listen({ host, port })
