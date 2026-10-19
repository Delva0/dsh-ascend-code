/** Automatic, idempotent preparation shared by the plugin and optional setup CLI. */
import {spawn, spawnSync} from 'node:child_process';
import {closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, rmSync,
  statSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {dirname, join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {Docker} from './docker.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const preparations = new Map();
const stateHome = () => join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'ascend');
const json = path => JSON.parse(readFileSync(path, 'utf8'));
const connectionSettings = options => ({container: options.container, volume: options.volume,
  image: options.image, githubCommand: options.githubCommand});
function save(path, value) {
  const pending = `${path}.${process.pid}.tmp`;
  writeFileSync(pending, JSON.stringify(value, null, 2) + '\n', {mode: 0o600});
  renameSync(pending, path);
}
function alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === 'EPERM'; }
}

export function ensureWorkspace(config = {}, report = console.error, signal) {
  const directory = stateHome();
  const options = {container: config.container || 'dsh-ascend-workspace',
    volume: config.volume || config.container || 'dsh-ascend-workspace',
    image: config.image || 'dsh-ascend:0.1.0', workdir: config.workdir || '/workspace',
    githubCommand: config.githubCommand || join(directory, 'runtimes/github-mcp-0.1.1/bin/github-mcp'),
    provisionGithub: !config.githubCommand};
  const key = createHash('sha256').update(JSON.stringify({directory, ...options,
    dockerHost: process.env.DOCKER_HOST, dockerContext: process.env.DOCKER_CONTEXT})).digest('hex');
  if (preparations.has(key)) return preparations.get(key);
  const preparation = (async () => {
    if (existsSync(options.githubCommand)) {
      try {
        await new Docker(options, process.env).connect(signal);
        return connectionSettings(options);
      } catch { /* The worker checks prerequisites and repairs missing preparation. */ }
    }
    signal?.throwIfAborted();
    mkdirSync(join(directory, 'provision'), {recursive: true});
    const status = join(directory, 'provision', `${randomUUID()}.json`);
    const log = join(directory, `setup-${key.slice(0, 12)}.log`);
    const request = {...options, status, lock: join(directory, 'provision', key), root};
    report(`Ascend: automatically preparing the container, LSP and repositories. Log: ${log}`);
    const fd = openSync(log, 'a', 0o600);
    const child = spawn(process.execPath, [fileURLToPath(import.meta.url), JSON.stringify(request)],
      {detached: true, stdio: ['ignore', fd, fd]});
    closeSync(fd);
    let failure;
    child.on('error', error => { failure = error; });
    child.unref();
    for (;;) {
      signal?.throwIfAborted();
      if (existsSync(status)) {
        const result = json(status);
        if (result.error) throw new Error(`${result.error}\nSetup log: ${log}`);
        report('Ascend: container, LSP, vllm and vllm-ascend are ready.');
        return result;
      }
      if (failure) throw failure;
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(`Ascend preparation exited before reporting readiness. Setup log: ${log}`);
      }
      // Cancel this observer on unload; the detached preparer preserves its progress.
      await delay(250, undefined, {signal});
    }
  })();
  preparations.set(key, preparation);
  preparation.catch(() => preparations.delete(key));
  return preparation;
}

function run(argv, {capture = false, allowFailure = false, input} = {}) {
  const result = spawnSync(argv[0], argv.slice(1), {cwd: root, encoding: 'utf8',
    input, maxBuffer: 8 * 1024 * 1024,
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : [input ? 'pipe' : 'ignore', 'inherit', 'inherit']});
  if (!allowFailure && (result.error || result.status !== 0)) {
    throw new Error(result.error?.message || result.stderr || `${argv[0]} exited ${result.status}`);
  }
  return capture ? (result.status === 0 ? result.stdout.trim() : null) : result.status;
}

