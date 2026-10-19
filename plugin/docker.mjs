/** Docker transport pinned to one daemon and full container identity. */
import {spawn} from 'node:child_process';

export function command(argv, {input, env = process.env, signal, timeout = 30000} = {}) {
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const child = spawn(argv[0], argv.slice(1), {env, stdio: ['pipe', 'pipe', 'pipe']});
    const output = [], errors = [];
    let bytes = 0, errorBytes = 0, failure;
    const stop = reason => { failure ??= reason; child.kill('SIGKILL'); };
    const abort = () => stop(signal.reason ?? new Error('Operation aborted'));
    const timer = setTimeout(() => stop(new Error(`${argv[0]} timed out`)), timeout);
    signal?.addEventListener('abort', abort, {once: true});
    if (signal?.aborted) abort();
    child.stdout.on('data', part => {
      bytes += part.length;
      if (bytes > 8 * 1024 * 1024) stop(new Error('Docker protocol output exceeded 8 MiB'));
      else output.push(part);
    });
    child.stderr.on('data', part => { if ((errorBytes += part.length) <= 65536) errors.push(part); });
    child.on('error', error => { failure ??= error; });
    child.stdin.on('error', error => { failure ??= error; });
    child.on('close', code => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      if (failure) reject(failure);
      else if (code !== 0) reject(new Error(Buffer.concat(errors).toString().trim() || `${argv[0]} exited ${code}`));
      else resolve(Buffer.concat(output).toString());
    });
    child.stdin.end(input);
  });
}

export class Docker {
  constructor(config, env) { this.config = config; this.env = {...env}; this.prefix = []; }
  run(args, options) { return command(['docker', ...this.prefix, ...args], {env: this.env, ...options}); }
  async endpoint() {
    const [context] = JSON.parse(await this.run(['context', 'inspect', this.context]));
    return JSON.stringify(context.Endpoints.docker);
  }
  async connect(signal) {
    if (this.env.DOCKER_HOST && !this.env.DOCKER_CONTEXT) this.prefix = ['--host', this.env.DOCKER_HOST];
    else {
      this.context = (await this.run(['context', 'show'], {signal})).trim();
      this.prefix = ['--context', this.context];
      this.pinnedEndpoint = await this.endpoint();
    }
    delete this.env.DOCKER_HOST;
    delete this.env.DOCKER_CONTEXT;
    const [container] = JSON.parse(await this.run(['inspect', '--type', 'container', this.config.container], {signal}));
    if (container.Config.Labels?.['io.delva.dsh-ascend'] !== '0.1.0') {
      throw new Error('Selected container is not a dsh-ascend 0.1.0 workspace. Run npm run setup.');
    }
    this.id = container.Id;
    this.image = container.Image;
    if (!container.State.Running) await this.run(['start', this.id], {signal});
    return this.request({action: 'info', workdir: this.config.workdir}, signal);
  }
  async request(body, signal) {
    signal?.throwIfAborted();
    if (this.context && await this.endpoint() !== this.pinnedEndpoint) {
      throw new Error('Docker context endpoint changed; refusing a different daemon');
    }
    const output = await this.run(['exec', '-i', this.id, '/opt/ascend/.venv/bin/python',
      '-m', 'ascend_workspace.rpc'], {input: JSON.stringify(body), signal});
    const response = JSON.parse(output);
    if (response.error) throw new Error(`${response.error.type}: ${response.error.message}`);
    return response.result;
  }
}
