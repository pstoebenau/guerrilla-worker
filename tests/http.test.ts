import { test, expect } from 'bun:test';
import { ApiError, Client, describeWorkerError } from '../agent/http';

test('transport diagnostics retain DNS and nested socket codes while omitting private cause messages',()=> {
  const nested = new AggregateError([
    Object.assign(new Error('private address and credentials'),{code:'ECONNREFUSED'}),
    Object.assign(new Error('private signed URL'),{code:'ENOTFOUND'}),
  ]);
  const error = new TypeError('fetch failed',{cause:nested});
  expect(describeWorkerError(error)).toBe('fetch failed (ECONNREFUSED, ENOTFOUND)');
  expect(describeWorkerError(new Error('Request to https://example.com/private?token=secret with Bearer credential'))).toBe('Request to [redacted URL] with Bearer [redacted]');
  expect(describeWorkerError(Object.assign(new Error('fetch failed'),{code:'PRIVATE_SECRET_VALUE'}))).toBe('fetch failed');
  const cycle = Object.assign(new Error('fetch failed'),{code:'UND_ERR_CONNECT_TIMEOUT',cause:null as unknown});
  cycle.cause=cycle;
  expect(describeWorkerError(cycle)).toBe('fetch failed (UND_ERR_CONNECT_TIMEOUT)');
});

test('HTTP failures identify the operation and do not retry lost ownership',async()=> {
  let calls=0;
  const server=Bun.serve({port:0,fetch(){calls++;return new Response(null,{status:409});}});
  try {
    await expect(new Client(server.url.href,'').post('heartbeat',{})).rejects.toThrow('Worker API heartbeat rejected request (409)');
    expect(calls).toBe(1);
    try {await new Client(server.url.href,'').post('heartbeat',{});}
    catch(error){expect(error).toBeInstanceOf(ApiError);}
  } finally {server.stop(true);}
});

test('exhausted transport retries expose operation and cause without exposing the endpoint',async()=> {
  const server=Bun.serve({port:0,fetch(){return Response.json({});}});
  const url=server.url.href;server.stop(true);
  await expect(new Client(url,'').post('poll',{})).rejects.toThrow(/Worker poll request failed/);
  try {await new Client(url,'').post('poll',{});}
  catch(error){expect(describeWorkerError(error)).not.toContain(url);}
},10000);
