/** Automatic, idempotent preparation shared by the plugin and optional setup CLI. */
import {spawn, spawnSync} from 'node:child_process';
import {closeSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmSync,
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
  image: options.image, githubPython: options.githubPython});
function githubRuntime(directory) {
  const hash = createHash('sha256');
  for (const path of ['pyproject.toml', 'uv.lock', ...readdirSync(join(root, 'src/ascend_github'), {recursive: true})
    .filter(path => /\.(py|json|graphql)$/.test(path)).sort().map(path => `src/ascend_github/${path}`)]) {
    hash.update(path).update(readFileSync(join(root, path)));
  }
  return join(directory, 'runtimes', `github-${hash.digest('hex').slice(0, 16)}`);
}
const githubPrepared = options => existsSync(options.githubPython)
  && (!options.provisionGithub || existsSync(options.githubReady));
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
  const runtime = githubRuntime(directory);
  const options = {container: config.container || 'dsh-ascend-workspace',
    volume: config.volume || config.container || 'dsh-ascend-workspace',
    image: config.image || 'dsh-ascend:0.1.0', workdir: config.workdir || '/workspace',
    githubPython: config.githubPython || join(runtime, 'bin/python'),
    githubReady: join(runtime, '.ready'),
    provisionGithub: !config.githubPython};
  const key = createHash('sha256').update(JSON.stringify({directory, ...options,
    dockerHost: process.env.DOCKER_HOST, dockerContext: process.env.DOCKER_CONTEXT})).digest('hex');
  if (preparations.has(key)) return preparations.get(key);
  const preparation = (async () => {
    if (githubPrepared(options)) {
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

function run(argv, {capture = false, allowFailure = false, input, env} = {}) {
  const result = spawnSync(argv[0], argv.slice(1), {cwd: root, encoding: 'utf8',
    input, env: {...process.env, ...env}, maxBuffer: 8 * 1024 * 1024,
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
    if (githubPrepared(options)) {
      try {
        await new Docker(options, process.env).connect();
        save(options.status, connectionSettings(options));
        return;
      } catch { /* Another preparer may have completed while this worker waited. */ }
    }
    console.log(`\nAscend preparation started at ${new Date().toISOString()}`);
    run(['docker', 'info'], {capture: true});
    if (!githubPrepared(options)) {
      if (!options.provisionGithub) throw new Error(`Configured GitHub Python does not exist: ${options.githubPython}`);
      run(['uv', '--version'], {capture: true});
      const environment = resolve(options.githubPython, '../..');
      run(['uv', 'sync', '--project', root, '--python', '3.13', '--frozen', '--extra', 'github',
        '--no-dev', '--no-editable'], {env: {UV_PROJECT_ENVIRONMENT: environment}});
      run([options.githubPython, '-I', '-c', 'from ascend_github.worker import BACKENDS; '
        + 'from ascend_github.web_worker import definition; '
        + 'assert len(BACKENDS) == 3 and definition()["name"] == "web_fetch"']);
      writeFileSync(options.githubReady, 'ready\n');
    }
    // A new host GitHub runtime does not require rebuilding a healthy workspace.
    try {
      await new Docker(options, process.env).connect();
      save(options.status, connectionSettings(options));
      return;
    } catch { /* Continue with container preparation. */ }
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
