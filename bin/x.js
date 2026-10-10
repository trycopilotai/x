#!/usr/bin/env node
'use strict';

// Launcher for x. The engine is Python; this finds an interpreter
// new enough to run it and hands over. Arguments, streams and the
// exit status pass through unchanged.
//
// Where this process stays as the parent it forwards SIGTERM and
// SIGHUP, and SIGINT and SIGQUIT unless its standard input is a
// terminal. A signal that ends the interpreter ends this process the
// same way when Node lets it. Node keeps SIGUSR1 for its debugger and
// ignores some others, SIGPIPE among them; for those this process
// exits with 128 plus the signal's number instead. A signal Node has
// no name for cannot be told apart from a clean exit.

const { spawn, spawnSync } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');

const MINIMUM = [3, 11];
const PACKAGE_ROOT = path.resolve(__dirname, '..');

// npm exports the flags of the `npx` that started this process to
// every child. Left in place they hijack a nested `npx`, which then
// runs this package again instead of the one it was asked for.
const LEAKED_ENV = ['npm_config_package', 'npm_config_call'];

const PROBE =
  'import sys; print("%d.%d.%d" % sys.version_info[:3]); print(sys.executable)';

function candidates(platform, env) {
  if (env.X_PYTHON) {
    return [{ command: env.X_PYTHON, args: [], explicit: true }];
  }
  if (platform === 'win32') {
    return [
      { command: 'py', args: ['-3'] },
      { command: 'python', args: [] },
      { command: 'python3', args: [] },
    ];
  }
  const names = [
    'python3',
    'python3.13',
    'python3.12',
    'python3.11',
    'python3.14',
    'python',
  ];
  return names.map((command) => ({ command, args: [] }));
}

function label(candidate) {
  return [candidate.command, ...candidate.args].join(' ');
}

function supported(version) {
  const [major, minor] = version.split('.').map(Number);
  return major > MINIMUM[0] || (major === MINIMUM[0] && minor >= MINIMUM[1]);
}

function probe(candidate, env) {
  const result = spawnSync(
    candidate.command,
    [...candidate.args, '-c', PROBE],
    { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
  );
  if (result.error || result.status !== 0 || !result.stdout) {
    return { found: false };
  }
  const [version, executable] = result.stdout.trim().split(/\r?\n/);
  if (!version || !executable) {
    return { found: false };
  }
  return { found: true, version, executable, ok: supported(version) };
}

function findPython(platform, env) {
  const tried = [];
  for (const candidate of candidates(platform, env)) {
    const result = probe(candidate, env);
    if (result.found && result.ok) {
      return { executable: result.executable, tried };
    }
    if (result.found) {
      tried.push(`${label(candidate)} (${result.version} at ${result.executable})`);
    } else {
      tried.push(`${label(candidate)} (not found)`);
    }
    if (candidate.explicit) {
      break;
    }
  }
  return { executable: null, tried };
}

function childEnvironment(env) {
  const selected = { ...env };
  for (const key of LEAKED_ENV) {
    delete selected[key];
  }
  return selected;
}

function missingPython(tried) {
  const minimum = MINIMUM.join('.');
  return [
    `x: needs Python ${minimum} or newer, and none was found.`,
    `  tried: ${tried.join(', ')}`,
    `  fix:   install Python ${minimum}+ on PATH, or set X_PYTHON=/path/to/python`,
  ].join('\n');
}

function run(executable, args, env) {
  if (process.platform !== 'win32' && typeof process.execve === 'function') {
    try {
      // Replaces this process, so there is nothing left to relay.
      process.execve(executable, [executable, ...args], env);
    } catch (error) {
      // Fall through to a child process.
    }
  }

  const child = spawn(executable, args, { stdio: 'inherit', env });
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  if (process.platform !== 'win32') {
    signals.push('SIGQUIT');
  }
  const listeners = new Map();
  for (const name of signals) {
    const listener = () => {
      // Ctrl-C and Ctrl-\ reach the whole foreground process group,
      // so the child already has its own copy of those.
      const keyboard = name === 'SIGINT' || name === 'SIGQUIT';
      if (keyboard && process.stdin.isTTY) {
        return;
      }
      child.kill(name);
    };
    listeners.set(name, listener);
    process.on(name, listener);
  }

  child.on('error', (error) => {
    process.stderr.write(`x: could not start ${executable}: ${error.message}\n`);
    process.exit(127);
  });
  child.on('exit', (code, signal) => {
    for (const [name, listener] of listeners) {
      process.removeListener(name, listener);
    }
    if (signal) {
      const status = 128 + (os.constants.signals[signal] || 0);
      if (signal === 'SIGUSR1') {
        // Sending this one to ourselves would start Node's debugger.
        process.exit(status);
      }
      process.kill(process.pid, signal);
      // Reached only if the signal did not end this process.
      setTimeout(() => process.exit(status), 100);
      return;
    }
    if (code === null) {
      process.exit(1);
    }
    process.exit(code);
  });
}

function main() {
  const env = childEnvironment(process.env);
  const { executable, tried } = findPython(process.platform, env);
  if (executable === null) {
    process.stderr.write(missingPython(tried) + '\n');
    process.exit(127);
  }
  // -E and -s keep PYTHONPATH and the user site directory from
  // shadowing the engine; running the package directory puts it
  // first on the import path.
  const args = ['-E', '-s', '-X', 'utf8', PACKAGE_ROOT, ...process.argv.slice(2)];
  run(executable, args, env);
}

if (require.main === module) {
  main();
} else {
  module.exports = { candidates, childEnvironment, missingPython, probe, supported };
}
