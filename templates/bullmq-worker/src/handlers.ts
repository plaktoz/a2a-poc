/** What the worker does for each kind of job. A handler gets the job's data and returns its result;
 *  it throws to fail the job, and BullMQ retries it. Handlers do not touch the queue, so tests call
 *  them directly with no Redis. */
export type Handler = (data: any) => Promise<unknown>

export const handlers: Record<string, Handler> = {
  ping: async () => ({ pong: true }),
}

export async function handle(name: string, data: unknown): Promise<unknown> {
  const handler = handlers[name]
  if (!handler) throw new Error(`no handler for job '${name}'`)
  return handler(data)
}
