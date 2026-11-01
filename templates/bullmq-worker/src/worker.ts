import { Worker } from 'bullmq'
import { handle } from './handlers.js'
import { concurrency, queueName } from './settings.js'

const redis = new URL(process.env.REDIS_URL ?? 'redis://127.0.0.1:6379')

const worker = new Worker(queueName(), (job) => handle(job.name, job.data), {
  connection: {
    host: redis.hostname,
    port: Number(redis.port || 6379),
    password: redis.password || undefined,
    maxRetriesPerRequest: null,
  },
  concurrency: concurrency(),
})
worker.on('failed', (job, err) => console.error(`job ${job?.name} ${job?.id} failed: ${err.message}`))
process.on('SIGTERM', () => void worker.close())
