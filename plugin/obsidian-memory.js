import { spawn } from "node:child_process";
import { readFileSync, appendFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

// Every OpenCode location may load this plugin; the worker's OS file lock
// guarantees that exactly one process consumes the queue.
const root =
  process.env.SECOND_BRAIN_ROOT ||
  path.join(homedir(), ".config/opencode/second-brain");

function pythonCommand() {
  if (process.env.SECOND_BRAIN_PYTHON) return [process.env.SECOND_BRAIN_PYTHON, []];
  if (process.platform === "win32") return ["pythonw", []];
  return ["python3", []];
}

function ensureWorker() {
  try {
    const health = JSON.parse(readFileSync(path.join(root, "state/health.json"), "utf8"));
    if (Date.now() / 1000 - health.time < 90) return;
  } catch {}
  const [command, extra] = pythonCommand();
  const child = spawn(command, [...extra, "-X", "utf8", path.join(root, "worker.py")], {
    cwd: root, detached: true, windowsHide: true, stdio: "ignore",
  });
  child.on("error", error => {
    try {
      appendFileSync(path.join(root, "state/launcher.log"), `${new Date().toISOString()} ${error.message}\n`);
    } catch {}
  });
  child.unref();
}

export default {
  id: "obsidian-memory",
  async setup() {
    ensureWorker();
    // Restart the worker if it dies; the loop itself never needs the UI open.
    const timer = setInterval(ensureWorker, 60000);
    timer.unref?.();
    return () => clearInterval(timer);
  },
};
