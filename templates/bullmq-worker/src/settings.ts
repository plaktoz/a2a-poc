/** The worker's settings, read from the environment. worker.ts (fixed) calls these when it starts,
 *  so a setting that needs more logic than a default is changed here, not there. */
export function concurrency(env: NodeJS.ProcessEnv = process.env): number {
  return Number(env.CONCURRENCY ?? 2)
}

export function queueName(env: NodeJS.ProcessEnv = process.env): string {
  return env.QUEUE_NAME ?? 'jobs'
}