function seedRepositories(options) {
  // Host Git can use a user's proxy/credential helper; no host directory is mounted.
  if (!run(['git', '--version'], {capture: true, allowFailure: true})) return;
  for (const name of ['vllm', 'vllm-ascend']) {
    if (run(['docker', 'exec', options.container, 'test', '-e', `/workspace/${name}`],
      {allowFailure: true}) === 0) continue;
    const staging = join(stateHome(), 'staging', randomUUID());
    const checkout = join(staging, name);
    mkdirSync(dirname(checkout), {recursive: true});
    const temporary = `/workspace/.ascend-import-${randomUUID()}`;
    try {
      console.log(`Automatically cloning ${name} through host Git, with full history and tags`);
      try {
        run(['git', 'clone', `https://github.com/vllm-project/${name}.git`, checkout]);
      } catch (error) {
        console.warn(`Host Git failed; the container will retry ${name}: ${error.message}`);
        continue;
      }
      run(['docker', 'cp', checkout, `${options.container}:${temporary}`]);
      run(['docker', 'exec', '--user', '0', options.container, 'chown', '-R', '1000:1000', temporary]);
      // Publish under the same lock as container-side preparation, preserving an existing checkout.
      run(['docker', 'exec', '-i', options.container, '/opt/ascend/.venv/bin/python', '-', temporary, name],
        {input: 'import fcntl, pathlib, shutil, sys\n'
          + 'root = pathlib.Path("/workspace")\n'
          + '(root / ".dsh-ascend").mkdir(exist_ok=True)\n'
          + 'with (root / ".dsh-ascend/prepare.lock").open("a") as lock:\n'
          + '    fcntl.flock(lock, fcntl.LOCK_EX)\n'
          + '    temporary, target = pathlib.Path(sys.argv[1]), root / sys.argv[2]\n'
          + '    if target.exists(): shutil.rmtree(temporary)\n'
          + '    else: temporary.rename(target)\n'});
    } finally {
      rmSync(staging, {recursive: true, force: true});
    }
  }
}

async function provision(options) {
  let locked = false;
  try {
    for (;;) {
      try {
        mkdirSync(options.lock);
        save(join(options.lock, 'owner.json'), {pid: process.pid});
        locked = true;
        break;
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        try {
          const owner = join(options.lock, 'owner.json');
          if (existsSync(owner) ? !alive(json(owner).pid) : Date.now() - statSync(options.lock).mtimeMs > 10000) {
            rmSync(options.lock, {recursive: true, force: true});
            continue;
          }
        } catch (lockError) {
          if (lockError.code === 'ENOENT') continue;
          throw lockError;
        }
        await delay(500);
      }
    }
    if (existsSync(options.githubCommand)) {
      try {
        await new Docker(options, process.env).connect();
        save(options.status, connectionSettings(options));
        return;
      } catch { /* Another preparer may have completed while this worker waited. */ }
    }
    console.log(`\nAscend preparation started at ${new Date().toISOString()}`);
    run(['docker', 'info'], {capture: true});
    if (!existsSync(options.githubCommand)) {
      if (!options.provisionGithub) throw new Error(`Configured GitHub MCP does not exist: ${options.githubCommand}`);
      run(['uv', '--version'], {capture: true});
      const wheel = join(root, 'assets/github_mcp-0.1.1-py3-none-any.whl');
      if (createHash('sha256').update(readFileSync(wheel)).digest('hex') !==
          '192a06ccfe0c98ebd6e543a455af0f0e437b83956224bcf95c86f2be1f247ec1') {
        throw new Error('Bundled GitHub MCP checksum mismatch');
      }
      const environment = resolve(options.githubCommand, '../..');
      run(['uv', 'venv', '--python', '3.13', '--allow-existing', environment]);
      run(['uv', 'pip', 'install', '--python', join(environment, 'bin/python'), wheel]);
    }
    run(['docker', 'build', '-t', options.image, root]);
    const existing = run(['docker', 'inspect', '--type', 'container', options.container],
      {capture: true, allowFailure: true});
    if (existing) {
      const [data] = JSON.parse(existing);
      if (data.Config.Labels?.['io.delva.dsh-ascend'] !== '0.1.0'
          || !data.Mounts.some(m => m.Type === 'volume' && m.Name === options.volume && m.Destination === '/workspace')) {
        throw new Error(`Existing container ${options.container} does not match the configured workspace`);
      }
      if (!data.State.Running) run(['docker', 'start', data.Id]);
    } else {
      run(['docker', 'volume', 'create', options.volume]);
      run(['docker', 'run', '-d', '--name', options.container, '--restart', 'unless-stopped',
        '--mount', `type=volume,src=${options.volume},dst=/workspace`, options.image]);
    }
    seedRepositories(options);
    run(['docker', 'exec', '-i', options.container, '/opt/ascend/.venv/bin/python', '-'],
      {input: readFileSync(join(root, 'scripts/prepare_workspace.py'), 'utf8')});
    run(['docker', 'exec', options.container, '/opt/ascend/.venv/bin/lsp', '--version']);
    save(options.status, connectionSettings(options));
  } catch (error) {
    console.error(error.message);
    save(options.status, {error: error.message});
    process.exitCode = 1;
  } finally {
    if (locked) rmSync(options.lock, {recursive: true, force: true});
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await provision(JSON.parse(process.argv[2]));
}
