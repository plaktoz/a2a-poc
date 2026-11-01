import { Worker } from 'bullmq'
import { handle } from './handlers.js'

const redis = new URL(process.env.REDIS_URL ?? 'redis://127.0.0.1:6379')
const queue = process.env.QUEUE_NAME ?? 'jobs'

const worker = new Worker(queue, (job) => handle(job.name, job.data), {
  connection: {
    host: redis.hostname,
    port: Number(redis.port || 6379),
    password: redis.password || undefined,
    maxRetriesPerRequest: null,
  },
  concurrency: Number(process.env.CONCURRENCY ?? 2),
})
worker.on('failed', (job, err) => console.error(`job ${job?.name} ${job?.id} failed: ${err.message}`))
process.on('SIGTERM', () => void worker.close())
